"""Личные шаблоны задач и поручений через API (docs/MINIAPP_SPEC.md §16).

Шаблон — заготовка формы: название, ожидаемый результат, план; у начальника ещё приоритет, вес и исполнитель.
Он личный (видит и меняет только владелец), не несёт ничего из жизни исходной задачи (номер, статус, сроки,
результаты, оценки, файлы) и не создаёт задач сам: из него заполняется обычная форма, а задача или поручение
создаётся обычным адресом со всеми проверками.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select, text

from bot.db.models import Role, TaskTemplate
from bot.services import templates as templates_svc
from bot.services import users as users_svc

from .conftest import EMP, EMP2, MGR, MGR2, MiniApp
from .test_api_budget import probe  # noqa: F401 - фикстура замера обменов с базой

BASE_KEYS = {"id", "title", "expected_result", "plan_value", "plan_unit", "plan_text", "updated_at"}
MANAGER_KEYS = BASE_KEYS | {"priority", "priority_label", "weight", "assignee"}
BODY = {"title": "Сверка с поставщиками", "expected_result": "Сверить расчёты и подписать акты", "plan_value": 60, "plan_unit": "актов"}


async def stored(ma: MiniApp) -> list[TaskTemplate]:
    async with ma.db() as session:
        return list(await session.scalars(select(TaskTemplate).order_by(TaskTemplate.id)))


async def seed_template(ma: MiniApp, owner: Any, title: str = "Чужой шаблон", **fields: Any) -> int:
    async with ma.db() as session:
        row = TaskTemplate(owner_id=owner.id, title=title, expected_result=fields.pop("expected_result", "Результат"), **fields)
        session.add(row)
        await session.commit()
        return row.id


async def task_snapshot(ma: MiniApp) -> list[tuple[Any, ...]]:
    async with ma.db() as session:
        tasks = await session.execute(text("SELECT id, status, title, deadline, weight, priority, final_score, updated_at FROM tasks ORDER BY id"))
        events = await session.scalar(text("SELECT count(*) FROM task_events"))
        return [*map(tuple, tasks), (events,)]


# --- Создание вручную -----------------------------------------------------------------------------------------


async def test_manager_saves_a_template_with_all_fields(ma: MiniApp) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    resp = await ma.post("/api/templates", as_=MGR, json={**BODY, "priority": "high", "weight": 25, "assignee_id": emp.id})
    assert resp.status == 201, resp.data
    tpl = resp["template"]
    assert set(tpl) == MANAGER_KEYS
    assert (tpl["title"], tpl["expected_result"], tpl["plan_value"], tpl["plan_unit"]) == (BODY["title"], BODY["expected_result"], 60.0, "актов")
    assert tpl["plan_text"] == "60 актов" and tpl["priority"] == "high" and tpl["weight"] == 25
    assert tpl["assignee"]["id"] == emp.id and tpl["assignee"]["short_name"] == "Иванов И. И."
    [row] = await stored(ma)
    assert row.owner_id == mgr.id and row.assignee_id == emp.id

    listed = await ma.get("/api/templates", as_=MGR)
    assert listed.status == 200 and listed["limit"] == 50 and [item["id"] for item in listed["items"]] == [tpl["id"]]
    assert listed["items"][0] == tpl


async def test_manager_template_needs_only_title_and_result(ma: MiniApp) -> None:
    await ma.seed_team(1)
    resp = await ma.post("/api/templates", as_=MGR, json={"title": "Еженедельный отчёт", "expected_result": "Отчёт за неделю"})
    assert resp.status == 201, resp.data
    tpl = resp["template"]
    assert tpl["plan_value"] is None and tpl["plan_text"] is None
    assert tpl["priority"] == "medium" and tpl["weight"] is None and tpl["assignee"] is None


async def test_employee_template_keeps_three_fields_only(ma: MiniApp) -> None:
    _mgr, (emp,) = await ma.seed_team(1)
    resp = await ma.post("/api/templates", as_=EMP, json=BODY)
    assert resp.status == 201, resp.data
    assert set(resp["template"]) == BASE_KEYS
    [row] = await stored(ma)
    assert row.owner_id == emp.id and (row.priority, row.weight, row.assignee_id) == (None, None, None)


@pytest.mark.parametrize("extra", [{"priority": "high"}, {"weight": 50}, {"assignee_id": 1}, {"deadline": "2026-10-20"}, {"status": "done"}])
async def test_employee_cannot_put_anything_else_into_a_template(ma: MiniApp, extra: dict[str, Any]) -> None:
    await ma.seed_team(1)
    resp = await ma.post("/api/templates", as_=EMP, json={**BODY, **extra})
    assert resp.status == 400 and resp.code == "bad_request", resp.data
    assert await stored(ma) == []


@pytest.mark.parametrize(
    "patch",
    [
        {"title": ""}, {"title": "я" * 256}, {"title": 5}, {"expected_result": None}, {"expected_result": "я" * 2001},
        {"plan_value": "много"}, {"plan_value": -1}, {"plan_unit": "я" * 65}, {"priority": "срочно"}, {"weight": 0},
        {"weight": 101}, {"weight": "20"}, {"assignee_id": "2"}, {"deadline": "2026-10-20"}, {"description": "текст"},
        {"final_score": 150}, {"unknown": 1},
    ],
)
async def test_bad_values_are_refused(ma: MiniApp, patch: dict[str, Any]) -> None:
    await ma.seed_team(1)
    resp = await ma.post("/api/templates", as_=MGR, json={**BODY, **patch})
    assert resp.status == 400 and resp.code == "bad_request", (patch, resp.data)
    assert await stored(ma) == []


async def test_assignee_must_be_an_active_employee(ma: MiniApp) -> None:
    mgr, (emp, emp2) = await ma.seed_team(2)
    async with ma.db() as session:
        await users_svc.block_user(session, emp2.id, await session.get(type(mgr), mgr.id))
        await session.commit()
    for assignee_id in (mgr.id, emp2.id, 99999):
        resp = await ma.post("/api/templates", as_=MGR, json={**BODY, "assignee_id": assignee_id})
        assert resp.status == 400 and resp.code == "domain", (assignee_id, resp.data)
    assert await stored(ma) == []
    assert (await ma.post("/api/templates", as_=MGR, json={**BODY, "assignee_id": emp.id})).status == 201


async def test_unit_without_a_number_is_dropped(ma: MiniApp) -> None:
    await ma.seed_team(1)
    resp = await ma.post("/api/templates", as_=EMP, json={**BODY, "plan_value": None})
    assert resp.status == 201 and resp["template"]["plan_unit"] is None and resp["template"]["plan_text"] is None


# --- Личное: только владелец ----------------------------------------------------------------------------------


async def test_templates_are_private(ma: MiniApp) -> None:
    mgr, (emp, emp2) = await ma.seed_team(2)
    mgr2 = await ma.seed_user(MGR2, "Орлова Мария Петровна", role="manager")
    ids = {
        MGR: await seed_template(ma, mgr, "Шаблон Петровой"), MGR2: await seed_template(ma, mgr2, "Шаблон Орловой"),
        EMP: await seed_template(ma, emp, "Шаблон Иванова"), EMP2: await seed_template(ma, emp2, "Шаблон Сидорова"),
    }
    for viewer, own in ids.items():
        listed = await ma.get("/api/templates", as_=viewer)
        assert [item["id"] for item in listed["items"]] == [own], viewer
        for owner, foreign in ids.items():
            if owner == viewer:
                continue
            changed = await ma.patch(f"/api/templates/{foreign}", as_=viewer, json={"title": "Захвачен"})
            removed = await ma.post(f"/api/templates/{foreign}/delete", as_=viewer)
            assert (changed.status, changed.code) == (404, "not_found"), (viewer, owner, changed.data)
            assert (removed.status, removed.code) == (404, "not_found"), (viewer, owner, removed.data)
    assert [row.title for row in await stored(ma)] == ["Шаблон Петровой", "Шаблон Орловой", "Шаблон Иванова", "Шаблон Сидорова"]


@pytest.mark.parametrize("tg_id", [3001, 3002, 9999])
async def test_people_without_access_have_no_templates(ma: MiniApp, tg_id: int) -> None:
    await ma.seed_team(1)
    await ma.seed_user(3001, "Новиков Пётр Ильич", status="pending")
    await ma.seed_user(3002, "Бывший Олег Ильич", status="blocked")
    for resp in (await ma.get("/api/templates", as_=tg_id), await ma.post("/api/templates", as_=tg_id, json=BODY)):
        assert resp.status == 403 and resp.code in ("pending", "blocked", "not_registered"), resp.data
    assert await stored(ma) == []


# --- Правка и удаление ----------------------------------------------------------------------------------------


async def test_owner_changes_a_template(ma: MiniApp) -> None:
    _mgr, (emp, emp2) = await ma.seed_team(2)
    created = await ma.post("/api/templates", as_=MGR, json={**BODY, "priority": "high", "weight": 25, "assignee_id": emp.id})
    template_id = created["template"]["id"]

    resp = await ma.patch(f"/api/templates/{template_id}", as_=MGR, json={"title": "Сверка за квартал", "weight": 30, "assignee_id": emp2.id})
    assert resp.status == 200, resp.data
    tpl = resp["template"]
    assert (tpl["title"], tpl["weight"], tpl["assignee"]["id"], tpl["priority"]) == ("Сверка за квартал", 30, emp2.id, "high")
    assert tpl["expected_result"] == BODY["expected_result"] and tpl["plan_text"] == "60 актов"  # остальное не тронуто

    cleared = await ma.patch(f"/api/templates/{template_id}", as_=MGR, json={"plan_value": None, "weight": None, "assignee_id": None})
    tpl = cleared["template"]
    assert cleared.status == 200 and (tpl["plan_value"], tpl["plan_unit"], tpl["weight"], tpl["assignee"]) == (None, None, None, None)

    assert (await ma.patch(f"/api/templates/{template_id}", as_=MGR, json={})).status == 400
    assert (await ma.patch(f"/api/templates/{template_id}", as_=MGR, json={"title": " "})).status == 400
    assert (await ma.patch("/api/templates/99999", as_=MGR, json={"title": "Нет такого"})).status == 404
    assert (await stored(ma))[0].title == "Сверка за квартал"


async def test_employee_cannot_add_manager_fields_by_editing(ma: MiniApp) -> None:
    _mgr, (emp,) = await ma.seed_team(1)
    template_id = await seed_template(ma, emp, "Справка")
    for extra in ({"weight": 50}, {"priority": "high"}, {"assignee_id": emp.id}):
        resp = await ma.patch(f"/api/templates/{template_id}", as_=EMP, json=extra)
        assert resp.status == 400 and resp.code == "bad_request", (extra, resp.data)
    resp = await ma.patch(f"/api/templates/{template_id}", as_=EMP, json={"expected_result": "Справка по 5 договорам"})
    assert resp.status == 200 and set(resp["template"]) == BASE_KEYS
    [row] = await stored(ma)
    assert (row.priority, row.weight, row.assignee_id, row.expected_result) == (None, None, None, "Справка по 5 договорам")


async def test_owner_deletes_a_template(ma: MiniApp) -> None:
    _mgr, (emp,) = await ma.seed_team(1)
    keep = await seed_template(ma, emp, "Оставить")
    drop = await seed_template(ma, emp, "Удалить")
    resp = await ma.post(f"/api/templates/{drop}/delete", as_=EMP)
    assert resp.status == 200 and resp.data == {"deleted": True}
    assert [row.id for row in await stored(ma)] == [keep]
    again = await ma.post(f"/api/templates/{drop}/delete", as_=EMP)
    assert (again.status, again.code) == (404, "not_found")


# --- «Сохранить как шаблон» из задачи ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["active", "overdue", "rework", "submitted", "done", "cancelled", "proposed", "rejected"])
async def test_manager_saves_a_template_from_any_task(ma: MiniApp, kind: str) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr, kind=kind, title="Анализ договоров", weight=30, files=1 if kind in ("submitted", "done") else 0)
    before = await task_snapshot(ma)

    resp = await ma.post("/api/templates", as_=MGR, json={"task_id": task_id})
    assert resp.status == 201, resp.data
    tpl = resp["template"]
    assert set(tpl) == MANAGER_KEYS
    assert (tpl["title"], tpl["expected_result"], tpl["plan_text"]) == ("Анализ договоров", "Проверить 100 договоров и представить отчёт", "100 договоров")
    assert (tpl["priority"], tpl["weight"], tpl["assignee"]["id"]) == ("medium", 30, emp.id)
    assert await task_snapshot(ma) == before  # исходная задача не изменилась, в её истории ничего нового
    assert ma.h.sent_to(EMP) == []  # и сотруднику ничего не ушло


async def test_employee_saves_a_template_from_own_task_only(ma: MiniApp) -> None:
    mgr, (emp, emp2) = await ma.seed_team(2)
    own = await ma.seed_task(emp, mgr, kind="done", title="Претензии за сентябрь", weight=40)
    foreign = await ma.seed_task(emp2, mgr, kind="done", title="Чужая задача")
    before = await task_snapshot(ma)

    resp = await ma.post("/api/templates", as_=EMP, json={"task_id": own})
    assert resp.status == 201 and set(resp["template"]) == BASE_KEYS, resp.data
    assert resp["template"]["title"] == "Претензии за сентябрь" and resp["template"]["plan_text"] == "100 договоров"
    [row] = await stored(ma)
    assert (row.owner_id, row.priority, row.weight, row.assignee_id) == (emp.id, None, None, None)

    refused = await ma.post("/api/templates", as_=EMP, json={"task_id": foreign})
    assert (refused.status, refused.code) == (403, "forbidden")
    missing = await ma.post("/api/templates", as_=EMP, json={"task_id": 99999})
    assert (missing.status, missing.code) == (404, "not_found")
    assert len(await stored(ma)) == 1 and await task_snapshot(ma) == before


@pytest.mark.parametrize("body", [{"task_id": 1, "title": "Своё название"}, {"task_id": "1"}, {"task_id": 0}, {"task_id": None, "weight": 5}])
async def test_task_number_cannot_be_mixed_with_fields(ma: MiniApp, body: dict[str, Any]) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    await ma.seed_task(emp, mgr)
    resp = await ma.post("/api/templates", as_=MGR, json=body)
    assert resp.status == 400 and resp.code == "bad_request", (body, resp.data)
    assert await stored(ma) == []


def test_template_has_no_place_for_task_history() -> None:
    """В таблице шаблонов просто нет столбцов для номера задачи, статуса, срока, результатов, оценок и файлов."""
    columns = set(TaskTemplate.__table__.columns.keys())
    assert columns == {
        "id", "owner_id", "title", "expected_result", "plan_value", "plan_unit", "priority", "weight", "assignee_id",
        "created_at", "updated_at",
    }
    assert {fk.column.table.name for fk in TaskTemplate.__table__.foreign_keys} == {"users"}  # на задачи не ссылается


async def test_former_assignee_is_not_offered(ma: MiniApp) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr, kind="done")
    manual = await ma.post("/api/templates", as_=MGR, json={**BODY, "assignee_id": emp.id})
    async with ma.db() as session:
        await users_svc.block_user(session, emp.id, await session.get(type(mgr), mgr.id))
        await session.commit()
    from_task = await ma.post("/api/templates", as_=MGR, json={"task_id": task_id})
    assert from_task.status == 201 and from_task["template"]["assignee"] is None
    listed = await ma.get("/api/templates", as_=MGR)
    assert {item["id"]: item["assignee"] for item in listed["items"]} == {manual["template"]["id"]: None, from_task["template"]["id"]: None}


# --- Предел, смена роли, язык ---------------------------------------------------------------------------------


async def test_no_more_than_fifty_templates_per_person(ma: MiniApp) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr)
    for n in range(templates_svc.LIMIT - 1):
        await seed_template(ma, mgr, f"Шаблон {n}")
    assert (await ma.post("/api/templates", as_=MGR, json=BODY)).status == 201  # пятидесятый
    for body in (BODY, {"task_id": task_id}):
        resp = await ma.post("/api/templates", as_=MGR, json=body)
        assert resp.status == 400 and resp.code == "domain" and "50" in resp.error, resp.data
    async with ma.db() as session:
        assert await session.scalar(select(func.count()).select_from(TaskTemplate)) == templates_svc.LIMIT
    assert (await ma.post("/api/templates", as_=EMP, json=BODY)).status == 201  # предел — на человека, не общий


async def test_newest_template_comes_first(ma: MiniApp, clock: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    await ma.seed_team(1)
    monkeypatch.setattr(templates_svc, "utcnow", lambda: clock.now)
    first = (await ma.post("/api/templates", as_=EMP, json={**BODY, "title": "Первый"}))["template"]["id"]
    clock.advance(minutes=5)
    second = (await ma.post("/api/templates", as_=EMP, json={**BODY, "title": "Второй"}))["template"]["id"]
    assert [item["id"] for item in (await ma.get("/api/templates", as_=EMP))["items"]] == [second, first]
    clock.advance(minutes=5)
    await ma.patch(f"/api/templates/{first}", as_=EMP, json={"title": "Первый, исправленный"})
    assert [item["id"] for item in (await ma.get("/api/templates", as_=EMP))["items"]] == [first, second]


async def test_role_change_keeps_templates_but_not_manager_fields(ma: MiniApp) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    mgr2 = await ma.seed_user(MGR2, "Орлова Мария Петровна", role="manager")
    created = await ma.post("/api/templates", as_=MGR2, json={**BODY, "priority": "high", "weight": 25, "assignee_id": emp.id})
    employee_template = await seed_template(ma, emp, "Справка")
    async with ma.db() as session:
        actor = await session.get(type(mgr), mgr.id)
        await users_svc.set_role(session, mgr2.id, Role.EMPLOYEE, actor)
        await users_svc.set_role(session, emp.id, Role.MANAGER, actor)
        await session.commit()

    demoted = await ma.get("/api/templates", as_=MGR2)
    assert [set(item) for item in demoted["items"]] == [BASE_KEYS] and demoted["items"][0]["id"] == created["template"]["id"]
    promoted = await ma.get("/api/templates", as_=EMP)
    [item] = promoted["items"]
    assert item["id"] == employee_template and set(item) == MANAGER_KEYS
    assert (item["priority"], item["weight"], item["assignee"]) == ("medium", None, None)


async def test_uzbek_answers(ma: MiniApp) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    assert (await ma.post("/api/lang", as_=MGR, json={"lang": "uz"})).status == 200
    created = await ma.post("/api/templates", as_=MGR, json={**BODY, "priority": "high"})
    assert created.status == 201 and "Yuqori" in created["template"]["priority_label"]
    missing = await ma.post("/api/templates/99999/delete", as_=MGR)
    assert missing.status == 404 and "topilmadi" in missing.error.lower(), missing.data
    for n in range(templates_svc.LIMIT - 1):
        await seed_template(ma, mgr, f"Шаблон {n}")
    full = await ma.post("/api/templates", as_=MGR, json=BODY)
    assert full.status == 400 and "shablon" in full.error.lower() and "Шаблон" not in full.error, full.data
    assert emp  # сотрудник в этой проверке не участвует


# --- Шаблон не создаёт задач сам и ничего не обходит --------------------------------------------------------------


async def test_using_a_template_goes_through_the_usual_creation(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    boss_tpl = (await ma.post("/api/templates", as_=MGR, json={**BODY, "priority": "high", "weight": 25, "assignee_id": emp.id}))["template"]
    emp_tpl = (await ma.post("/api/templates", as_=EMP, json=BODY))["template"]
    async with ma.db() as session:
        assert await session.scalar(text("SELECT count(*) FROM tasks")) == 0  # шаблоны задач не создают

    fields = {key: boss_tpl[key] for key in ("title", "expected_result", "plan_value", "plan_unit", "priority", "weight")}
    without_deadline = await ma.post("/api/tasks", as_=MGR, json={**fields, "assignee_id": boss_tpl["assignee"]["id"]})
    assert without_deadline.status == 400  # срок выбирается заново: в шаблоне его нет
    task = await ma.post("/api/tasks", as_=MGR, json={**fields, "assignee_id": boss_tpl["assignee"]["id"], "deadline": "2026-10-09"})
    assert task.status == 201 and task["task"]["status"] == "active" and task["task"]["weight"] == 25

    fields = {key: emp_tpl[key] for key in ("title", "expected_result", "plan_value", "plan_unit")}
    proposal = await ma.post("/api/proposals", as_=EMP, json={**fields, "deadline": "2026-10-09"})
    assert proposal.status == 201 and proposal["task"]["status"] == "proposed"  # как обычно — на подтверждение начальнику
    direct = await ma.post("/api/tasks", as_=EMP, json={**fields, "assignee_id": emp.id, "deadline": "2026-10-09", "priority": "high", "weight": 50})
    assert (direct.status, direct.code) == (403, "forbidden")
    assert mgr and len(await stored(ma)) == 2  # шаблоны после использования остаются


# --- Обмены с базой --------------------------------------------------------------------------------------------


async def test_list_costs_no_more_than_the_proposal_queue(ma: MiniApp, probe: Any) -> None:
    """Список шаблонов — один запрос сверх чтения самого человека (как у очереди поручений)."""
    mgr, (emp,) = await ma.seed_team(1)
    for n in range(20):
        await seed_template(ma, mgr, f"Шаблон {n}", assignee_id=emp.id, weight=10)
    assert (await ma.get("/api/templates", as_=MGR)).status == 200
    assert (await ma.get("/api/proposals", as_=MGR)).status == 200
    async with probe.measure("очередь поручений") as queue:
        await ma.get("/api/proposals", as_=MGR)
    async with probe.measure("список шаблонов") as listing:
        resp = await ma.get("/api/templates", as_=MGR)
    assert resp.status == 200 and len(resp["items"]) == 20
    sql = "\n".join(statement for _, statement in listing.sql)
    assert listing.round_trips <= queue.round_trips, f"шаблоны {listing.round_trips}, поручения {queue.round_trips}\n{sql}"
