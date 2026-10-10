"""Какие напоминания пора отправить.

Сервис только решает, что отправить; отправку, mark_sent и commit делает планировщик
(bot/scheduler/jobs.py). Каждое напоминание имеет ключ kind, уникальный в рамках задачи:
отправленные ключи пишутся в ReminderLog и повторно не возвращаются.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db.models import OPEN_STATUSES, ReminderLog, Task, TaskStatus
from bot.services.dbsafe import naive_utc
from bot.utils.dates import days_between, to_local, utcnow

logger = logging.getLogger(__name__)

EMPLOYEE = "employee"
MANAGER = "manager"

KIND_BEFORE_HOURS = "before_hours"
KIND_DEADLINE_PASSED = "deadline_passed"
KIND_OVERDUE_MANAGER = "overdue_manager"


@dataclass
class Reminder:
    """Напоминание к отправке.

    reason: before_days | before_hours | deadline_passed | overdue_daily | overdue_manager | review_pending |
            auto_score | auto_proposal (скоро автоподтверждение — их собирает bot.scheduler.jobs).
    days_left: дней до срока (отрицательное — просрочка); для review_pending — None.
    data: дополнительные сведения для текста (auto_*: due — когда сработает, source — чей вес).
    """

    task: Task
    kind: str        # ключ для ReminderLog, уникален в рамках задачи
    recipient: str   # "employee" | "manager"
    reason: str
    days_left: float | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Threshold:
    days: float   # порог «осталось ≤ days дней»
    kind: str
    reason: str


def _before_thresholds() -> list[_Threshold]:
    """Пороги «до срока» по возрастанию: за N часов, за N дней."""
    settings = get_settings()
    items = [
        _Threshold(n, f"before_{n}d", "before_days")
        for n in set(settings.reminder_days_before)
        if n > 0
    ]
    if settings.reminder_hours_before > 0:
        items.append(_Threshold(settings.reminder_hours_before / 24, KIND_BEFORE_HOURS, "before_hours"))
    return sorted(items, key=lambda t: t.days)


async def due_reminders(session: AsyncSession, now: datetime | None = None) -> list[Reminder]:
    """Напоминания, которые пора отправить (now — naive UTC).

    Ненужные ключи due_reminders сразу помечает отправленными (mark_sent), чтобы не слать их позже:
    * подходят несколько порогов «до срока» (за N дней и «в день срока» за N часов) — возвращается
      только наименьший, остальные пропускаются;
    * уходит «срок истёк» — ежедневное напоминание о просрочке за этот день пропускается.
    """
    now = naive_utc(now or utcnow())  # колонки — naive UTC; aware-дату PostgreSQL не примет
    thresholds = _before_thresholds()
    open_tasks, review_tasks = await _candidate_tasks(session, now, thresholds)
    sent = await sent_kinds(session, [t.id for t in (*open_tasks, *review_tasks)])

    reminders: list[Reminder] = []
    skipped_all: list[tuple[int, str]] = []
    for task in open_tasks:
        if task.deadline < now:
            found, skipped = _overdue_reminders(task, now, sent[task.id])
        else:
            found, skipped = _before_deadline(task, now, thresholds, sent[task.id])
        for kind in skipped:
            logger.debug("Задача #%s: напоминание %s не нужно, помечаем отправленным", task.id, kind)
            skipped_all.append((task.id, kind))
        reminders += found
    await _mark_sent_many(session, skipped_all)

    review_kind = f"review_{to_local(now).date().isoformat()}"
    for task in review_tasks:
        if review_kind not in sent[task.id]:
            reminders.append(Reminder(task, review_kind, MANAGER, "review_pending"))
    return reminders


async def _candidate_tasks(
    session: AsyncSession, now: datetime, thresholds: list[_Threshold]
) -> tuple[list[Task], list[Task]]:
    """(открытые, на проверке) — одним запросом:

    * ACTIVE/REWORK, у которых срок истёк или подошёл к наибольшему порогу (по сроку);
    * SUBMITTED, которые ждут проверки дольше review_reminder_days (по времени сдачи).

    Задания по времени идут каждые 5 минут, а каждый запрос к облачной базе — 130–190 мс.
    """
    horizon_days = max((t.days for t in thresholds), default=0.0)
    cutoff = now - timedelta(days=get_settings().review_reminder_days)
    stmt = select(Task).where(
        or_(
            and_(Task.status.in_(OPEN_STATUSES), Task.deadline <= now + timedelta(days=horizon_days)),
            and_(Task.status == TaskStatus.SUBMITTED, Task.submitted_at < cutoff),
        )
    )
    tasks = list((await session.scalars(stmt)).all())
    open_tasks = sorted((t for t in tasks if t.status in OPEN_STATUSES), key=lambda t: (t.deadline, t.id))
    review_tasks = sorted(
        (t for t in tasks if t.status == TaskStatus.SUBMITTED), key=lambda t: (t.submitted_at, t.id)
    )
    return open_tasks, review_tasks


async def sent_kinds(session: AsyncSession, task_ids: list[int]) -> defaultdict[int, set[str]]:
    """{task_id: {kind, ...}} — уже отправленные напоминания."""
    sent: defaultdict[int, set[str]] = defaultdict(set)
    if task_ids:
        rows = await session.execute(
            select(ReminderLog.task_id, ReminderLog.kind).where(ReminderLog.task_id.in_(task_ids))
        )
        for task_id, kind in rows:
            sent[task_id].add(kind)
    return sent


def _before_deadline(
    task: Task, now: datetime, thresholds: list[_Threshold], sent: set[str]
) -> tuple[list[Reminder], list[str]]:
    """Напоминание по наименьшему подходящему порогу «до срока» + пропускаемые пороги."""
    left = days_between(task.deadline, now)
    applicable = [t for t in thresholds if 0 < left <= t.days]
    if not applicable:
        return [], []
    main, *others = applicable
    found = [] if main.kind in sent else [Reminder(task, main.kind, EMPLOYEE, main.reason, days_left=left)]
    return found, [t.kind for t in others if t.kind not in sent]


def _overdue_reminders(task: Task, now: datetime, sent: set[str]) -> tuple[list[Reminder], list[str]]:
    """«Срок истёк» сотруднику, «просрочена» начальнику и ежедневное напоминание о просрочке.

    Ежедневное не шлётся в тот местный день, когда ушло «срок истёк»: его ключ за этот день
    помечается отправленным вместе с «срок истёк».
    """
    left = days_between(task.deadline, now)
    local_now = to_local(now)
    daily_kind = f"overdue_{local_now.date().isoformat()}"
    found: list[Reminder] = []
    skipped: list[str] = []
    if KIND_DEADLINE_PASSED not in sent:
        found.append(Reminder(task, KIND_DEADLINE_PASSED, EMPLOYEE, "deadline_passed", days_left=left))
        if daily_kind not in sent:
            skipped.append(daily_kind)
    elif daily_kind not in sent and local_now.hour >= get_settings().overdue_reminder_hour:
        found.append(Reminder(task, daily_kind, EMPLOYEE, "overdue_daily", days_left=left))
    if KIND_OVERDUE_MANAGER not in sent:
        found.append(Reminder(task, KIND_OVERDUE_MANAGER, MANAGER, "overdue_manager", days_left=left))
    return found, skipped


async def mark_sent(session: AsyncSession, task_id: int, kind: str) -> None:
    """Пометить напоминание отправленным. Повторный вызов ничего не делает.

    INSERT … ON CONFLICT DO NOTHING (SQLite и PostgreSQL) вместо SAVEPOINT: у драйвера sqlite3
    SAVEPOINT в начале транзакции фиксирует данные досрочно, а сервисы не должны коммитить;
    в PostgreSQL ошибка уникальности без SAVEPOINT обрывает всю транзакцию. ON CONFLICT
    атомарен и при гонке: два экземпляра бота (webhook во время обновления на Render) пишут
    одну отметку — второй дождётся первого и ничего не сделает, без ошибки.
    Другие СУБД — SAVEPOINT + IntegrityError.
    """
    dialect = session.get_bind().dialect.name
    index = [ReminderLog.task_id, ReminderLog.kind]
    if dialect in ("sqlite", "postgresql"):
        insert = sqlite_insert if dialect == "sqlite" else postgresql_insert
        stmt = insert(ReminderLog).values(task_id=task_id, kind=kind).on_conflict_do_nothing(index_elements=index)
        await session.execute(stmt)
        return
    exists = await session.scalar(
        select(ReminderLog.id).where(ReminderLog.task_id == task_id, ReminderLog.kind == kind)
    )
    if exists is not None:
        return
    try:
        async with session.begin_nested():
            session.add(ReminderLog(task_id=task_id, kind=kind))
    except IntegrityError:
        pass  # отметку только что записал параллельный процесс


async def _mark_sent_many(session: AsyncSession, items: list[tuple[int, str]]) -> None:
    """mark_sent для нескольких (task_id, kind) сразу: SQLite и PostgreSQL — одним
    INSERT … VALUES (…), (…) ON CONFLICT DO NOTHING; прочие СУБД — по одному."""
    if not items:
        return
    dialect = session.get_bind().dialect.name
    if dialect not in ("sqlite", "postgresql"):
        for task_id, kind in items:
            await mark_sent(session, task_id, kind)
        return
    insert = sqlite_insert if dialect == "sqlite" else postgresql_insert
    rows = [{"task_id": task_id, "kind": kind} for task_id, kind in dict.fromkeys(items)]
    index = [ReminderLog.task_id, ReminderLog.kind]
    await session.execute(insert(ReminderLog).values(rows).on_conflict_do_nothing(index_elements=index))


async def reset_reminders(session: AsyncSession, task_id: int) -> None:
    """Удалить журнал напоминаний задачи (например, после смены срока)."""
    await session.execute(delete(ReminderLog).where(ReminderLog.task_id == task_id))


def in_quiet_hours(now: datetime | None = None) -> bool:
    """Тихие часы по местному времени; диапазон может переходить через полночь (21 → 8)."""
    settings = get_settings()
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    hour = to_local(now or utcnow()).hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end
