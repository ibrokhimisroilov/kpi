"""Поручения сотрудников через API: внесение, подтверждение, отклонение, правка, загрузка недели
(docs/MINIAPP_SPEC.md §8.5, §8.9, §12.2 test_proposals_api)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from bot.db.models import TaskSource, TaskStatus
from bot.services import tasks as tasks_svc
from bot.webapp import api

from .conftest import EMP, MGR, MGR2, MiniApp

PROPOSAL = {"title": "Подготовить справку", "expected_result": "Справка по 5 договорам", "deadline": "2026-10-09",
            "plan_value": 5, "plan_unit": "договоров", "description": "устно на планёрке"}


async def test_employee_proposes_all_managers_notified(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_team(1)
    await ma.seed_user(MGR2, "Сидоров Олег Петрович", role="manager")
    resp = await ma.post("/api/proposals", as_=EMP, json=PROPOSAL)
    assert resp.status == 201, resp.data
    assert resp["notified"] == 2 and resp["notice"] is None
    task = resp["task"]
    assert task["status"] == "proposed" and task["source"] == "employee" and task["weight_pending"] is True
    assert task["plan_text"] == "5 договоров" and task["description"] == "устно на планёрке"
    for manager in (MGR, MGR2):
        assert "📥 Сотрудник внёс поручение" in ma.h.last_text(manager)
        assert ma.h.buttons(manager) == ["✅ Подтвердить", "✏️ Изменить", "❌ Отклонить"]
    stored = await ma.task(task["id"])
    assert stored.source == TaskSource.EMPLOYEE and stored.manager_id is None


async def test_no_active_manager_gives_notice(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_user(EMP, "Иванов Иван Иванович")
    resp = await ma.post("/api/proposals", as_=EMP, json=PROPOSAL)
    assert resp.status == 201 and resp["notified"] == 0
    assert resp["notice"] == api.NO_MANAGER_NOTICE.format(task_id=resp["task"]["id"])
    assert resp["notice"].startswith("📥 Поручение #")


async def test_proposal_validation_and_busy(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_team(1)
    bad = {**PROPOSAL, "weight": 20}
    resp = await ma.post("/api/proposals", as_=EMP, json=bad)
    assert resp.status == 400 and resp.error == "Неизвестное поле «weight»"
    resp = await ma.post("/api/proposals", as_=EMP, json={**PROPOSAL, "deadline": "2026-09-01"})
    assert resp.status == 400 and resp.code == "domain" and resp.error == "Срок должен быть в будущем"
    assert ma.ctx.gate.try_enter("propose", EMP)
    resp = await ma.post("/api/proposals", as_=EMP, json=PROPOSAL)
    assert resp.status == 429 and resp.error == "⏳ Поручение уже отправляется…"
    ma.ctx.gate.leave("propose", EMP)


async def test_list_proposals_old_first(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_team(1)
    first = (await ma.post("/api/proposals", as_=EMP, json=PROPOSAL))["task"]["id"]
    frozen.advance(minutes=5)
    second = (await ma.post("/api/proposals", as_=EMP, json={**PROPOSAL, "title": "Вторая"}))["task"]["id"]
    resp = await ma.get("/api/proposals", as_=MGR)
    assert [item["task"]["id"] for item in resp["items"]] == [first, second]
    assert resp["items"][0]["task"]["actions"]["approve"] is True


async def test_approve_sets_active_and_notifies(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_team(1)
    task_id = (await ma.post("/api/proposals", as_=EMP, json=PROPOSAL))["task"]["id"]
    resp = await ma.post(f"/api/tasks/{task_id}/approve", as_=MGR, json={})
    assert resp.status == 400 and "Вес" in resp.error
    resp = await ma.post(f"/api/tasks/{task_id}/approve", as_=MGR, json={"weight": 25, "priority": "high"})
    assert resp.status == 200, resp.data
    task = resp["task"]
    assert task["status"] == "active" and task["weight"] == 25 and task["priority"] == "high"
    assert task["accepted"] is True and task["approved_at"] is not None and task["manager"]["id"] == (await ma.h.get_user(MGR)).id
    assert resp["delivered"] is True
    assert "✅ Руководитель подтвердил ваше поручение" in ma.h.last_text(EMP)
    again = await ma.post(f"/api/tasks/{task_id}/approve", as_=MGR, json={"weight": 25})
    assert again.status == 400 and again.error == "Предложение уже обработано"


async def test_approve_after_deadline_passed(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_team(1)
    task_id = (await ma.post("/api/proposals", as_=EMP, json={**PROPOSAL, "deadline": "2026-10-03"}))["task"]["id"]
    frozen.advance(days=2)
    resp = await ma.post(f"/api/tasks/{task_id}/approve", as_=MGR, json={"weight": 10})
    assert resp.status == 400 and resp.code == "domain"
    assert resp.error == "Срок поручения уже прошёл — сначала измените срок"
    # Руководитель сначала меняет срок (правка поручения), потом подтверждает.
    resp = await ma.patch(f"/api/tasks/{task_id}", as_=MGR, json={"deadline": "2026-10-10"})
    assert resp.status == 200 and resp["changed"] == ["deadline"]
    assert (await ma.post(f"/api/tasks/{task_id}/approve", as_=MGR, json={"weight": 10})).status == 200


async def test_reject_with_and_without_reason(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_team(1)
    first = (await ma.post("/api/proposals", as_=EMP, json=PROPOSAL))["task"]["id"]
    resp = await ma.post(f"/api/tasks/{first}/reject", as_=MGR, json={"reason": "Не наша зона"})
    assert resp.status == 200 and resp["task"]["status"] == "rejected" and resp["delivered"] is True
    text = ma.h.last_text(EMP)
    assert "❌ Руководитель отклонил ваше поручение" in text and "Не наша зона" in text
    second = (await ma.post("/api/proposals", as_=EMP, json=PROPOSAL))["task"]["id"]
    resp = await ma.post(f"/api/tasks/{second}/reject", as_=MGR)
    assert resp.status == 200 and "Причина" not in ma.h.last_text(EMP)
    assert (await ma.task(second)).status == TaskStatus.REJECTED
    resp = await ma.post(f"/api/tasks/{second}/reject", as_=MGR)
    assert resp.status == 400 and resp.error == "Предложение уже обработано"


async def test_edit_proposal_fields(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_team(1)
    task_id = (await ma.post("/api/proposals", as_=EMP, json=PROPOSAL))["task"]["id"]
    card = await ma.get(f"/api/tasks/{task_id}", as_=MGR)
    assert card["task"]["actions"]["edit_fields"] == ["title", "expected_result", "plan", "deadline"]
    resp = await ma.patch(f"/api/tasks/{task_id}", as_=MGR, json={"expected_result": "Справка по 6 договорам",
                                                                   "plan_value": 6})
    assert resp.status == 200 and set(resp["changed"]) == {"expected_result", "plan_value"}
    resp = await ma.patch(f"/api/tasks/{task_id}", as_=MGR, json={"priority": "high"})
    assert resp.status == 400 and resp.error == api.PROPOSAL_FIELDS


async def test_weight_load_matches_service_and_excludes_task(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    friday = frozen.now + timedelta(hours=3)
    heavy = await ma.seed_task(emp, mgr, weight=40, deadline=friday)
    await ma.seed_task(emp, mgr, weight=30, deadline=friday + timedelta(days=1))
    await ma.seed_task(emp, mgr, weight=50, deadline=friday + timedelta(days=7))  # следующая неделя
    await ma.seed_task(emp, mgr, kind="cancelled", weight=50, deadline=friday)
    await ma.post("/api/proposals", as_=EMP, json={**PROPOSAL, "deadline": "2026-10-03"})  # поручения не в счёт

    resp = await ma.get(f"/api/employees/{emp.id}/weight-load", as_=MGR, params={"deadline": "2026-10-03"})
    assert resp.status == 200, resp.data
    async with ma.db() as session:
        expected = await tasks_svc.weight_load(session, emp.id, friday)
        expected_excl = await tasks_svc.weight_load(session, emp.id, friday, exclude_task_id=heavy)
    assert resp["load"] == expected == 70  # 40 + 30; поручение, отмена и следующая неделя не в счёт
    assert resp["week_label"] == "Неделя 28.09–04.10.2026"
    assert [o["weight"] for o in resp["options"]] == list(api.WEIGHT_OPTIONS)
    assert [o["over"] for o in resp["options"]] == [70 + w > 100 for w in api.WEIGHT_OPTIONS]

    resp = await ma.get(f"/api/employees/{emp.id}/weight-load", as_=MGR,
                        params={"deadline": "2026-10-03", "exclude_task_id": heavy})
    assert resp["load"] == expected_excl == 30

    resp = await ma.get("/api/employees/99999/weight-load", as_=MGR, params={"deadline": "2026-10-03"})
    assert resp.status == 404 and resp.error == api.USER_NOT_FOUND
    resp = await ma.get(f"/api/employees/{emp.id}/weight-load", as_=MGR)
    assert resp.status == 400 and resp.code == "bad_request"
    resp = await ma.get(f"/api/employees/{emp.id}/weight-load", as_=MGR, params={"deadline": "x", "exclude_task_id": "1"})
    assert resp.status == 400


async def test_employees_list(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_team(3)
    await ma.seed_user(3001, "Аверин Блок", status="blocked")
    resp = await ma.get("/api/employees", as_=MGR)
    assert [item["full_name"] for item in resp["items"]] == [
        "Иванов Иван Иванович", "Кузнецова Анна Сергеевна", "Сидоров Пётр Ильич"]
    assert resp["items"][0]["short_name"] == "Иванов И. И." and resp["items"][0]["position"] == "Юрист"
    assert frozen.now - timedelta(seconds=1) < frozen.now  # время заморожено
