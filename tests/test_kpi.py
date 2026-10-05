"""bot.services.kpi: методика коэффициента эффективности (SPEC 3.4)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bot.db.models import TaskSource, TaskStatus, User, UserStatus
from bot.services.kpi import (
    KpiResult,
    TaskSnapshot,
    compute_kpi,
    kpi_for_team,
    kpi_for_user,
    team_kpi,
)
from bot.services.periods import get_period
from bot.utils.text import fmt_pct

NOW = datetime(2026, 10, 2, 7, 0)  # пятница 02.10.2026 12:00 по Ташкенту
PAST = NOW - timedelta(days=1)
FUTURE = NOW + timedelta(days=1)

_ids = iter(range(1, 10_000))


def snap(
    status: TaskStatus = TaskStatus.DONE,
    *,
    weight: int = 10,
    score: float | None = None,
    deadline: datetime = PAST,
    late: bool | None = None,
    source: TaskSource = TaskSource.MANAGER,
) -> TaskSnapshot:
    task_id = next(_ids)
    return TaskSnapshot(
        task_id=task_id,
        title=f"Задача {task_id}",
        weight=weight,
        status=status,
        deadline=deadline,
        final_score=score,
        source=source,
        last_late=late if late is not None else (False if status in (TaskStatus.DONE, TaskStatus.SUBMITTED) else None),
    )


# --- Чистая функция compute_kpi ----------------------------------------------------------------------


def test_tz_example_weighted_kpi() -> None:
    """Пример из ТЗ: веса 30/20/20/30 × оценки 100/110/90/105 -> 101.5 -> «102 %»."""
    snapshots = [
        snap(weight=30, score=100),
        snap(weight=20, score=110),
        snap(weight=20, score=90),
        snap(weight=30, score=105),
    ]
    res = compute_kpi(snapshots, NOW)
    assert res.kpi == pytest.approx(101.5)
    assert fmt_pct(res.kpi) == "102 %"
    assert res.total == res.done == 4
    assert res.done_on_time == 4 and res.done_late == 0
    assert res.overperformed == 2  # 110 и 105
    assert res.avg_score == pytest.approx(101.25)
    assert res.on_time_pct == pytest.approx(100.0)
    assert [item.weight for item in res.items] == [30, 20, 20, 30]
    assert not any(item.zero_overdue for item in res.items)


def test_weights_need_not_sum_to_100() -> None:
    res = compute_kpi([snap(weight=10, score=100), snap(weight=10, score=50)], NOW)
    assert res.kpi == pytest.approx(75.0)


def test_overdue_open_counts_as_zero_when_enabled() -> None:
    snapshots = [
        snap(weight=20, score=100),
        snap(TaskStatus.ACTIVE, weight=30, deadline=PAST),
    ]
    res = compute_kpi(snapshots, NOW, overdue_as_zero=True)
    assert res.kpi == pytest.approx(40.0)  # 20×100 / (20+30)
    assert res.overdue_open == 1
    zero = [item for item in res.items if item.zero_overdue]
    assert len(zero) == 1 and zero[0].score == 0 and zero[0].weight == 30


def test_overdue_open_ignored_when_disabled() -> None:
    snapshots = [
        snap(weight=20, score=100),
        snap(TaskStatus.ACTIVE, weight=30, deadline=PAST),
    ]
    res = compute_kpi(snapshots, NOW, overdue_as_zero=False)
    assert res.kpi == pytest.approx(100.0)
    assert res.overdue_open == 1  # счётчик просрочки остаётся
    assert len(res.items) == 1


def test_overdue_rework_counts_as_zero() -> None:
    res = compute_kpi([snap(TaskStatus.REWORK, weight=10, deadline=PAST)], NOW)
    assert res.kpi == 0
    assert res.overdue_open == 1


def test_only_overdue_tasks_give_zero_not_none() -> None:
    res = compute_kpi([snap(TaskStatus.ACTIVE, deadline=PAST)], NOW)
    assert res.kpi == 0.0


def test_submitted_and_in_progress_are_excluded() -> None:
    snapshots = [
        snap(weight=20, score=100),
        snap(TaskStatus.SUBMITTED, weight=50, deadline=PAST),  # на проверке, даже после срока — не 0
        snap(TaskStatus.ACTIVE, weight=50, deadline=FUTURE),   # в работе, срок не истёк
        snap(TaskStatus.REWORK, weight=50, deadline=FUTURE),
    ]
    res = compute_kpi(snapshots, NOW)
    assert res.kpi == pytest.approx(100.0)
    assert res.on_review == 1
    assert res.in_progress == 2
    assert res.overdue_open == 0
    assert res.total == 4
    assert [item.weight for item in res.items] == [20]


def test_excluded_statuses_not_counted_at_all() -> None:
    snapshots = [
        snap(TaskStatus.PROPOSED, deadline=PAST),
        snap(TaskStatus.REJECTED, deadline=PAST),
        snap(TaskStatus.CANCELLED, deadline=PAST),
    ]
    res = compute_kpi(snapshots, NOW)
    assert res.total == 0
    assert res.kpi is None
    assert res.items == []


def test_no_tasks_gives_none() -> None:
    res = compute_kpi([], NOW)
    assert res.kpi is None
    assert res.avg_score is None
    assert res.on_time_pct is None
    assert res.overdue_total == 0


def test_counters() -> None:
    snapshots = [
        snap(weight=10, score=120, late=False),                          # в срок, перевыполнено
        snap(weight=10, score=100, late=False, source=TaskSource.EMPLOYEE),
        snap(weight=10, score=90, late=True),                            # сдано с опозданием
        snap(weight=10, score=101, late=True, source=TaskSource.EMPLOYEE),
        snap(TaskStatus.SUBMITTED, late=True),                           # на проверке, сдано поздно
        snap(TaskStatus.SUBMITTED, late=False, deadline=FUTURE),
        snap(TaskStatus.ACTIVE, deadline=PAST),                          # просрочена, не сдана
        snap(TaskStatus.ACTIVE, deadline=FUTURE, source=TaskSource.EMPLOYEE),
        snap(TaskStatus.CANCELLED, source=TaskSource.EMPLOYEE),          # не считается нигде
    ]
    res = compute_kpi(snapshots, NOW)
    assert res.total == 8
    assert res.done == 4
    assert res.done_on_time == 2
    assert res.done_late == 2
    assert res.on_review == 2
    assert res.on_review_late == 1
    assert res.overdue_open == 1
    assert res.in_progress == 1
    assert res.overperformed == 2  # 120 и 101
    assert res.self_initiated == 3
    assert res.overdue_total == 1 + 2 + 1
    assert res.on_time_pct == pytest.approx(50.0)
    assert res.avg_score == pytest.approx((120 + 100 + 90 + 101) / 4)
    # KPI: 4 DONE по 10 + просроченная как 0 (вес 10).
    assert res.kpi == pytest.approx((120 + 100 + 90 + 101) * 10 / 50)


def test_deadline_equal_to_now_is_not_overdue() -> None:
    res = compute_kpi([snap(TaskStatus.ACTIVE, deadline=NOW)], NOW)
    assert res.overdue_open == 0
    assert res.in_progress == 1


def test_kpi_result_defaults() -> None:
    res = KpiResult()
    assert res.kpi is None and res.items == [] and res.total == 0


# --- Запросы к БД ------------------------------------------------------------------------------------


async def test_snapshot_from_task_uses_last_submission(make_task, employee: User) -> None:
    task = await make_task(employee, deadline=PAST, status=TaskStatus.DONE, final_score=95, late=True, weight=25)
    snapshot = TaskSnapshot.from_task(task)
    assert snapshot.task_id == task.id
    assert snapshot.weight == 25
    assert snapshot.final_score == 95
    assert snapshot.last_late is True
    open_task = await make_task(employee, deadline=FUTURE)
    assert TaskSnapshot.from_task(open_task).last_late is None


async def test_kpi_for_user_tz_example(session, make_task, employee: User) -> None:
    week = get_period("week", 0, NOW)
    for weight, score in ((30, 100), (20, 110), (20, 90), (30, 105)):
        await make_task(employee, deadline=PAST, status=TaskStatus.DONE, weight=weight, final_score=score)
    res = await kpi_for_user(session, employee.id, week, NOW)
    assert res.kpi == pytest.approx(101.5)
    assert fmt_pct(res.kpi) == "102 %"


async def test_kpi_for_user_filters_by_deadline_in_period(
    session, make_task, employee: User, employee2: User
) -> None:
    week = get_period("week", 0, NOW)
    done = TaskStatus.DONE
    await make_task(employee, deadline=week.start, status=done, final_score=100)  # граница: входит
    await make_task(employee, deadline=week.end - timedelta(minutes=1), status=done, final_score=50)
    await make_task(employee, deadline=week.end, status=done, final_score=10)  # граница: не входит
    await make_task(employee, deadline=week.start - timedelta(days=2), status=done, final_score=0)
    await make_task(employee, deadline=PAST, status=TaskStatus.CANCELLED)
    await make_task(employee, deadline=PAST, status=TaskStatus.PROPOSED)
    await make_task(employee2, deadline=PAST, status=TaskStatus.DONE, final_score=0)  # чужая задача

    res = await kpi_for_user(session, employee.id, week, NOW)
    assert res.total == 2
    assert res.kpi == pytest.approx(75.0)

    previous = await kpi_for_user(session, employee.id, get_period("week", -1, NOW), NOW)
    assert previous.total == 1 and previous.kpi == 0.0

    month = await kpi_for_user(session, employee.id, get_period("month", 0, NOW), NOW)
    # Октябрь: в периоде только задачи со сроком с 01.10 (в т.ч. на следующей неделе).
    assert month.total == 2
    assert month.kpi == pytest.approx(30.0)  # (50 + 10) / 2


async def test_kpi_for_user_overdue_setting(session, set_env, make_task, employee: User) -> None:
    week = get_period("week", 0, NOW)
    await make_task(employee, deadline=PAST, status=TaskStatus.DONE, final_score=100, weight=20)
    await make_task(employee, deadline=PAST, status=TaskStatus.ACTIVE, weight=30)
    assert (await kpi_for_user(session, employee.id, week, NOW)).kpi == pytest.approx(40.0)
    set_env(OVERDUE_COUNTS_AS_ZERO="false")
    assert (await kpi_for_user(session, employee.id, week, NOW)).kpi == pytest.approx(100.0)


async def test_kpi_for_team_order_and_inclusion(
    session, make_task, user_factory, manager: User, employee: User, employee2: User
) -> None:
    week = get_period("week", 0, NOW)
    no_tasks = await user_factory(2003, "Алексеев Алексей")         # без задач — в конце
    tied = await user_factory(2004, "Борисов Борис")                 # та же оценка, что у employee2
    blocked = await user_factory(2005, "Заблокированный", status=UserStatus.BLOCKED)
    for weight, score in ((30, 100), (20, 110), (20, 90), (30, 105)):
        await make_task(employee, deadline=PAST, status=TaskStatus.DONE, weight=weight, final_score=score)
    await make_task(employee2, deadline=PAST, status=TaskStatus.DONE, weight=10, final_score=50)
    await make_task(tied, deadline=PAST, status=TaskStatus.DONE, weight=40, final_score=50)
    await make_task(blocked, deadline=PAST, status=TaskStatus.DONE, weight=10, final_score=150)

    rows = await kpi_for_team(session, week, NOW)
    users = [user for user, _ in rows]
    assert manager not in users and blocked not in users
    assert users == [employee, tied, employee2, no_tasks]  # KPI desc, при равенстве — по ФИО, None в конце
    assert [res.kpi for _, res in rows] == [pytest.approx(101.5), 50.0, 50.0, None]
    assert rows[-1][1].total == 0

    expected_team = (30 * 100 + 20 * 110 + 20 * 90 + 30 * 105 + 10 * 50 + 40 * 50) / 150
    assert team_kpi(rows) == pytest.approx(expected_team, abs=0.01)


async def test_team_kpi_without_items() -> None:
    assert team_kpi([]) is None
