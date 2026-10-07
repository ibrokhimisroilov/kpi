"""KPI через API: дашборд команды и сотрудника, тренд, карточка сотрудника и история оценок — те же числа,
что kpi.kpi_for_team / kpi_for_user / team_kpi и tasks.evaluated_history (docs/MINIAPP_SPEC.md §8.8, §10.3)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from bot.services import kpi, periods
from bot.services import tasks as tasks_svc
from bot.utils.text import fmt_pct
from bot.webapp import api
from bot.webapp import serializers as ser

from .conftest import EMP, EMP2, MGR, MiniApp

OFFSETS = (0, -1, -30)


async def seed_history(ma: MiniApp, frozen: Any) -> tuple[Any, list[Any]]:
    """Три сотрудника, задачи на 12 недель назад и 2 вперёд: выполненные с разными оценками и опозданиями,
    просроченные, в работе, на проверке, поручения и отменённые (не входят в KPI)."""
    mgr, staff = await ma.seed_team(3)
    now = frozen.now
    scores = [90, 100, 110, 120, 70, 100, 130]
    n = 0
    for index, emp in enumerate(staff[:2]):
        for week in range(-12, 2):
            base = now + timedelta(weeks=week, hours=index * 7)
            kinds = ["done", "active"] if week >= 0 else ["done", "done", "overdue", "submitted"]
            for kind in kinds:
                n += 1
                deadline = base + timedelta(hours=n % 20)
                if kind == "active" and deadline < now:
                    kind = "overdue"
                await ma.seed_task(
                    emp, mgr, kind=kind, title=f"Задача {n}", deadline=deadline, weight=5 + n % 4 * 5,
                    final_score=scores[n % len(scores)], late=(kind == "done" and n % 3 == 0),
                    completed_at=deadline + timedelta(hours=1) if kind == "done" else None,
                )
        await ma.seed_task(emp, mgr, kind="proposed", deadline=now + timedelta(days=1))
        await ma.seed_task(emp, mgr, kind="cancelled", deadline=now - timedelta(days=1))
    return mgr, staff


def stats_of(res: Any) -> dict[str, Any]:
    return ser.kpi_stats(res)


@pytest.mark.parametrize("kind", periods.PERIOD_KINDS)
async def test_team_dashboard_matches_chat(ma: MiniApp, frozen: Any, kind: str) -> None:
    await seed_history(ma, frozen)
    for offset in OFFSETS:
        resp = await ma.get("/api/dashboard", as_=MGR, params={"kind": kind, "offset": offset})
        assert resp.status == 200 and resp["scope"] == "team"
        async with ma.db() as session:
            period = periods.get_period(kind, offset, frozen.now)
            rows = await kpi.kpi_for_team(session, period, frozen.now)
        assert resp["period"]["label"] == period.label and resp["period"]["offset"] == offset
        assert resp["period"]["has_next"] == (offset < 0) and resp["period"]["has_prev"] is True
        assert [row["user"]["id"] for row in resp["rows"]] == [user.id for user, _ in rows]
        for row, (_, res) in zip(resp["rows"], rows, strict=True):
            assert row["kpi"] == res.kpi and row["stats"] == stats_of(res)
            assert row["kpi_text"] == (fmt_pct(res.kpi) if res.kpi is not None else "нет данных")
        assert resp["team"]["kpi"] == kpi.team_kpi(rows)
        assert resp["totals"]["employees"] == len(rows) == 3
        assert resp["totals"]["done"] == sum(res.done for _, res in rows)
        assert resp["totals"]["overdue_total"] == sum(res.overdue_total for _, res in rows)


async def test_team_trend_is_team_kpi_of_each_week(ma: MiniApp, frozen: Any) -> None:
    await seed_history(ma, frozen)
    resp = await ma.get("/api/dashboard", as_=MGR)
    trend = resp["trend"]
    assert trend["weeks"] == 8 and len(trend["points"]) == 8
    async with ma.db() as session:
        for point, offset in zip(trend["points"], range(-7, 1), strict=True):
            week = periods.get_period("week", offset, frozen.now)
            rows = await kpi.kpi_for_team(session, week, frozen.now)
            assert point["kpi"] == kpi.team_kpi(rows), offset
            assert point["start"] == ser.iso(week.start) and point["label"] == week.label[7:12]
    assert any(point["kpi"] is not None for point in trend["points"])


@pytest.mark.parametrize("kind", periods.PERIOD_KINDS)
async def test_employee_dashboard_matches_chat(ma: MiniApp, frozen: Any, kind: str) -> None:
    _, staff = await seed_history(ma, frozen)
    emp = staff[0]
    for offset in OFFSETS:
        resp = await ma.get("/api/dashboard", as_=EMP, params={"kind": kind, "offset": offset})
        assert resp.status == 200 and resp["scope"] == "self" and resp["user"]["id"] == emp.id
        async with ma.db() as session:
            period = periods.get_period(kind, offset, frozen.now)
            current = await kpi.kpi_for_user(session, emp.id, period, frozen.now)
            week = await kpi.kpi_for_user(session, emp.id, periods.get_period("week", 0, frozen.now), frozen.now)
            month = await kpi.kpi_for_user(session, emp.id, periods.get_period("month", 0, frozen.now), frozen.now)
            points = [
                (await kpi.kpi_for_user(session, emp.id, periods.get_period("week", w, frozen.now), frozen.now)).kpi
                for w in range(-7, 1)
            ]
        assert resp["current"] == ser.kpi_block(current)
        assert resp["week"] == ser.kpi_short(week) and resp["month"] == ser.kpi_short(month)
        assert [p["kpi"] for p in resp["trend"]["points"]] == points


async def test_period_params(ma: MiniApp, frozen: Any) -> None:
    await seed_history(ma, frozen)
    resp = await ma.get("/api/dashboard", as_=MGR, params={"offset": 5})
    assert resp["period"]["offset"] == 0 and resp["period"]["kind"] == "week"
    resp = await ma.get("/api/dashboard", as_=MGR, params={"kind": "month", "offset": -100000})
    assert resp["period"]["offset"] == -api.MAX_BACK_OFFSET and resp["period"]["has_prev"] is False
    for params in ({"kind": "day"}, {"offset": "x"}, {"offset": "1.5"}):
        resp = await ma.get("/api/dashboard", as_=MGR, params=params)
        assert resp.status == 400 and resp.code == "bad_request", params


async def test_user_kpi_access_and_history(ma: MiniApp, frozen: Any) -> None:
    _, staff = await seed_history(ma, frozen)
    emp, emp2, emp3 = staff
    async with ma.db() as session:
        user = await session.get(type(emp3), emp3.id)
        user.status = "blocked"
        await session.commit()
    # Руководитель — любой, в т.ч. заблокированный; сотрудник — только себя.
    resp = await ma.get(f"/api/users/{emp3.id}/kpi", as_=MGR)
    assert resp.status == 200 and resp["scope"] == "user" and resp["user"]["status"] == "blocked"
    assert (await ma.get(f"/api/users/{emp2.id}/kpi", as_=EMP)).status == 403
    assert (await ma.get("/api/users/99999/kpi", as_=MGR)).status == 404

    async with ma.db() as session:
        total = await tasks_svc.count_tasks(session, assignee_id=emp.id, statuses=["done"])
        expected = await tasks_svc.evaluated_history(session, emp.id, limit=1000)
    assert total > api.HISTORY_PAGE_SIZE
    seen: list[dict[str, Any]] = []
    page = 0
    while True:
        resp = await ma.get(f"/api/users/{emp.id}/kpi", as_=EMP, params={"page": page, "kind": "month"})
        history = resp["history"]
        assert history["total"] == total and history["page_size"] == 10 and history["page"] == page
        assert history["pages"] == -(-total // 10)
        if not history["items"]:
            break
        seen += history["items"]
        page += 1
    assert [item["task_id"] for item in seen] == [task.id for task in expected]
    first, task = seen[0], expected[0]
    assert first["final_score"] == task.final_score and first["decision"] == "approved"
    assert first["ai_score"] == task.ai_score and first["is_late"] == task.last_submission.is_late
    assert first["completed_local"] == f"{__import__('bot.utils.dates', fromlist=['to_local']).to_local(task.completed_at):%d.%m}"
    assert first["final_score_text"].endswith("%")
    # Те же поля, что у дашборда сотрудника
    dash = await ma.get("/api/dashboard", as_=EMP, params={"kind": "month"})
    assert {k: v for k, v in resp.data.items() if k not in ("scope", "history")} == {
        k: v for k, v in dash.data.items() if k != "scope"}


async def test_dashboard_without_employees_and_empty_history(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    resp = await ma.get("/api/dashboard", as_=MGR)
    assert resp["rows"] == [] and resp["team"] == {"kpi": None, "kpi_text": "нет данных"}
    assert all(point["kpi"] is None for point in resp["trend"]["points"])
    await ma.seed_user(EMP2, "Сидоров Пётр Ильич")
    resp = await ma.get(f"/api/users/{(await ma.h.get_user(EMP2)).id}/kpi", as_=EMP2)
    assert resp["history"] == {"items": [], "page": 0, "pages": 1, "total": 0, "page_size": 10}
    assert resp["current"]["kpi_text"] == "нет данных" and resp["current"]["stats"]["total"] == 0
