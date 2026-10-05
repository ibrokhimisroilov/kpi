"""Коэффициент эффективности (KPI) сотрудников.

Методика: KPI = Σ(вес × оценка) / Σ(вес) по задачам периода, которые уже можно оценить:
* DONE — с окончательной оценкой руководителя;
* просроченные несданные (ACTIVE/REWORK, срок истёк) — как 0 %, если так настроено.
SUBMITTED (на проверке) и не просроченные ACTIVE/REWORK (в работе) в KPI пока не входят.
Месяц/квартал/год считаются той же формулой по всем задачам периода, а не как среднее недель.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db.models import (
    EXCLUDED_FROM_KPI,
    OPEN_STATUSES,
    Role,
    Task,
    TaskSource,
    TaskStatus,
    User,
    UserStatus,
)
from bot.services.periods import Period
from bot.utils.dates import utcnow


@dataclass
class TaskSnapshot:
    """Данные задачи, нужные расчёту, — без ORM, чтобы KPI считался чистой функцией."""

    task_id: int
    title: str
    weight: int
    status: TaskStatus
    deadline: datetime
    final_score: float | None
    source: TaskSource
    last_late: bool | None  # is_late последней сдачи (None — сдач не было)

    @classmethod
    def from_task(cls, task: Task) -> TaskSnapshot:
        last = task.last_submission
        return cls(
            task_id=task.id,
            title=task.title,
            weight=task.weight,
            status=task.status,
            deadline=task.deadline,
            final_score=task.final_score,
            source=task.source,
            last_late=last.is_late if last is not None else None,
        )


@dataclass
class KpiItem:
    """Задача, вошедшая в расчёт KPI."""

    task_id: int
    title: str
    weight: int
    score: float
    zero_overdue: bool  # просрочена и не сдана — учтена как 0 %


@dataclass
class KpiResult:
    kpi: float | None = None           # взвешенный коэффициент, %
    items: list[KpiItem] = field(default_factory=list)  # что вошло в расчёт
    total: int = 0                     # задач в периоде
    done: int = 0                      # DONE
    done_on_time: int = 0              # DONE, последняя сдача не просрочена
    done_late: int = 0                 # DONE, сдано с опозданием
    overdue_open: int = 0              # просрочены и не сданы (в т.ч. REWORK)
    on_review: int = 0                 # SUBMITTED
    on_review_late: int = 0            # SUBMITTED, сдано с опозданием
    in_progress: int = 0               # ACTIVE/REWORK, срок не истёк
    overperformed: int = 0             # DONE с оценкой > 100
    self_initiated: int = 0            # source=EMPLOYEE (внесены сотрудником)
    avg_score: float | None = None     # простое среднее оценок DONE

    @property
    def overdue_total(self) -> int:
        """«Просрочено»: не сданные в срок + сданные с опозданием."""
        return self.overdue_open + self.done_late + self.on_review_late

    @property
    def on_time_pct(self) -> float | None:
        """«Выполнение в срок», % от выполненных задач."""
        if not self.done:
            return None
        return round(self.done_on_time / self.done * 100, 2)


def _weighted(items: Sequence[KpiItem]) -> float | None:
    """Σ(вес × оценка) / Σ(вес); None, если оценивать нечего."""
    total_weight = sum(item.weight for item in items)
    if total_weight <= 0:
        return None
    return round(sum(item.weight * item.score for item in items) / total_weight, 2)


def compute_kpi(
    snapshots: Sequence[TaskSnapshot], now: datetime, overdue_as_zero: bool = True
) -> KpiResult:
    """Чистый расчёт KPI по задачам периода (now — naive UTC)."""
    res = KpiResult()
    scores: list[float] = []
    for snap in snapshots:
        if snap.status in EXCLUDED_FROM_KPI:
            continue
        res.total += 1
        if snap.source == TaskSource.EMPLOYEE:
            res.self_initiated += 1

        if snap.status == TaskStatus.DONE:
            res.done += 1
            if snap.last_late:
                res.done_late += 1
            else:
                res.done_on_time += 1
            if snap.final_score is not None:
                scores.append(snap.final_score)
                res.items.append(KpiItem(snap.task_id, snap.title, snap.weight, snap.final_score, False))
                if snap.final_score > 100:
                    res.overperformed += 1
        elif snap.status == TaskStatus.SUBMITTED:
            res.on_review += 1
            if snap.last_late:
                res.on_review_late += 1
        elif snap.status in OPEN_STATUSES:
            if snap.deadline < now:
                res.overdue_open += 1
                if overdue_as_zero:
                    res.items.append(KpiItem(snap.task_id, snap.title, snap.weight, 0.0, True))
            else:
                res.in_progress += 1

    res.kpi = _weighted(res.items)
    res.avg_score = round(sum(scores) / len(scores), 2) if scores else None
    return res


async def period_tasks(
    session: AsyncSession, period: Period, *, assignee_ids: Sequence[int] | None = None
) -> list[Task]:
    """Задачи периода: срок в [start, end), кроме PROPOSED/REJECTED/CANCELLED.

    assignee_ids — ограничить исполнителями (None — все). Сортировка по сроку.
    """
    stmt = select(Task).where(
        Task.deadline >= period.start,
        Task.deadline < period.end,
        Task.status.not_in(EXCLUDED_FROM_KPI),
    )
    if assignee_ids is not None:
        stmt = stmt.where(Task.assignee_id.in_(assignee_ids))
    stmt = stmt.order_by(Task.deadline, Task.id)
    return list((await session.scalars(stmt)).all())


def _compute_for_tasks(tasks: Sequence[Task], now: datetime) -> KpiResult:
    snapshots = [TaskSnapshot.from_task(task) for task in tasks]
    return compute_kpi(snapshots, now, get_settings().overdue_counts_as_zero)


async def kpi_for_user(
    session: AsyncSession, user_id: int, period: Period, now: datetime | None = None
) -> KpiResult:
    """KPI одного сотрудника за период."""
    tasks = await period_tasks(session, period, assignee_ids=[user_id])
    return _compute_for_tasks(tasks, now or utcnow())


async def kpi_for_team(
    session: AsyncSession, period: Period, now: datetime | None = None
) -> list[tuple[User, KpiResult]]:
    """KPI всех активных сотрудников (даже без задач).

    Сортировка: KPI по убыванию, «нет данных» — в конце, затем по ФИО.
    """
    now = now or utcnow()
    employees = list(
        (
            await session.scalars(
                select(User)
                .where(User.status == UserStatus.ACTIVE, User.role == Role.EMPLOYEE)
                .order_by(User.full_name, User.id)
            )
        ).all()
    )
    by_assignee: dict[int, list[Task]] = defaultdict(list)
    for task in await period_tasks(session, period, assignee_ids=[u.id for u in employees]):
        by_assignee[task.assignee_id].append(task)

    rows = [(user, _compute_for_tasks(by_assignee[user.id], now)) for user in employees]
    rows.sort(key=lambda row: (row[1].kpi is None, -(row[1].kpi or 0.0), row[0].full_name))
    return rows


def team_kpi(rows: list[tuple[User, KpiResult]]) -> float | None:
    """KPI команды: та же взвешенная формула по всем вошедшим задачам всех сотрудников."""
    return _weighted([item for _, res in rows for item in res.items])
