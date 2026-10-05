"""bot.services.reminders: какие напоминания пора отправить (SPEC 3.5)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from bot.db.models import ReminderLog, TaskStatus, User
from bot.services.reminders import Reminder, due_reminders, in_quiet_hours, mark_sent, reset_reminders
from bot.utils.dates import to_local, to_utc


def local(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Местное время 2026 года -> naive UTC."""
    return to_utc(datetime(2026, month, day, hour, minute))


async def logged_kinds(session, task_id: int) -> set[str]:
    rows = await session.scalars(select(ReminderLog.kind).where(ReminderLog.task_id == task_id))
    return set(rows)


async def simulate(session, start: datetime, end: datetime, step_min: int = 15) -> list[tuple[str, str, str, str]]:
    """Как планировщик: каждые step_min минут (кроме тихих часов) due_reminders -> mark_sent.

    Возвращает [(местное время «dd.mm HH:MM», kind, recipient, reason)].
    """
    sent: list[tuple[str, str, str, str]] = []
    now = start
    while now <= end:
        if not in_quiet_hours(now):
            for reminder in await due_reminders(session, now):
                await mark_sent(session, reminder.task.id, reminder.kind)
                stamp = to_local(now).strftime("%d.%m %H:%M")
                sent.append((stamp, reminder.kind, reminder.recipient, reminder.reason))
        now += timedelta(minutes=step_min)
    return sent


# --- Полный сценарий ---------------------------------------------------------------------------------


async def test_timeline_for_one_task(session, make_task, employee: User) -> None:
    """Срок — пятница 09.10 18:00. Напоминания: за 3 дня, за 1 день, за 3 часа, срок истёк, ежедневно."""
    task = await make_task(employee, deadline=local(10, 9, 18), accepted=False)  # не принята — всё равно
    sent = await simulate(session, local(10, 5, 9), local(10, 11, 12))
    assert sent == [
        ("06.10 18:00", "before_3d", "employee", "before_days"),
        ("08.10 18:00", "before_1d", "employee", "before_days"),
        ("09.10 15:00", "before_hours", "employee", "before_hours"),
        ("09.10 18:15", "deadline_passed", "employee", "deadline_passed"),
        ("09.10 18:15", "overdue_manager", "manager", "overdue_manager"),
        ("10.10 10:00", "overdue_2026-10-10", "employee", "overdue_daily"),
        ("11.10 10:00", "overdue_2026-10-11", "employee", "overdue_daily"),
    ]
    # Ежедневное за день срока не отправлялось, но помечено, чтобы не прийти позже.
    assert "overdue_2026-10-09" in await logged_kinds(session, task.id)


async def test_deadline_in_quiet_hours(session, make_task, employee: User) -> None:
    """Срок 22:00 (тихие часы): «срок истёк» — в 08:00, ежедневное — только на следующий день."""
    await make_task(employee, deadline=local(10, 9, 22))
    sent = await simulate(session, local(10, 9, 20), local(10, 11, 12))
    assert [(stamp, kind) for stamp, kind, _, _ in sent] == [
        ("09.10 20:00", "before_hours"),
        ("10.10 08:00", "deadline_passed"),
        ("10.10 08:00", "overdue_manager"),
        ("11.10 10:00", "overdue_2026-10-11"),
    ]


# --- Правило «наименьший порог» ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours_left", "expected", "marked"),
    [
        (2, "before_hours", {"before_1d", "before_3d"}),
        (20, "before_1d", {"before_3d"}),
        (48, "before_3d", set()),
    ],
)
async def test_only_smallest_threshold_is_sent(
    session, make_task, employee: User, hours_left: int, expected: str, marked: set[str]
) -> None:
    now = local(10, 5, 12)
    task = await make_task(employee, deadline=now + timedelta(hours=hours_left))
    reminders = await due_reminders(session, now)
    assert [r.kind for r in reminders] == [expected]
    reminder = reminders[0]
    assert reminder.task is task and reminder.recipient == "employee"
    assert reminder.days_left == pytest.approx(hours_left / 24)
    assert await logged_kinds(session, task.id) == marked  # пропущенные пороги помечены сами

    await mark_sent(session, task.id, expected)
    assert await due_reminders(session, now) == []


async def test_far_deadline_has_no_reminders(session, make_task, employee: User) -> None:
    now = local(10, 5, 12)
    await make_task(employee, deadline=now + timedelta(days=10))
    assert await due_reminders(session, now) == []


async def test_custom_thresholds(session, set_env, make_task, employee: User) -> None:
    set_env(REMINDER_DAYS_BEFORE="5,2", REMINDER_HOURS_BEFORE="0")
    now = local(10, 5, 12)
    task = await make_task(employee, deadline=now + timedelta(days=4))
    assert [r.kind for r in await due_reminders(session, now)] == ["before_5d"]
    assert [r.kind for r in await due_reminders(session, now + timedelta(days=2, hours=1))] == ["before_2d"]
    assert "before_hours" not in await logged_kinds(session, task.id)


# --- Просрочка ----------------------------------------------------------------------------------------


