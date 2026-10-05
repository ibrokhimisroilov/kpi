"""Планировщик: напоминания о сроках, просрочках и непроверенных результатах, еженедельная сводка,
ежедневная резервная копия базы руководителям (bot.scheduler.backup), сдачи, оценку которых прервала
остановка бота (recover_stalled_evaluations).

Что и когда напоминать, решает bot.services.reminders; здесь — тексты, получатели, отправка,
пометка «отправлено» (ReminderLog) и запись в журнал задачи.

Два способа запуска заданий по времени:

* polling (свой компьютер, VPS) — APScheduler в процессе бота (setup_scheduler);
* webhook (Render и другие веб-хостинги) — фоновый цикл бота (bot.web.BackgroundLoop) каждые 5 минут,
  а также необязательный внешний «будильник» через /tick, вызывают run_due_jobs: она выполняет всё,
  чему пора, — напоминания, сводку, резервную копию.

Во время перезапуска на хостинге два экземпляра бота работают одновременно, поэтому каждое
действие сначала «занимается» записью в БД (уникальный ключ), а уже потом отправляется:
напоминание — ReminderLog, сводка и резервная копия — JobLog. Второй экземпляр получает
IntegrityError и ничего не отправляет. Не дошло ни одного сообщения из-за сбоя сети — запись
снимается, и при следующем запуске действие повторяется.

Еженедельная сводка не теряется, если компьютер с ботом включили позже её времени (не больше чем
на 6 ч): при запуске она догоняется, а запись в DigestLog не даёт отправить её дважды.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import notify
from bot.ai import evaluate as ai_evaluate
from bot.config import get_settings
from bot.db.base import Base
from bot.db.models import DigestLog, EventType, JobLog, ReminderLog, Submission, Task, TaskStatus, User
from bot.scheduler import backup
from bot.services import kpi, periods, reminders, tasks, users
from bot.services.reminders import Reminder
from bot.ui import keyboards, render
from bot.ui.callbacks import ListCB
from bot.utils.dates import days_between, fmt_deadline, to_local, utcnow
from bot.utils.text import esc, fmt_num, plural, truncate

__all__ = [
    "setup_scheduler",
    "run_reminders",
    "weekly_digest",
    "recover_stalled_evaluations",
    "run_due_jobs",
    "tick_schedule_summary",
]

log = logging.getLogger(__name__)

Sessionmaker = async_sessionmaker[AsyncSession]

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_FIRST_RUN_DELAY_SEC = 30          # первая проверка напоминаний вскоре после запуска
_DIGEST_GRACE_SEC = 6 * 3600       # бот был выключен в момент сводки — отправить, если опоздали не больше чем на 6 ч
_DIGEST_CATCH_UP_DELAY_SEC = 60    # догнать пропущенную сводку через минуту после запуска (бот успеет подключиться)
_BACKUP_GRACE_SEC = 3600           # копия не ушла вовремя (бот был занят/выключен) — догнать в течение часа
_DEFAULT_BACKUP_HOUR = 23
_MSG_LIMIT = 4000
_RESULT_LIMIT = 500                # сколько символов ожидаемого результата показывать в напоминании

# Ключи JobLog для заданий режима webhook (run_due_jobs).
JOB_BACKUP = "backup"
JOB_DIGEST = "digest"
JOB_EVALUATION = "eval"            # сдача с прерванной оценкой передана руководителю (key — id сдачи)

# Сдача без оценки дольше бюджета AI (evaluation_budget_sec) + этот запас — оценка прервана остановкой бота.
_EVAL_STALL_MARGIN_SEC = 5 * 60
# Старше — не трогаем: о таких сдачах руководителю напомнит review_pending (через REVIEW_REMINDER_DAYS).
_EVAL_RECOVERY_WINDOW = timedelta(days=1)


# --- Расписание ---------------------------------------------------------------------------------


def _digest_schedule(weekday: int, hour: int, *, warn: bool = True) -> tuple[int, int]:
    """День недели (0 = пн) и час сводки из настроек; неверные значения — понедельник 9:00 (с предупреждением)."""
    if 0 <= weekday <= 6 and 0 <= hour <= 23:
        return weekday, hour
    if warn:
        log.warning(
            "DIGEST_WEEKDAY=%s / DIGEST_HOUR=%s вне диапазона 0–6 / 0–23 — сводка будет по понедельникам в 9:00",
            weekday,
            hour,
        )
    return 0, 9


def _backup_hour(hour: int, *, warn: bool = True) -> int:
    """Час резервной копии из настроек; неверное значение — 23:00 (с предупреждением), бот не падает."""
    if 0 <= hour <= 23:
        return hour
    if warn:
        log.warning(
            "BACKUP_HOUR=%s вне диапазона 0–23 — резервная копия будет в %s:00", hour, _DEFAULT_BACKUP_HOUR
        )
    return _DEFAULT_BACKUP_HOUR


def _last_digest_slot(now_utc: datetime, weekday: int, hour: int) -> datetime:
    """Последнее (не позже now) время сводки по расписанию — местное время, aware."""
    now = to_local(now_utc)
    slot = (now - timedelta(days=(now.weekday() - weekday) % 7)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    if slot > now:
        slot -= timedelta(days=7)
    return slot


def _missed_digest_run(now_utc: datetime, weekday: int, hour: int) -> datetime | None:
    """Время догнать сводку, пропущенную из-за выключенного бота, или None.

    APScheduler хранит задания в памяти: после запуска он считает следующий запуск от «сейчас»,
    и сводка, время которой прошло (понедельник 9:00, а компьютер включили в 9:30), ушла бы
    только через неделю. Если с её времени прошло не больше _DIGEST_GRACE_SEC — догоняем через
    минуту. Повтор после обычного перезапуска (сводка уже ушла) отсекает DigestLog в weekly_digest.
    """
    now = to_local(now_utc)
    late = (now - _last_digest_slot(now_utc, weekday, hour)).total_seconds()
    if 0 < late <= _DIGEST_GRACE_SEC:
        return now + timedelta(seconds=_DIGEST_CATCH_UP_DELAY_SEC)
    return None


def _digest_due(now_utc: datetime, weekday: int, hour: int) -> bool:
    """Пора отправить сводку: её время по расписанию наступило не больше _DIGEST_GRACE_SEC назад."""
    late = (to_local(now_utc) - _last_digest_slot(now_utc, weekday, hour)).total_seconds()
    return 0 <= late <= _DIGEST_GRACE_SEC


def setup_scheduler(bot: Bot, sessionmaker: Sessionmaker) -> AsyncIOScheduler:
    """Планировщик (ещё не запущен): напоминания и поиск сдач с прерванной оценкой каждые N минут,
    еженедельная сводка руководителям и (BACKUP_ENABLED) ежедневная резервная копия базы в BACKUP_HOUR
    по местному времени."""
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    interval_min = max(1, settings.scheduler_interval_min)
    scheduler.add_job(
        run_reminders,
        IntervalTrigger(minutes=interval_min, timezone=settings.tz),
        args=(bot, sessionmaker),
        id="reminders",
        name="Напоминания о сроках",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=interval_min * 60,
        next_run_time=datetime.now(settings.tz) + timedelta(seconds=_FIRST_RUN_DELAY_SEC),
    )
    scheduler.add_job(
        recover_stalled_evaluations,
        IntervalTrigger(minutes=interval_min, timezone=settings.tz),
        args=(bot, sessionmaker),
        id="stalled_evaluations",
        name="Сдачи с прерванной оценкой",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=interval_min * 60,
        next_run_time=datetime.now(settings.tz) + timedelta(seconds=_FIRST_RUN_DELAY_SEC),
    )
    weekday, hour = _digest_schedule(settings.digest_weekday, settings.digest_hour)
    catch_up = _missed_digest_run(utcnow(), weekday, hour)
    if catch_up is not None:
        log.info("Еженедельная сводка пропущена, пока бот был выключен, — отправлю в %s", catch_up)
    scheduler.add_job(
        weekly_digest,
        CronTrigger(day_of_week=_WEEKDAYS[weekday], hour=hour, minute=0, timezone=settings.tz),
        args=(bot, sessionmaker),
        kwargs={"once": True},
        id="weekly_digest",
        name="Еженедельная сводка",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=_DIGEST_GRACE_SEC,
        # Первый запуск — догнать пропущенную сводку; дальше — по расписанию.
        **({"next_run_time": catch_up} if catch_up is not None else {}),
    )
    if settings.backup_enabled:
        scheduler.add_job(
            backup.send_backup,
            CronTrigger(hour=_backup_hour(settings.backup_hour), minute=0, timezone=settings.tz),
            args=(bot, sessionmaker),
            id="backup",
            name="Резервная копия базы",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=_BACKUP_GRACE_SEC,
        )
    return scheduler


# --- Тексты напоминаний ---------------------------------------------------------------------------


def _duration(days: float, *, whole_days: bool = False) -> str:
    """Длительность по-русски: «3 дня», «5 часов», «меньше часа» (округление до ближайшего).

    whole_days=True — полные дни без округления вверх, как «просрочено на 1 дн.» в карточке задачи
    (1 день 16 часов просрочки — «1 день», а не «2 дня»).
    """
    hours = math.floor(abs(days) * 24 + 0.5)
    if hours >= 24:
        full_days = math.floor(abs(days)) if whole_days else math.floor(hours / 24 + 0.5)
        return plural(max(full_days, 1), "день", "дня", "дней")
    if hours >= 1:
        return plural(hours, "час", "часа", "часов")
    return "меньше часа"


def _clip(text: str, limit: int) -> str:
    """Свернуть пробелы, обрезать и экранировать пользовательский текст."""
    text = " ".join(text.split())
    return esc(text if len(text) <= limit else text[: limit - 1].rstrip() + "…")


def _task_ref(reminder: Reminder) -> str:
    return f"«{esc(reminder.task.title)}»"


def _days_left(reminder: Reminder, now: datetime) -> float:
    if reminder.days_left is not None:
        return reminder.days_left
    return days_between(reminder.task.deadline, now)


def _before_deadline(reminder: Reminder, now: datetime) -> tuple[str, InlineKeyboardMarkup]:
    task = reminder.task
    text = (
        f"⏰ До срока задачи {_task_ref(reminder)} осталось {_duration(_days_left(reminder, now))} "
        f"({fmt_deadline(task.deadline)}).\n\n"
        f"🎯 Ожидаемый результат: {_clip(task.expected_result, _RESULT_LIMIT)}"
    )
    if task.plan_value is not None:
        # Число, по которому потом сравнят план и факт, — как в карточке задачи.
        amount = f"{fmt_num(task.plan_value)} {_clip(task.plan_unit or '', 64)}".strip()
        text += f"\n📊 План: {amount}"
    return text, keyboards.submit_kb(task)


def _deadline_passed(reminder: Reminder, now: datetime) -> tuple[str, InlineKeyboardMarkup]:
    task = reminder.task
    text = (
        f"⌛ Срок задачи {_task_ref(reminder)} истёк ({fmt_deadline(task.deadline)}).\n\n"
        "Сдайте, пожалуйста, результат:\n"
        "• Что фактически сделано?\n"
        "• Какой получен результат?\n"
        "• Какие документы или материалы подтверждают выполнение?"
    )
    return text, keyboards.submit_kb(task)


def _overdue_daily(reminder: Reminder, now: datetime) -> tuple[str, InlineKeyboardMarkup]:
    task = reminder.task
    text = (
        f"⏰ Задача {_task_ref(reminder)} просрочена на {_duration(_days_left(reminder, now), whole_days=True)} "
        f"(срок — {fmt_deadline(task.deadline)}).\n\n"
        "Сдайте результат: что фактически сделано, какой получен результат "
        "и какие документы это подтверждают."
    )
    return text, keyboards.submit_kb(task)


def _overdue_manager(reminder: Reminder, now: datetime) -> tuple[str, InlineKeyboardMarkup]:
    task = reminder.task
    accepted = "принята в работу" if task.accepted_at is not None else "исполнитель не подтвердил получение"
    text = (
        f"⚠️ Просрочена задача #{task.id} {_task_ref(reminder)} ({esc(task.assignee.short_name)})\n"
        f"📅 Срок: {fmt_deadline(task.deadline)} · {accepted}\n"
        "Результат пока не сдан."
    )
    return text, notify.task_button_kb(task, "📋 Открыть", "open")


def _review_pending(reminder: Reminder, now: datetime) -> tuple[str, InlineKeyboardMarkup]:
    task = reminder.task
    submitted = task.submitted_at or now
    waiting = plural(math.floor(max(0.0, days_between(now, submitted))), "день", "дня", "дней")
    text = (
        f"📝 Ждёт проверки {waiting}: задача #{task.id} {_task_ref(reminder)} "
        f"({esc(task.assignee.short_name)})\n"
        f"📤 Сдано: {to_local(submitted):%d.%m %H:%M}"
    )
    return text, notify.task_button_kb(task, "🔍 Проверить", "review")


_Builder = Callable[[Reminder, datetime], tuple[str, InlineKeyboardMarkup]]

_BUILDERS: dict[str, _Builder] = {
    "before_days": _before_deadline,
    "before_hours": _before_deadline,
    "deadline_passed": _deadline_passed,
    "overdue_daily": _overdue_daily,
    "overdue_manager": _overdue_manager,
    "review_pending": _review_pending,
}


# --- Отправка напоминаний -------------------------------------------------------------------------


def _recipients(reminder: Reminder, managers: list[User]) -> list[User]:
    """Руководителю — ответственный (или все активные руководители); сотруднику — если он активен."""
    if reminder.recipient == reminders.MANAGER:
        return notify.responsible_managers(reminder.task, managers)
    assignee = reminder.task.assignee
    return [assignee] if assignee.is_active else []


async def _claim(sessionmaker: Sessionmaker, row: Base) -> bool:
    """Атомарно записать строку-«замок» (уникальный ключ) отдельной транзакцией.

    False — такая запись уже есть: действие выполняет (или выполнил) другой экземпляр бота.
    """
    async with sessionmaker() as session:
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
    return True


async def _unclaim_reminder(sessionmaker: Sessionmaker, reminder: Reminder) -> None:
    """Снять пометку «отправлено»: напоминание повторится при следующем запуске."""
    async with sessionmaker() as session:
        await session.execute(
            delete(ReminderLog).where(ReminderLog.task_id == reminder.task.id, ReminderLog.kind == reminder.kind)
        )
        await session.commit()


async def _deliver(
    bot: Bot, sessionmaker: Sessionmaker, reminder: Reminder, managers: list[User], now: datetime
) -> int:
    """Пометить напоминание отправленным, отправить его и записать в журнал задачи.

    Возвращает число доставленных сообщений.
    Пометка (ReminderLog) ставится ДО отправки: если два экземпляра бота работают одновременно
    (перезапуск на хостинге), второй не сможет её поставить и ничего не отправит.
    Сбой сети или сервера Telegram без единой доставки — пометка снимается, повторим при следующем запуске.
    Бот заблокирован, чат недоступен, получателей нет — пометка остаётся, чтобы не «долбить».
    """
    text, markup = _BUILDERS[reminder.reason](reminder, now)
    if not await _claim(sessionmaker, ReminderLog(task_id=reminder.task.id, kind=reminder.kind)):
        log.info("Напоминание %s по задаче #%s уже отправляет другой экземпляр бота", reminder.kind, reminder.task.id)
        return 0
    try:
        statuses = [
            (await notify.send_text(bot, user.tg_id, text, reply_markup=markup))[1]
            for user in _recipients(reminder, managers)
        ]
    except Exception:
        await _unclaim_reminder(sessionmaker, reminder)
        raise
    delivered = statuses.count(notify.Delivery.SENT)
    if not delivered and notify.Delivery.FAILED in statuses:
        await _unclaim_reminder(sessionmaker, reminder)
        return 0
    if delivered:
        async with sessionmaker() as session:
            await tasks.add_event(session, reminder.task, None, EventType.REMINDER, kind=reminder.kind)
            await session.commit()
    return delivered


async def run_reminders(bot: Bot, sessionmaker: Sessionmaker, now: datetime | None = None) -> int:
    """Отправить все напоминания, которым пора. В тихие часы ничего не делает.

    Каждое напоминание — отдельная транзакция (commit после каждого), ошибка одного не мешает остальным.
    Возвращает число доставленных сообщений.
    """
    now = now or utcnow()
    if reminders.in_quiet_hours(now):
        return 0
    async with sessionmaker() as session:
        due = await reminders.due_reminders(session, now)
        managers = await users.list_managers(session) if due else []
        await session.commit()  # пороги, которые due_reminders пропустил, уже помечены отправленными
    sent = 0
    for reminder in due:
        try:
            sent += await _deliver(bot, sessionmaker, reminder, managers, now)
        except Exception:
            log.exception("Не удалось обработать напоминание %s по задаче #%s", reminder.kind, reminder.task.id)
    if due:
        log.info("Напоминаний к отправке: %s, доставлено сообщений: %s", len(due), sent)
    return sent


# --- Еженедельная сводка ------------------------------------------------------------------------


def _digest_text(
    period: periods.Period, rows: list[tuple[User, kpi.KpiResult]], on_review: int, proposals: int
) -> str:
    lines = ["🗓 <b>Еженедельная сводка</b>"]
    waiting = []
    if on_review:
        waiting.append(f"📝 на проверке — {on_review}")
    if proposals:
        waiting.append(f"📥 предложений — {proposals}")
    if waiting:
        lines.append("Ждут вашего решения: " + " · ".join(waiting))
    lines += ["", render.team_dashboard(period, rows, kpi.team_kpi(rows))]
    return truncate("\n".join(lines), _MSG_LIMIT)


def _digest_kb(
    rows: list[tuple[User, kpi.KpiResult]], period: periods.Period, on_review: int, proposals: int
) -> InlineKeyboardMarkup:
    """Кнопки дашборда (периоды, сотрудники) + переход к тому, что ждёт решения руководителя."""
    waiting: list[InlineKeyboardButton] = []
    if on_review:
        waiting.append(InlineKeyboardButton(
            text=f"📝 На проверке ({on_review})",
            callback_data=ListCB(scope="review", status="review", page=0).pack(),
        ))
    if proposals:
        waiting.append(InlineKeyboardButton(
            text=f"📥 Предложения ({proposals})",
            callback_data=ListCB(scope="proposals", status="all", page=0).pack(),
        ))
    team = keyboards.team_kb(rows, period.kind, period.offset).inline_keyboard
    return InlineKeyboardMarkup(inline_keyboard=[waiting, *team] if waiting else list(team))


async def _digest_sent(session: AsyncSession, period: periods.Period) -> bool:
    return await session.scalar(select(DigestLog.id).where(DigestLog.period_start == period.start)) is not None


async def _mark_digest_sent(sessionmaker: Sessionmaker, period: periods.Period) -> None:
    """Запомнить, что сводка за неделю ушла (повтор после перезапуска бота не нужен)."""
    try:
        async with sessionmaker() as session:
            if not await _digest_sent(session, period):
                session.add(DigestLog(period_start=period.start))
                await session.commit()
    except Exception:  # noqa: BLE001 — сводка уже отправлена, учёт не должен ронять задание
        log.exception("Не удалось записать отправку сводки (%s)", period.label)


async def _send_digest(
    bot: Bot, sessionmaker: Sessionmaker, period: periods.Period, now: datetime
) -> int | None:
    """Разослать сводку за period. None — некому или не о ком (нет активных руководителей или
    сотрудников), иначе — скольким руководителям она дошла."""
    async with sessionmaker() as session:
        rows = await kpi.kpi_for_team(session, period, now)
        managers = await users.list_managers(session)
        on_review = await tasks.count_tasks(session, statuses=[TaskStatus.SUBMITTED])
        proposals = await tasks.count_tasks(session, statuses=[TaskStatus.PROPOSED])
    if not rows or not managers:
        log.info("Еженедельная сводка не отправлена: нет активных сотрудников или руководителей")
        return None
    text = _digest_text(period, rows, on_review, proposals)
    markup = _digest_kb(rows, period, on_review, proposals)
    delivered = 0
    for manager in managers:
        if await notify.safe_send(bot, manager.tg_id, text, reply_markup=markup) is not None:
            delivered += 1
    log.info("Еженедельная сводка (%s) отправлена руководителям: %s из %s", period.label, delivered, len(managers))
    return delivered


async def weekly_digest(
    bot: Bot, sessionmaker: Sessionmaker, now: datetime | None = None, *, once: bool = False
) -> None:
    """Каждому активному руководителю — дашборд команды за прошлую неделю и кнопки периодов/сотрудников.

    once=True (так вызывает планировщик) — не отправлять, если сводка за эту неделю уже ушла:
    например, её догнали после запуска бота, а потом наступило время по расписанию, или бот
    перезапустили сразу после сводки.
    """
    now = now or utcnow()
    period = periods.get_period("week", -1, now)
    if once:
        async with sessionmaker() as session:
            if await _digest_sent(session, period):
                log.info("Еженедельная сводка (%s) уже отправлена — повторно не отправляю", period.label)
                return
    if await _send_digest(bot, sessionmaker, period, now):
        await _mark_digest_sent(sessionmaker, period)


# --- Сдачи с прерванной оценкой ------------------------------------------------------------------------


async def recover_stalled_evaluations(bot: Bot, sessionmaker: Sessionmaker, now: datetime | None = None) -> int:
    """Сдачи, оценку которых прервала остановка бота, — оценить по правилам и передать руководителю.

    Обычно сдачу оценивает и передаёт руководителю сам обработчик «📤 Отправить»
    (bot.handlers.task_submit.on_confirm): сдача сохраняется сразу, потом до evaluation_budget_sec()
    идёт оценка AI (иначе — по правилам), и только после неё — уведомление руководителю. Если бота
    остановили в это время (обновление на хостинге: старый экземпляр через ~1,5 мин прерывает
    недоделанное; сбой, нехватка памяти, выключенный компьютер), сдача осталась без оценки, руководитель
    о ней не знает, а у сотрудника висит «⏳ Анализирую результат…».

    Признак: задача на проверке, сдача без оценки (ai_source пуст) и без решения, сделана больше
    evaluation_budget_sec() + 5 мин назад (живой обработчик к этому времени уже записал бы хотя бы оценку
    по правилам), но не раньше суток назад. Каждая сдача «занимается» записью JobLog(job="eval", key=id) —
    два экземпляра бота её не задвоят. Возвращает, сколько сдач передано руководителю.
    """
    now = now or utcnow()
    stalled_before = now - timedelta(seconds=ai_evaluate.evaluation_budget_sec() + _EVAL_STALL_MARGIN_SEC)
    async with sessionmaker() as session:
        sub_ids = list(
            await session.scalars(
                select(Submission.id)
                .join(Task, Task.id == Submission.task_id)
                .where(
                    Task.status == TaskStatus.SUBMITTED,
                    Submission.ai_source.is_(None),
                    Submission.decision.is_(None),
                    Submission.created_at <= stalled_before,
                    Submission.created_at >= now - _EVAL_RECOVERY_WINDOW,
                )
                .order_by(Submission.id)
            )
        )
    recovered = 0
    for sub_id in sub_ids:
        try:
            if await _recover_evaluation(bot, sessionmaker, sub_id):
                recovered += 1
        except Exception:
            log.exception("Не удалось передать руководителю сдачу #%s с прерванной оценкой", sub_id)
    if recovered:
        log.info("Сдачи с прерванной оценкой (бот был остановлен) переданы руководителю: %s", recovered)
    return recovered


async def _recover_evaluation(bot: Bot, sessionmaker: Sessionmaker, sub_id: int) -> bool:
    """Одна сдача: занять (JobLog), оценить по правилам, уведомить руководителя и сотрудника."""
    key = str(sub_id)
    if not await _claim_job(sessionmaker, JOB_EVALUATION, key):
        return False
    try:
        async with sessionmaker() as session:
            sub = await tasks.get_submission(session, sub_id)
            task = sub.task if sub is not None else None
            last = task.last_submission if task is not None else None
            if (
                sub is None
                or task is None
                or task.status != TaskStatus.SUBMITTED
                or sub.decision is not None
                or sub.ai_source is not None
                or last is None
                or last.id != sub.id
            ):
                return False  # пока искали — оценили, проверили или отменили
            score, explanation = ai_evaluate.rules_score(task.plan_value, sub.fact_value, float(sub.late_days or 0.0))
            await tasks.record_evaluation(
                session, sub.id, score=score, rationale=ai_evaluate.RULES_PREFIX + explanation, source="rules"
            )
            await session.commit()
    except Exception:
        await _release_job(sessionmaker, JOB_EVALUATION, key)  # повторим при следующем запуске
        raise
    log.warning(
        "Оценка сдачи #%s по задаче #%s была прервана остановкой бота — оценено по правилам, сдача передана "
        "руководителю",
        sub.id,
        task.id,
    )
    async with sessionmaker() as session:
        await notify.notify_submission(bot, session, task, sub)
    if task.assignee.is_active:
        await notify.safe_send(
            bot,
            task.assignee.tg_id,
            f"✅ Результат по задаче #{task.id} «{esc(task.title)}» передан руководителю на проверку. "
            "Решение придёт сюда.",
            reply_markup=notify.task_button_kb(task, "📋 Открыть", "open"),
        )
    return True


# --- Задания режима webhook (фоновый цикл бота и /tick) --------------------------------------------


async def _job_logged(sessionmaker: Sessionmaker, job: str, key: str) -> bool:
    async with sessionmaker() as session:
        found = await session.scalar(select(JobLog.id).where(JobLog.job == job, JobLog.key == key))
    return found is not None


async def _claim_job(sessionmaker: Sessionmaker, job: str, key: str) -> bool:
    """Занять разовое задание (job, key). False — уже выполнено или выполняется другим экземпляром бота.

    Сначала дешёвая проверка (каждый «тик» после выполнения задания — без попытки INSERT и ошибки
    уникальности в логах сервера БД), затем атомарная вставка: из двух одновременных вставок
    проходит одна.
    """
    if await _job_logged(sessionmaker, job, key):
        return False
    return await _claim(sessionmaker, JobLog(job=job, key=key))


async def _release_job(sessionmaker: Sessionmaker, job: str, key: str) -> None:
    async with sessionmaker() as session:
        await session.execute(delete(JobLog).where(JobLog.job == job, JobLog.key == key))
        await session.commit()


async def _digest_if_due(bot: Bot, sessionmaker: Sessionmaker, now: datetime) -> str:
    """Еженедельная сводка, если её время по расписанию наступило не больше 6 ч назад и она ещё не ушла.

    "not_due" — не время; "done" — уже отправлена (DigestLog) или занята другим экземпляром бота;
    "sent" — отправлена; "nobody" — некому/не о ком (повторять в эту неделю не будем);
    "failed" — никому не дошла (сбой сети, бот заблокирован) — повторим при следующем «тике».
    """
    settings = get_settings()
    weekday, hour = _digest_schedule(settings.digest_weekday, settings.digest_hour, warn=False)
    if not _digest_due(now, weekday, hour):
        return "not_due"
    period = periods.get_period("week", -1, now)
    key = f"{period.start:%Y-%m-%d}"
    async with sessionmaker() as session:
        if await _digest_sent(session, period):
            return "done"
    if not await _claim_job(sessionmaker, JOB_DIGEST, key):
        return "done"
    try:
        delivered = await _send_digest(bot, sessionmaker, period, now)
    except Exception:
        await _release_job(sessionmaker, JOB_DIGEST, key)
        raise
    if delivered is None:
        return "nobody"
    if not delivered:
        await _release_job(sessionmaker, JOB_DIGEST, key)
        return "failed"
    await _mark_digest_sent(sessionmaker, period)
    return "sent"


async def _backup_if_due(bot: Bot, sessionmaker: Sessionmaker, now: datetime) -> str:
    """Резервная копия: раз в местные сутки, начиная с BACKUP_HOUR:00.

    Задание занимается записью JobLog(job="backup", key=местная дата) ДО отправки — второй
    экземпляр бота копию не отправит. Повтора при неудаче нет (как и в режиме polling): следующая
    копия — завтра. "disabled" | "not_due" | "done" | "sent:<скольким руководителям доставлено>".
    """
    settings = get_settings()
    if not settings.backup_enabled:
        return "disabled"
    local = to_local(now)
    if local.hour < _backup_hour(settings.backup_hour, warn=False):
        return "not_due"
    if not await _claim_job(sessionmaker, JOB_BACKUP, f"{local:%Y-%m-%d}"):
        return "done"
    delivered = await backup.send_backup(bot, sessionmaker, now)
    return f"sent:{delivered}"


_TickStep = Callable[[Bot, Sessionmaker, datetime], Awaitable[object]]


async def run_due_jobs(bot: Bot, sessionmaker: Sessionmaker, now: datetime | None = None) -> dict[str, object]:
    """Выполнить всё, чему пора (режим webhook: фоновый цикл бота каждые 5 минут и /tick).

    * сдачи с прерванной оценкой — recover_stalled_evaluations (JobLog не даёт передать дважды);
    * напоминания — run_reminders (в тихие часы ничего; ReminderLog не даёт отправить дважды);
    * еженедельная сводка — если её время наступило не больше 6 ч назад и она ещё не ушла;
    * резервная копия — раз в местные сутки после BACKUP_HOUR (если BACKUP_ENABLED).

    Можно вызывать сколько угодно раз и из двух экземпляров бота одновременно (при деплое старый
    и новый экземпляры работают вместе ~1–1,5 мин, у каждого свой фоновый цикл): каждое действие
    выполняется один раз. Ошибка одного задания не мешает остальным. Возвращает итог по заданиям,
    например {"evaluations": 0, "reminders": 2, "digest": "not_due", "backup": "done"}
    ("error" — задание упало).
    """
    now = now or utcnow()
    steps: tuple[tuple[str, _TickStep], ...] = (
        ("evaluations", recover_stalled_evaluations),
        ("reminders", run_reminders),
        ("digest", _digest_if_due),
        ("backup", _backup_if_due),
    )
    result: dict[str, object] = {}
    for name, step in steps:
        try:
            result[name] = await step(bot, sessionmaker, now)
        except Exception:
            log.exception("Задание «%s» по расписанию не выполнено", name)
            result[name] = "error"
    log.debug("Задания по расписанию: %s", result)
    return result


def tick_schedule_summary(interval_sec: float | None = None) -> str:
    """Расписание заданий режима webhook — для лога при запуске (с предупреждениями о неверных настройках).

    interval_sec — как часто бот сам запускает run_due_jobs (bot.web.BackgroundLoop); None — не указывать.
    """
    settings = get_settings()
    weekday, hour = _digest_schedule(settings.digest_weekday, settings.digest_hour)
    days = ("понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям")
    if interval_sec is None:
        when = "при каждом запуске заданий"
    elif interval_sec >= 60 and interval_sec % 60 == 0:
        when = f"каждые {int(interval_sec // 60)} мин"
    else:
        when = f"каждые {interval_sec:g} с"
    parts = [
        f"напоминания — {when} (кроме тихих часов "
        f"{settings.quiet_hours_start}:00–{settings.quiet_hours_end}:00)",
        f"сводка — по {days[weekday]} с {hour}:00 (догоняется в течение 6 ч)",
    ]
    if settings.backup_enabled:
        parts.append(f"резервная копия — раз в день после {_backup_hour(settings.backup_hour)}:00")
    else:
        parts.append("резервная копия выключена (BACKUP_ENABLED=false)")
    return f"{'; '.join(parts)} (время — {settings.timezone})"
