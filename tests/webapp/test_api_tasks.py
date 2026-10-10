"""Задачи через API: списки, поиск, карточка, видимость оценки AI, создание, правка, принятие, отмена
(docs/MINIAPP_SPEC.md §7, §8.4, §12.2 test_tasks_api)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from bot.db.models import EventType, Task, TaskEvent, TaskStatus
from bot.services import tasks as tasks_svc
from bot.ui import keyboards, render
from bot.webapp import api

from .conftest import EMP, EMP2, EMP3, MGR, MiniApp, plain

KINDS = ["active", "overdue", "rework", "submitted", "done", "proposed", "cancelled", "rejected"]


async def seed_mix(ma: MiniApp) -> tuple[Any, list[Any], dict[str, int]]:
    """Начальник и три сотрудника; у Иванова — задача каждого вида, у остальных — по нескольку."""
    mgr, staff = await ma.seed_team(3)
    emp, emp2, emp3 = staff
    now = __import__("bot.utils.dates", fromlist=["utcnow"]).utcnow()
    ids: dict[str, int] = {}
    for n, kind in enumerate(KINDS):
        ids[kind] = await ma.seed_task(
            emp, mgr, kind=kind, title=f"{kind} задача {n}", deadline=now + timedelta(days=n + 1) if kind != "overdue" else None
        )
    ids["unaccepted"] = await ma.seed_task(emp, mgr, title="Непринятая", accepted=False, deadline=now + timedelta(hours=3))
    ids["done2"] = await ma.seed_task(emp, mgr, kind="done", title="Вторая выполненная", completed_at=now - timedelta(days=3))
    ids["договор"] = await ma.seed_task(emp2, mgr, title="Анализ договоров поставки", deadline=now + timedelta(days=2))
    ids["ёлки"] = await ma.seed_task(emp3, mgr, title="Отчёт по ёлкам", expected_result="Сосчитать   ЁЛКИ в парке")
    ids["emp2-overdue"] = await ma.seed_task(emp2, mgr, kind="overdue", title="Просроченная Сидорова")
    return mgr, staff, ids


async def service_ids(ma: MiniApp, *, assignee_id: int | None, status: str, manager: bool) -> tuple[list[int], int]:
    statuses, overdue_only = api.status_filter(status, manager)
    async with ma.db() as session:
        rows = await tasks_svc.list_tasks(session, assignee_id=assignee_id, statuses=statuses, overdue_only=overdue_only)
        total = await tasks_svc.count_tasks(session, assignee_id=assignee_id, statuses=statuses, overdue_only=overdue_only)
    return [task.id for task in rows], total


# --- Списки -----------------------------------------------------------------------------------------------


async def test_lists_match_service_for_every_scope_and_status(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, emp2, _), _ = await seed_mix(ma)
    cases = [(MGR, {"scope": "all"}, None, True), (MGR, {"scope": "emp", "user_id": emp2.id}, emp2.id, True),
             (MGR, {}, None, True), (EMP, {"scope": "my"}, emp.id, False), (EMP, {}, emp.id, False)]
    for tg_id, params, assignee_id, manager in cases:
        for status in ("open", "overdue", "review", "done", "proposed", "all"):
            resp = await ma.get("/api/tasks", as_=tg_id, params={**params, "status": status, "limit": 50})
            assert resp.status == 200, resp.data
            expected, total = await service_ids(ma, assignee_id=assignee_id, status=status, manager=manager)
            assert [item["id"] for item in resp["items"]] == expected, (tg_id, params, status)
            assert resp["total"] == total and resp["pages"] == max(1, -(-total // 50))
            assert resp["truncated"] is False and "counts" not in resp.data
    # Сотрудник: «Все» — без отменённых; начальник — с ними. Отклонённые — нигде.
    all_emp = await ma.get("/api/tasks", as_=EMP, params={"status": "all", "limit": 50})
    statuses = {item["status"] for item in all_emp["items"]}
    assert "cancelled" not in statuses and "rejected" not in statuses
    all_mgr = await ma.get("/api/tasks", as_=MGR, params={"status": "all", "limit": 50})
    assert "cancelled" in {item["status"] for item in all_mgr["items"]}


async def test_default_status_is_open_and_bad_params_are_400(ma: MiniApp, frozen: Any) -> None:
    await seed_mix(ma)
    resp = await ma.get("/api/tasks", as_=MGR)
    assert {item["status"] for item in resp["items"]} <= {"active", "rework"}
    for params in ({"scope": "x"}, {"status": "x"}, {"page": "-1"}, {"page": "a"}, {"limit": "0"}, {"limit": "51"},
                   {"scope": "emp"}, {"scope": "emp", "user_id": "x"}, {"counts": "yes"}, {"q": "я" * 101}):
        resp = await ma.get("/api/tasks", as_=MGR, params=params)
        assert resp.status == 400 and resp.code == "bad_request", params


async def test_pagination_and_page_beyond_end(ma: MiniApp, frozen: Any) -> None:
    await seed_mix(ma)
    expected, total = await service_ids(ma, assignee_id=None, status="all", manager=True)
    seen: list[int] = []
    for page in range(0, 10):
        resp = await ma.get("/api/tasks", as_=MGR, params={"status": "all", "limit": 3, "page": page})
        assert resp["total"] == total and resp["pages"] == -(-total // 3) and resp["page"] == page
        if not resp["items"]:
            break
        seen += [item["id"] for item in resp["items"]]
    assert seen == expected
    resp = await ma.get("/api/tasks", as_=MGR, params={"status": "all", "limit": 3, "page": 100})
    assert resp["items"] == [] and resp["total"] == total


async def test_tab_counts(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _, _), _ = await seed_mix(ma)
    for tg_id, assignee_id, manager in ((MGR, None, True), (EMP, emp.id, False)):
        resp = await ma.get("/api/tasks", as_=tg_id, params={"counts": 1, "q": "задача"})
        counts = resp["counts"]
        assert set(counts) == {"open", "overdue", "review", "done", "proposed", "all"}
        for status, value in counts.items():
            _, total = await service_ids(ma, assignee_id=assignee_id, status=status, manager=manager)
            assert value == total, (tg_id, status)


async def test_search_cyrillic_case_yo_assignee_and_number(ma: MiniApp, frozen: Any) -> None:
    _, _, ids = await seed_mix(ma)

    async def found(q: str, **params: Any) -> list[int]:
        resp = await ma.get("/api/tasks", as_=MGR, params={"status": "all", "q": q, **params})
        assert resp.status == 200, resp.data
        return [item["id"] for item in resp["items"]]

    assert ids["договор"] in await found("ДОГОВОР")
    assert ids["ёлки"] in await found("елкам")
    assert ids["ёлки"] in await found("сосчитать ёлки")  # ожидаемый результат, пробелы схлопнуты
    assert set(await found("сидоров")) >= {ids["договор"], ids["emp2-overdue"]}
    assert ids["договор"] in await found(f"#{ids['договор']}")
    assert ids["договор"] in await found(str(ids["договор"]))
    assert await found("нетакогослова") == []
    # 1 символ — как пустой запрос (весь список)
    resp = await ma.get("/api/tasks", as_=MGR, params={"status": "all", "q": "а", "limit": 50})
    assert resp["total"] == (await service_ids(ma, assignee_id=None, status="all", manager=True))[1]
    # Поиск — в пределах вкладки и scope
    assert await found("ПОСТАВКИ", scope="emp", user_id=(await ma.h.get_user(EMP)).id) == []
    assert await found("ПОСТАВКИ", scope="emp", user_id=(await ma.h.get_user(EMP2)).id) == [ids["договор"]]


async def test_search_truncated_when_scan_limit_reached(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed_mix(ma)
    monkeypatch.setattr(api, "SEARCH_SCAN_LIMIT", 2)
    resp = await ma.get("/api/tasks", as_=MGR, params={"status": "all", "q": "задача"})
    assert resp["truncated"] is True and resp["total"] <= 2
    monkeypatch.setattr(api, "SEARCH_SCAN_LIMIT", 2000)
    resp = await ma.get("/api/tasks", as_=MGR, params={"status": "all", "q": "задача"})
    assert resp["truncated"] is False and resp["total"] == len(KINDS) - 1  # кроме отклонённой


async def test_tail_and_status_label_match_chat_task_line(ma: MiniApp, frozen: Any) -> None:
    await seed_mix(ma)
    resp = await ma.get("/api/tasks", as_=MGR, params={"status": "all", "limit": 50})
    rows = {item["id"]: item for item in resp["items"]}
    async with ma.db() as session:
        tasks = list(await session.scalars(select(Task)))
    checked = set()
    for task in tasks:
        line = plain(render.task_line(task, frozen.now))
        if task.id in rows:
            row = rows[task.id]
            assert line.endswith(f"— {row['tail']}"), (line, row["tail"])
            assert row["status_label"] == render.status_label(task, frozen.now)
            assert line.split(" ", 1)[0] == row["status_label"].split(" ", 1)[0]
            assert row["overdue"] == (task.is_open and task.deadline < frozen.now)
            checked.add(task.status)
        # Карточка (из ORM) даёт ту же строку, что и список
        card = await ma.get(f"/api/tasks/{task.id}", as_=MGR)
        assert line.endswith(f"— {card['task']['tail']}")
    assert checked >= {TaskStatus.ACTIVE, TaskStatus.REWORK, TaskStatus.SUBMITTED, TaskStatus.DONE, TaskStatus.PROPOSED,
                       TaskStatus.CANCELLED}


async def test_row_fields(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _, _), ids = await seed_mix(ma)
    resp = await ma.get("/api/tasks", as_=MGR, params={"status": "all", "limit": 50})
    rows = {item["id"]: item for item in resp["items"]}
    done = rows[ids["done"]]
    assert done["final_score"] == 110 and done["final_score_text"] == "110 %" and done["last_late"] is False
    active = rows[ids["active"]]
    assert active["final_score"] is None and active["last_late"] is None and active["accepted"] is True
    assert rows[ids["unaccepted"]]["accepted"] is False
    assert active["assignee"] == {"id": emp.id, "full_name": "Иванов Иван Иванович", "short_name": "Иванов И. И.",
                                  "position": "Юрист", "role": "employee", "status": "active"}
    assert active["priority"] == "medium" and active["priority_label"] == "🟡 Средний"
    assert active["deadline"].endswith("Z") and len(active["deadline_local"]) == 16
    assert rows[ids["overdue"]]["status_label"] == "⏰ Просрочена" and rows[ids["overdue"]]["overdue"] is True


# --- Карточка -----------------------------------------------------------------------------------------------

_BUTTONS = {
    "accept": "✅ Принял в работу",
    "submit": "📤 Сдать результат",
    "cancel": "🚫 Отменить",
    "review": "🔍 Проверить",
    "approve": "✅ Подтвердить",
    "reject": "❌ Отклонить",
}


async def test_card_actions_match_chat_keyboard(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _, _), ids = await seed_mix(ma)
    for kind, task_id in ids.items():
        for tg_id, viewer in ((MGR, mgr), (EMP, emp)):
            resp = await ma.get(f"/api/tasks/{task_id}", as_=tg_id)
            if resp.status == 403:
                continue
            actions = resp["task"]["actions"]
            async with ma.db() as session:
                task = await tasks_svc.get_task(session, task_id)
                assert task is not None
                fresh_viewer = await session.get(type(viewer), viewer.id)
                buttons = [b.text for row in keyboards.task_actions_kb(task, fresh_viewer).inline_keyboard for b in row]
            for action, text in _BUTTONS.items():
                assert actions[action] == (text in buttons), (kind, tg_id, action, buttons)
            assert actions["edit"] == ("✏️ Изменить" in buttons), (kind, tg_id)
            if actions["edit"]:
                expected = (["title", "expected_result", "plan", "deadline"] if task.status == TaskStatus.PROPOSED
                            else ["title", "expected_result", "plan", "deadline", "priority", "weight"])
                assert actions["edit_fields"] == expected
            else:
                assert actions["edit_fields"] == []
            if actions["review"]:
                assert actions["review_submission_id"] == task.last_submission.id
            assert resp["viewer"] == ("manager" if tg_id == MGR else "assignee")


async def test_card_fields(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _, _), ids = await seed_mix(ma)
    resp = await ma.get(f"/api/tasks/{ids['rework']}", as_=EMP)
    task = resp["task"]
    assert task["plan_text"] == "100 договоров" and task["plan_value"] == 100
    assert task["rework_comment"] == "Добавьте выводы" and task["attempts"] == 1
    assert task["created_by"]["id"] == mgr.id and task["manager"]["id"] == mgr.id
    assert task["deadline_label"] == render.deadline_label(await ma.task(ids["rework"]), frozen.now)
    assert task["weight_pending"] is False and task["description"] is None
    sub = resp["submissions"][0]
    assert sub["decision"] == "rework" and sub["decision_label"] == "↩️ Возвращено на доработку"
    assert sub["late_text"] == "в срок" and sub["review_comment"] == "Добавьте выводы"
    assert sub["fact_line"] == "🔢 План: 100 договоров → Факт: 110 договоров (110 %)"
    proposal = (await ma.get(f"/api/tasks/{ids['proposed']}", as_=MGR))["task"]
    assert proposal["weight_pending"] is True and proposal["manager"] is None and proposal["source"] == "employee"


# --- Видимость оценки AI ----------------------------------------------------------------------------------


async def create_and_submit(ma: MiniApp, emp: Any, deadline: str = "2026-10-09") -> tuple[int, int]:
    resp = await ma.post("/api/tasks", as_=MGR, json={
        "assignee_id": emp.id, "title": "Анализ договоров", "expected_result": "Проверить 100 договоров",
        "plan_value": 100, "plan_unit": "договоров", "deadline": deadline, "weight": 20,
    })
    assert resp.status == 201, resp.data
    task_id = resp["task"]["id"]
    from aiohttp import FormData

    form = FormData(quote_fields=False, default_to_multipart=True)
    form.add_field("fact_text", "Проверено 110 договоров")
    form.add_field("fact_value", "110")
    sub = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form)
    assert sub.status == 202, sub.data
    await ma.drain()
    return task_id, sub["submission_id"]


async def test_ai_visibility_for_assignee(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _, _) = await ma.seed_team(3)
    task_id, sub_id = await create_and_submit(ma, emp)

    mine = await ma.get(f"/api/tasks/{task_id}", as_=EMP)
    sub = mine["submissions"][-1]
    assert sub["ai"] is None and sub["ai_hidden"] is True and sub["ai_pending"] is False
    assert mine["task"]["ai_score"] is None
    assert EventType.AI_EVALUATED.value not in [ev["type"] for ev in mine["events"]]

    boss = await ma.get(f"/api/tasks/{task_id}", as_=MGR)
    bsub = boss["submissions"][-1]
    assert bsub["ai"]["score"] == 110 and bsub["ai"]["source"] == "rules"
    assert bsub["ai"]["label"] == "📐 Расчёт по правилам (AI недоступен)"
    assert bsub["ai"]["rationale"] and not bsub["ai"]["rationale"].startswith("Расчёт по правилам")
    assert boss["task"]["ai_score"] == 110 and bsub["ai_hidden"] is False
    assert EventType.AI_EVALUATED.value in [ev["type"] for ev in boss["events"]]
    assert boss["task"]["actions"]["review"] is True and boss["task"]["actions"]["review_submission_id"] == sub_id

    assert (await ma.post(f"/api/submissions/{sub_id}/confirm", as_=MGR)).status == 200
    after = await ma.get(f"/api/tasks/{task_id}", as_=EMP)
    asub = after["submissions"][-1]
    assert asub["ai"]["score"] == 110 and asub["ai"]["rationale"] is None and asub["ai"]["model"] is None
    assert asub["decision"] == "approved" and asub["decision_label"] == "✅ подтверждена"
    assert asub["final_score_text"] == "110 %" and asub["reviewer"]["full_name"] == "Петрова Анна Сергеевна"
    assert after["task"]["ai_score"] == 110
    assert EventType.AI_EVALUATED.value in [ev["type"] for ev in after["events"]]
    texts = [ev["text"] for ev in after["events"]]
    assert "оценка подтверждена: 110 %" in texts and after["events"][-1]["actor_name"] == "Петрова А. С."


async def test_ai_pending_for_manager_before_evaluation(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _, _) = await ma.seed_team(3)
    task_id = await ma.seed_task(emp, mgr, kind="submitted", ai_score=None)
    boss = await ma.get(f"/api/tasks/{task_id}", as_=MGR)
    assert boss["submissions"][0]["ai"] is None and boss["submissions"][0]["ai_pending"] is True


# --- Создание ---------------------------------------------------------------------------------------------


def body(emp: Any, **overrides: Any) -> dict[str, Any]:
    data = {"assignee_id": emp.id, "title": "Анализ договоров", "expected_result": "Проверить 100 договоров",
            "deadline": "2026-10-09", "weight": 20}
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not ...}


async def test_create_task_notifies_assignee_like_chat(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _, _) = await ma.seed_team(3)
    resp = await ma.post("/api/tasks", as_=MGR, json=body(emp, plan_value="1 200", plan_unit="договоров",
                                                          priority="high", description="своими словами"))
    assert resp.status == 201 and resp["delivered"] is True and resp["notice"] is None
    task = resp["task"]
    assert task["status"] == "active" and task["priority"] == "high" and task["weight"] == 20
    assert task["plan_value"] == 1200 and task["plan_text"] == "1200 договоров"
    assert task["description"] == "своими словами" and task["accepted"] is False
    assert task["deadline_local"] == "09.10.2026 18:00"
    assert "🆕 Вам поставлена новая задача" in ma.h.last_text(EMP)
    assert ma.h.buttons(EMP) == ["✅ Принял в работу", "📋 Открыть"]
    stored = await ma.task(task["id"])
    assert stored.created_by_id == stored.manager_id == (await ma.h.get_user(MGR)).id


async def test_create_task_when_assignee_blocked_bot(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _, _) = await ma.seed_team(3)
    ma.api.blocked_chats.add(EMP)
    resp = await ma.post("/api/tasks", as_=MGR, json=body(emp))
    assert resp.status == 201 and resp["delivered"] is False and resp["notice"] == api.NOT_DELIVERED


async def test_create_task_plan_unit_dropped_without_plan(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _, _) = await ma.seed_team(3)
    resp = await ma.post("/api/tasks", as_=MGR, json=body(emp, plan_unit="штук"))
    assert resp["task"]["plan_value"] is None and resp["task"]["plan_unit"] is None


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"title": ...}, "Название"),
        ({"title": "   "}, "Название"),
        ({"title": "я" * 256}, "Название: до 255 символов"),
        ({"title": 5}, "Название"),
        ({"expected_result": ...}, "Ожидаемый результат"),
        ({"expected_result": "я" * 2001}, "до 2000"),
        ({"description": "я" * 2001}, "Описание"),
        ({"plan_value": True}, "План"),
        ({"plan_value": -5}, "План"),
        ({"plan_value": "много"}, "План"),
        ({"plan_value": 1e16}, "План"),
        ({"plan_value": 5, "plan_unit": "я" * 65}, "Единица плана"),
        ({"deadline": ...}, "Срок"),
        ({"deadline": "завтра"}, "Срок"),
        ({"deadline": 20261009}, "Срок"),
        ({"deadline": "2026-13-40"}, "Срок"),
        ({"weight": ...}, "Вес"),
        ({"weight": 0}, "Вес"),
        ({"weight": 101}, "Вес"),
        ({"weight": 1.5}, "Вес"),
        ({"weight": "20"}, "Вес"),
        ({"weight": True}, "Вес"),
        ({"priority": "urgent"}, "Приоритет"),
        ({"assignee_id": ...}, "Сотрудник"),
        ({"assignee_id": "5"}, "Сотрудник"),
        ({"extra": 1}, "Неизвестное поле «extra»"),
    ],
)
async def test_create_task_validation(ma: MiniApp, frozen: Any, overrides: dict[str, Any], fragment: str) -> None:
    _, (emp, _, _) = await ma.seed_team(3)
    resp = await ma.post("/api/tasks", as_=MGR, json=body(emp, **overrides))
    assert resp.status == 400 and resp.code == "bad_request", resp.data
    assert fragment in resp.error
    assert ma.h.last_text(EMP) is None


async def test_create_task_domain_errors(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, emp2, _) = await ma.seed_team(3)
    resp = await ma.post("/api/tasks", as_=MGR, json=body(emp, deadline="2026-10-01"))
    assert resp.status == 400 and resp.code == "domain" and resp.error == "Срок должен быть в будущем"
    resp = await ma.post("/api/tasks", as_=MGR, json=body(emp, deadline="2026-10-02T11:00"))  # 11:00 местного < 12:00
    assert resp.status == 400 and resp.error == "Срок должен быть в будущем"
    async with ma.db() as session:
        user = await session.get(type(emp2), emp2.id)
        user.status = "blocked"
        await session.commit()
    resp = await ma.post("/api/tasks", as_=MGR, json=body(emp2))
    assert resp.status == 400 and resp.error == "Исполнителем может быть только активный сотрудник"
    resp = await ma.post("/api/tasks", as_=MGR, json=body(mgr))
    assert resp.status == 400 and resp.code == "domain"


async def test_create_task_deadline_formats(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _, _) = await ma.seed_team(3)
    cases = {"2026-10-09": "09.10.2026 18:00", "2026-10-09T10:30": "09.10.2026 10:30",
             "2026-10-09T10:30:00": "09.10.2026 10:30", "2026-10-09T05:30:00Z": "09.10.2026 10:30",
             "2026-10-09T10:30:00+05:00": "09.10.2026 10:30"}
    for value, local in cases.items():
        resp = await ma.post("/api/tasks", as_=MGR, json=body(emp, deadline=value))
        assert resp.status == 201, (value, resp.data)
        assert resp["task"]["deadline_local"] == local, value


async def test_create_task_busy_gate(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _, _) = await ma.seed_team(3)
    tg = (await ma.h.get_user(MGR)).tg_id
    assert ma.ctx.gate.try_enter("create", tg)
    try:
        resp = await ma.post("/api/tasks", as_=MGR, json=body(emp))
        assert resp.status == 429 and resp.code == "busy" and resp.error == "⏳ Задача уже создаётся…"
    finally:
        ma.ctx.gate.leave("create", tg)
    assert (await ma.post("/api/tasks", as_=MGR, json=body(emp))).status == 201


# --- Правка -----------------------------------------------------------------------------------------------


async def test_patch_active_task_all_fields(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _, _) = await ma.seed_team(3)
    task_id = await ma.seed_task(emp, mgr)
    resp = await ma.patch(f"/api/tasks/{task_id}", as_=MGR, json={
        "title": "Новое название", "expected_result": "Новый результат", "description": "слова",
        "plan_value": 50, "plan_unit": "актов", "deadline": "2026-10-20", "priority": "low", "weight": 30,
    })
    assert resp.status == 200, resp.data
    assert set(resp["changed"]) == {"title", "expected_result", "description", "plan_value", "plan_unit",
                                    "deadline", "priority", "weight"}
    assert resp["delivered"] is True and resp["notice"] is None
    task = resp["task"]
    assert (task["title"], task["weight"], task["priority"], task["plan_text"]) == ("Новое название", 30, "low", "50 актов")
    text = ma.h.last_text(EMP)
    assert "✏️ Начальник изменил задачу" in text and "→" in text and "Новое название" in text
    events = await ma.h.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.type == EventType.EDITED))
    assert len(events) == 1


async def test_patch_rules(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _, _) = await ma.seed_team(3)
    task_id = await ma.seed_task(emp, mgr)
    proposal_id = await ma.seed_task(emp, mgr, kind="proposed")
    done_id = await ma.seed_task(emp, mgr, kind="done")

    resp = await ma.patch(f"/api/tasks/{task_id}", as_=MGR, json={})
    assert resp.status == 400 and resp.error == api.NO_CHANGES
    resp = await ma.patch(f"/api/tasks/{proposal_id}", as_=MGR, json={"weight": 20})
    assert resp.status == 400 and resp.code == "domain" and resp.error == api.PROPOSAL_FIELDS
    resp = await ma.patch(f"/api/tasks/{proposal_id}", as_=MGR, json={"title": "Поручение (уточнено)"})
    assert resp.status == 200 and resp["changed"] == ["title"]
    assert "✏️ Начальник скорректировал ваше поручение" in ma.h.last_text(EMP)
    resp = await ma.patch(f"/api/tasks/{task_id}", as_=MGR, json={"plan_value": None})
    assert resp.status == 200 and set(resp["changed"]) == {"plan_value", "plan_unit"}
    assert resp["task"]["plan_value"] is None and resp["task"]["plan_unit"] is None
    resp = await ma.patch(f"/api/tasks/{task_id}", as_=MGR, json={"title": "Анализ договоров"})
    assert resp.status == 200 and resp["changed"] == [] and resp["delivered"] is None and resp["notice"] is None
    resp = await ma.patch(f"/api/tasks/{task_id}", as_=MGR, json={"deadline": "2026-09-01"})
    assert resp.status == 400 and resp.error == "Срок должен быть в будущем"
    resp = await ma.patch(f"/api/tasks/{task_id}", as_=MGR, json={"title": ""})
    assert resp.status == 400 and resp.code == "bad_request"
    resp = await ma.patch(f"/api/tasks/{done_id}", as_=MGR, json={"title": "x"})
    assert resp.status == 400 and resp.code == "domain"


# --- Принятие и отмена ------------------------------------------------------------------------------------


async def test_accept_task_and_repeat(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _, _) = await ma.seed_team(3)
    task_id = await ma.seed_task(emp, mgr, accepted=False)
    before = len(ma.api.calls)
    resp = await ma.post(f"/api/tasks/{task_id}/accept", as_=EMP)
    assert resp.status == 200 and resp["task"]["accepted"] is True and resp["task"]["actions"]["accept"] is False
    assert resp["task"]["accepted_at"] is not None
    assert len(ma.api.calls) == before  # уведомлений нет (как в чате)
    again = await ma.post(f"/api/tasks/{task_id}/accept", as_=EMP)
    assert again.status == 200 and again["task"]["accepted_at"] == resp["task"]["accepted_at"]
    done_id = await ma.seed_task(emp, mgr, kind="cancelled", accepted=False)
    resp = await ma.post(f"/api/tasks/{done_id}/accept", as_=EMP)
    assert resp.status == 400 and resp.code == "domain"


async def test_cancel_task_notifies_and_repeat_fails(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _, _) = await ma.seed_team(3)
    task_id = await ma.seed_task(emp, mgr)
    resp = await ma.post(f"/api/tasks/{task_id}/cancel", as_=MGR, json={"reason": "Не актуально"})
    assert resp.status == 200 and resp["task"]["status"] == "cancelled" and resp["delivered"] is True
    text = ma.h.last_text(EMP)
    assert "🚫 Задача отменена начальником" in text and "Не актуально" in text
    again = await ma.post(f"/api/tasks/{task_id}/cancel", as_=MGR)
    assert again.status == 400 and again.code == "domain"
    resp = await ma.post(f"/api/tasks/{task_id}/cancel", as_=MGR, json={"reason": "я" * 1001})
    assert resp.status == 400 and resp.code == "bad_request"
    other = await ma.seed_task(emp, mgr, title="Без причины")
    ma.api.blocked_chats.add(EMP)
    resp = await ma.post(f"/api/tasks/{other}/cancel", as_=MGR)
    assert resp.status == 200 and resp["delivered"] is False and resp["notice"] == api.NOT_DELIVERED


async def test_texts_match_chat_constants() -> None:
    from bot import main as bot_main
    from bot.handlers import common, dashboard, start, task_submit

    assert api.NO_RIGHTS == common.NO_RIGHTS
    assert api.NOT_DELIVERED == common.NOT_DELIVERED
    assert api.TXT_PENDING == start.TXT_PENDING and api.TXT_BLOCKED == start.TXT_BLOCKED
    assert api.GENERIC_ERROR == bot_main.GENERIC_ERROR
    assert api.EXPORT_FAILED == dashboard.EXPORT_FAILED and api.EXPORT_SEND_FAILED == dashboard.EXPORT_SEND_FAILED
    assert api.MAX_BACK_OFFSET == dashboard.MAX_BACK_OFFSET and api.HISTORY_PAGE_SIZE == dashboard.HISTORY_PAGE_SIZE
    assert api.NOT_OPEN_TEXTS == task_submit._NOT_OPEN_TEXTS  # noqa: SLF001
    assert api.WEIGHT_OPTIONS == keyboards._WEIGHT_OPTIONS and api.SCORE_OPTIONS == keyboards._SCORE_OPTIONS  # noqa: SLF001
    assert api.FACT_MIN == task_submit.MIN_FACT and api.TEXT_MAX == task_submit.MAX_TEXT
    assert api.NOTES_MAX == task_submit.MAX_NOTES


async def test_employee_my_list_and_ignores_other_people(ma: MiniApp, frozen: Any) -> None:
    _, _, ids = await seed_mix(ma)
    resp = await ma.get("/api/tasks", as_=EMP2, params={"status": "all"})
    assert {item["id"] for item in resp["items"]} == {ids["договор"], ids["emp2-overdue"]}
    resp = await ma.get("/api/tasks", as_=EMP3, params={"status": "all", "q": "поставки"})
    assert resp["items"] == []
