"""Бюджеты обменов с базой для API приложения (docs/MINIAPP_SPEC.md §6.3, §12.5).

Замер — ``RoundTripProbe`` (tests/perf/roundtrips.py) на движке «main» вокруг одного HTTP-запроса через
TestClient: обмены, которые ждёт пользователь (SQL-выражения, подготовка выражений, BEGIN/COMMIT по модели
PostgreSQL + asyncpg). Перед замером — такой же запрос «на разогрев»: подготовленные выражения кэшируются на
соединении, в работе бот почти всегда отвечает уже с кэшем. Фоновые задачи приложения дожидаются до и
после замера. Счётчики одинаковы на SQLite и PostgreSQL (TEST_DATABASE_URL).

``WEBAPP_BUDGETS`` — фактически измеренные числа (07.10.2026), не больше потолков §12.5 (``CEILINGS``);
проверка — с запасом как у бюджетов чата (10 %, не меньше 1). Бюджеты чата (tests/perf) не меняются.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.db.models import (
    Attachment,
    AttachmentKind,
    Priority,
    ReviewDecision,
    Submission,
    Task,
    TaskSource,
    TaskStatus,
)
from bot.utils.dates import utcnow
from perf.roundtrips import Measurement, RoundTripProbe

from .conftest import EMP, MGR, MiniApp

# Потолки §12.5 и фактически измеренные числа (07.10.2026).
CEILINGS: dict[str, int] = {
    "M01": 2, "M02": 3, "M03": 4, "M04": 2, "M05": 5, "M06": 3,
    "M07": 2, "M08": 5, "M09": 4, "M10": 3, "M11": 9, "M12": 6,
}
WEBAPP_BUDGETS: dict[str, int] = {
    "M01": 2,  # пользователь + счётчики вкладок одним запросом
    "M02": 2,  # пользователь + страница с count(*) OVER ()
    "M03": 3,  # + счётчики вкладок
    "M04": 2,  # пользователь + выборка для поиска
    "M05": 5,  # пользователь, задача, сдачи, файлы, журнал
    "M06": 3,  # пользователь, сотрудники, снимки периода и тренда
    "M07": 2,  # пользователь, снимки
    "M08": 4,  # пользователь, сотрудник, снимки, история с итогом
    "M09": 4,  # пользователь, задачи, сдачи, файлы
    "M10": 3,  # пользователь, поручения, сдачи
    "M11": 9,  # пользователь, сдача (3), BEGIN, UPDATE задачи, UPDATE сдачи + событие, COMMIT
    "M12": 6,  # пользователь, исполнитель, BEGIN, INSERT задачи, INSERT события, COMMIT
}
NAMES = {
    "M01": "GET /api/me (начальник)",
    "M02": "GET /api/tasks scope=all status=open",
    "M03": "GET /api/tasks … counts=1",
    "M04": "GET /api/tasks q=договор",
    "M05": "GET /api/tasks/{id} (доработка: сдача + файл)",
    "M06": "GET /api/dashboard (начальник)",
    "M07": "GET /api/dashboard (сотрудник)",
    "M08": "GET /api/users/{id}/kpi",
    "M09": "GET /api/review",
    "M10": "GET /api/proposals",
    "M11": "POST /api/submissions/{id}/confirm",
    "M12": "POST /api/tasks (создание)",
}


def _slack(value: int) -> int:
    """Запас к бюджету: 10 %, но не меньше одного обмена (как tests/perf)."""
    return max(1, round(value * 0.1))


def test_budgets_within_spec_ceilings() -> None:
    assert set(WEBAPP_BUDGETS) == set(CEILINGS) == set(NAMES)
    for key, value in WEBAPP_BUDGETS.items():
        assert value <= CEILINGS[key], key


def check(key: str, measurement: Measurement) -> None:
    budget = WEBAPP_BUDGETS[key]
    waited = measurement.round_trips
    print(f"{key} {NAMES[key]}: {waited} обменов ({measurement.total.as_dict()})")
    sql = "\n".join(statement for _, statement in measurement.sql)
    assert waited <= budget + _slack(budget), f"{key} {NAMES[key]}: {waited} обменов, бюджет {budget}\n{sql}"
    assert waited <= CEILINGS[key], f"{key}: {waited} обменов, потолок §12.5 — {CEILINGS[key]}\n{sql}"


@pytest.fixture
def probe(engine: AsyncEngine) -> Any:
    rt = RoundTripProbe()
    rt.attach(engine, "main")
    yield rt
    rt.detach()


async def measure(ma: MiniApp, probe: RoundTripProbe, key: str, call: Callable[[], Awaitable[Any]],
                  *, warm_up: Callable[[], Awaitable[Any]] | None = None) -> tuple[Measurement, Any]:
    """Разогрев (тот же запрос), затем замер одного запроса; фоновые задачи — вне замера."""
    await (warm_up or call)()
    await ma.drain()
    async with probe.measure(f"{key} {NAMES[key]}") as m:
        result = await call()
    await ma.drain()
    return m, result


async def seed_bulk(ma: MiniApp, employees: int, per_employee: int, *, scale_kind: str = "mix") -> tuple[Any, list[Any]]:
    """employees сотрудников × per_employee задач: смесь видов (в работе, просроченные, доработка, на проверке
    с файлом, выполненные) с договорами в названии, всё одной записью."""
    mgr, _ = await ma.seed_team(0)
    staff = [await ma.seed_user(5000 + n, f"Сотрудник{n:02d} Иван Иванович", position="Юрист") for n in range(employees)]
    now = utcnow()
    kinds = ["active", "active", "overdue", "rework", "submitted", "done", "done", "active", "done", "active"]
    async with ma.db() as session:
        for emp in staff:
            for n in range(per_employee):
                kind = kinds[n % len(kinds)] if scale_kind == "mix" else scale_kind
                deadline = now + timedelta(days=1 + n % 5, hours=n) if kind not in ("overdue", "done") else now - timedelta(hours=1 + n)
                task = Task(
                    title=f"Анализ договоров {n}", expected_result="Проверить 10 договоров", plan_value=10.0,
                    plan_unit="договоров", deadline=deadline, priority=Priority.MEDIUM, weight=10,
                    status=TaskStatus.ACTIVE, source=TaskSource.MANAGER, assignee_id=emp.id, created_by_id=mgr.id,
                    manager_id=mgr.id, accepted_at=now - timedelta(days=1), rework_count=0,
                )
                if kind in ("rework", "submitted", "done"):
                    sub = Submission(
                        attempt=1, fact_text="Проверено 10 договоров", fact_value=10.0, created_at=now - timedelta(hours=5),
                        deadline_at_submit=deadline, ai_score=100.0, ai_rationale="План выполнен.", ai_source="rules",
                        attachments=[Attachment(kind=AttachmentKind.DOCUMENT, file_id=f"f-{emp.id}-{n}", file_name="r.pdf")],
                    )
                    task.submissions = [sub]
                    task.submitted_at = sub.created_at
                    task.ai_score = 100.0
                    if kind == "rework":
                        task.status, task.rework_count = TaskStatus.REWORK, 1
                        sub.decision, sub.review_comment = ReviewDecision.REWORK, "Добавьте выводы"
                    elif kind == "submitted":
                        task.status = TaskStatus.SUBMITTED
                    else:
                        task.status, task.final_score, task.completed_at = TaskStatus.DONE, 100.0, now - timedelta(hours=1)
                        sub.decision, sub.final_score = ReviewDecision.APPROVED, 100.0
                    if sub.decision is not None:
                        sub.reviewer_id, sub.reviewed_at = mgr.id, now - timedelta(hours=2)
                session.add(task)
        await session.commit()
    return mgr, staff


async def test_m01_me(ma: MiniApp, probe: RoundTripProbe) -> None:
    await seed_bulk(ma, 5, 10)
    m, resp = await measure(ma, probe, "M01", lambda: ma.get("/api/me", as_=MGR))
    assert resp.status == 200 and resp["counts"]["open"] > 0
    check("M01", m)


@pytest.mark.parametrize("scale", [1, 5])
async def test_m02_m03_m04_lists(ma: MiniApp, probe: RoundTripProbe, scale: int) -> None:
    await seed_bulk(ma, 5, 10 * scale)
    params = {"scope": "all", "status": "open", "limit": 20}
    m02, resp = await measure(ma, probe, "M02", lambda: ma.get("/api/tasks", as_=MGR, params=params))
    assert resp.status == 200 and len(resp["items"]) == 20
    check("M02", m02)
    m03, resp = await measure(ma, probe, "M03", lambda: ma.get("/api/tasks", as_=MGR, params={**params, "counts": 1}))
    assert resp["counts"]["all"] == 50 * scale
    check("M03", m03)
    m04, resp = await measure(ma, probe, "M04", lambda: ma.get("/api/tasks", as_=MGR, params={"status": "all", "q": "договор"}))
    assert resp["total"] == 50 * scale
    check("M04", m04)
    if scale == 5:  # число обменов не зависит от объёма данных
        assert m02.round_trips == WEBAPP_BUDGETS["M02"] and m03.round_trips == WEBAPP_BUDGETS["M03"]


async def test_m05_task_card(ma: MiniApp, probe: RoundTripProbe) -> None:
    await seed_bulk(ma, 2, 10)
    rework = next(t for t in await ma.h.scalars(select(Task)) if t.status == TaskStatus.REWORK)
    m, resp = await measure(ma, probe, "M05", lambda: ma.get(f"/api/tasks/{rework.id}", as_=MGR))
    assert resp.status == 200 and resp["submissions"][0]["attachments"]
    check("M05", m)


@pytest.mark.parametrize("employees", [2, 5])
async def test_m06_team_dashboard(ma: MiniApp, probe: RoundTripProbe, employees: int) -> None:
    await seed_bulk(ma, employees, 10)
    m, resp = await measure(ma, probe, "M06", lambda: ma.get("/api/dashboard", as_=MGR))
    assert resp.status == 200 and len(resp["rows"]) == employees
    check("M06", m)
    assert m.round_trips == WEBAPP_BUDGETS["M06"]


async def test_m06_far_period_takes_one_more_query(ma: MiniApp, probe: RoundTripProbe) -> None:
    await seed_bulk(ma, 2, 10)
    m, resp = await measure(ma, probe, "M06", lambda: ma.get("/api/dashboard", as_=MGR, params={"offset": -30}))
    assert resp.status == 200
    assert m.round_trips == WEBAPP_BUDGETS["M06"] + 1  # период и окно тренда далеко друг от друга — два запроса


async def test_m07_m08_employee_kpi(ma: MiniApp, probe: RoundTripProbe) -> None:
    _, staff = await seed_bulk(ma, 1, 10)
    emp_tg = staff[0].tg_id
    m07, resp = await measure(ma, probe, "M07", lambda: ma.get("/api/dashboard", as_=emp_tg))
    assert resp.status == 200 and resp["scope"] == "self"
    check("M07", m07)
    m08, resp = await measure(ma, probe, "M08", lambda: ma.get(f"/api/users/{staff[0].id}/kpi", as_=MGR))
    assert resp.status == 200 and resp["history"]["total"] == 3
    check("M08", m08)


@pytest.mark.parametrize("scale", [1, 5])
async def test_m09_review_queue(ma: MiniApp, probe: RoundTripProbe, scale: int) -> None:
    await seed_bulk(ma, 1, 5 * scale, scale_kind="submitted")
    m, resp = await measure(ma, probe, "M09", lambda: ma.get("/api/review", as_=MGR))
    assert resp.status == 200 and len(resp["items"]) == 5 * scale
    check("M09", m)
    assert m.round_trips == WEBAPP_BUDGETS["M09"]


async def test_m10_proposals(ma: MiniApp, probe: RoundTripProbe) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    for n in range(3):
        await ma.seed_task(emp, mgr, kind="proposed", title=f"Поручение {n}")
    m, resp = await measure(ma, probe, "M10", lambda: ma.get("/api/proposals", as_=MGR))
    assert len(resp["items"]) == 3
    check("M10", m)


async def test_m11_confirm(ma: MiniApp, probe: RoundTripProbe) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    warm = (await ma.task(await ma.seed_task(emp, mgr, kind="submitted", files=1))).submissions[0].id
    target = (await ma.task(await ma.seed_task(emp, mgr, kind="submitted", files=1))).submissions[0].id
    m, resp = await measure(
        ma, probe, "M11",
        lambda: ma.post(f"/api/submissions/{target}/confirm", as_=MGR),
        warm_up=lambda: ma.post(f"/api/submissions/{warm}/confirm", as_=MGR),
    )
    assert resp.status == 200 and resp["task"]["status"] == "done"
    check("M11", m)


async def test_m12_create_task(ma: MiniApp, probe: RoundTripProbe) -> None:
    _, (emp,) = await ma.seed_team(1)
    body = {"assignee_id": emp.id, "title": "Анализ договоров", "expected_result": "Проверить 100 договоров",
            "deadline": (utcnow() + timedelta(days=3)).strftime("%Y-%m-%d"), "weight": 20}
    m, resp = await measure(ma, probe, "M12", lambda: ma.post("/api/tasks", as_=MGR, json=body))
    assert resp.status == 201
    check("M12", m)
    assert EMP  # исполнитель — Иванов (2001)