async def test_overdue_reminders(session, make_task, employee: User) -> None:
    now = local(10, 5, 12)
    task = await make_task(employee, deadline=now - timedelta(days=2))
    reminders = await due_reminders(session, now)
    by_kind = {r.kind: r for r in reminders}
    assert set(by_kind) == {"deadline_passed", "overdue_manager"}
    assert by_kind["deadline_passed"].recipient == "employee"
    assert by_kind["overdue_manager"].recipient == "manager"
    assert by_kind["deadline_passed"].days_left == pytest.approx(-2)
    for reminder in reminders:
        await mark_sent(session, task.id, reminder.kind)

    assert await due_reminders(session, now + timedelta(hours=1)) == []  # тот же день — ничего
    next_morning = local(10, 6, 9, 59)
    assert await due_reminders(session, next_morning) == []  # до overdue_reminder_hour (10:00)
    daily = await due_reminders(session, local(10, 6, 10))
    assert [(r.kind, r.reason, r.recipient) for r in daily] == [("overdue_2026-10-06", "overdue_daily", "employee")]


async def test_rework_task_gets_reminders(session, make_task, employee: User) -> None:
    now = local(10, 5, 12)
    await make_task(employee, deadline=now - timedelta(hours=1), status=TaskStatus.REWORK)
    assert {r.kind for r in await due_reminders(session, now)} == {"deadline_passed", "overdue_manager"}


@pytest.mark.parametrize(
    "status", [TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.PROPOSED, TaskStatus.REJECTED]
)
async def test_closed_tasks_get_nothing(session, make_task, employee: User, status: TaskStatus) -> None:
    now = local(10, 5, 12)
    await make_task(employee, deadline=now - timedelta(days=1), status=status, final_score=100)
    await make_task(employee, deadline=now + timedelta(hours=1), status=status, final_score=100)
    assert await due_reminders(session, now) == []


# --- Непроверенный результат ---------------------------------------------------------------------------


async def test_review_pending_reminder(session, make_task, employee: User) -> None:
    now = local(10, 5, 12)
    waiting = await make_task(
        employee, deadline=now - timedelta(days=4), status=TaskStatus.SUBMITTED,
        submitted_at=now - timedelta(days=3),
    )
    await make_task(  # ждёт всего сутки — рано напоминать
        employee, deadline=now + timedelta(days=4), status=TaskStatus.SUBMITTED,
        submitted_at=now - timedelta(days=1),
    )
    reminders = await due_reminders(session, now)
    assert len(reminders) == 1
    reminder = reminders[0]
    assert isinstance(reminder, Reminder)
    assert (reminder.task, reminder.kind, reminder.recipient, reminder.reason) == (
        waiting, "review_2026-10-05", "manager", "review_pending"
    )
    assert reminder.days_left is None

    await mark_sent(session, waiting.id, reminder.kind)
    assert await due_reminders(session, now + timedelta(hours=3)) == []  # раз в день
    tomorrow = await due_reminders(session, now + timedelta(days=1))
    assert [r.kind for r in tomorrow] == ["review_2026-10-06"]


# --- Идемпотентность, сброс, тихие часы -----------------------------------------------------------------


async def test_mark_sent_is_idempotent(session, make_task, employee: User) -> None:
    task = await make_task(employee, deadline=local(10, 9, 18))
    await mark_sent(session, task.id, "before_1d")
    await mark_sent(session, task.id, "before_1d")
    rows = (await session.scalars(select(ReminderLog).where(ReminderLog.task_id == task.id))).all()
    assert [row.kind for row in rows] == ["before_1d"]


async def test_mark_sent_does_not_commit(sessionmaker, make_task, session, employee: User) -> None:
    task = await make_task(employee, deadline=local(10, 9, 18))
    await session.commit()
    async with sessionmaker() as other:
        await mark_sent(other, task.id, "before_3d")
        await other.rollback()
    assert await logged_kinds(session, task.id) == set()


async def test_reset_reminders(session, make_task, employee: User) -> None:
    now = local(10, 5, 12)
    task = await make_task(employee, deadline=now - timedelta(hours=2))
    for reminder in await due_reminders(session, now):
        await mark_sent(session, task.id, reminder.kind)
    assert await due_reminders(session, now) == []
    await reset_reminders(session, task.id)
    assert await logged_kinds(session, task.id) == set()
    assert {r.kind for r in await due_reminders(session, now)} == {"deadline_passed", "overdue_manager"}


@pytest.mark.parametrize(
    ("hour", "minute", "quiet"),
    [
        (21, 0, True),
        (23, 59, True),
        (0, 0, True),
        (3, 0, True),
        (7, 59, True),
        (8, 0, False),
        (12, 0, False),
        (20, 59, False),
    ],
)
def test_quiet_hours_cross_midnight(hour: int, minute: int, quiet: bool) -> None:
    assert in_quiet_hours(local(10, 5, hour, minute)) is quiet


def test_quiet_hours_same_day_range(set_env) -> None:
    set_env(QUIET_HOURS_START="13", QUIET_HOURS_END="15")
    assert in_quiet_hours(local(10, 5, 13)) is True
    assert in_quiet_hours(local(10, 5, 14, 59)) is True
    assert in_quiet_hours(local(10, 5, 15)) is False
    assert in_quiet_hours(local(10, 5, 23)) is False


def test_quiet_hours_disabled(set_env) -> None:
    set_env(QUIET_HOURS_START="0", QUIET_HOURS_END="0")
    assert not any(in_quiet_hours(local(10, 5, hour)) for hour in range(24))


def test_quiet_hours_default_now() -> None:
    assert isinstance(in_quiet_hours(), bool)
