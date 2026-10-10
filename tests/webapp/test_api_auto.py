"""Автоподтверждение в приложении (SPEC.md §12.2, docs/MINIAPP_SPEC.md §8.1): строка ``auto_note`` для
начальника, действие ``revise`` и ``POST /api/submissions/{id}/revise`` — изменить оценку, которую подтвердил бот."""

from __future__ import annotations

from typing import Any

from bot.db.models import ReviewDecision, TaskStatus
from bot.services import tasks as tasks_svc

from .conftest import EMP, MGR, MiniApp


async def ai_scored(ma: MiniApp, emp: Any, mgr: Any, *, score: float = 95.0, source: str = "ai") -> tuple[int, int]:
    """Сданная задача с предварительной оценкой (по умолчанию — от AI)."""
    task_id = await ma.seed_task(emp, mgr, kind="submitted", ai_score=score)
    async with ma.db() as session:
        task = await tasks_svc.get_task(session, task_id)
        sub = task.last_submission
        sub.ai_source, sub.ai_rationale = source, "План выполнен на 95 %."
        await session.commit()
        return task_id, sub.id


async def auto_confirmed(ma: MiniApp, emp: Any, mgr: Any) -> tuple[int, int]:
    task_id, sub_id = await ai_scored(ma, emp, mgr)
    async with ma.db() as session:
        await tasks_svc.review_auto_confirm(session, sub_id)
        await session.commit()
    return task_id, sub_id


async def test_manager_sees_when_score_will_be_confirmed(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _) = await ma.seed_team(2)
    by_ai, _ = await ai_scored(ma, emp, mgr)
    by_rules, _ = await ai_scored(ma, emp, mgr, source="rules")
    too_high, _ = await ai_scored(ma, emp, mgr, score=120.0)

    note = (await ma.get(f"/api/tasks/{by_ai}", as_=MGR))["task"]["auto_note"]
    assert "подтвердится автоматически" in note
    assert "рассчитана без AI" in (await ma.get(f"/api/tasks/{by_rules}", as_=MGR))["task"]["auto_note"]
    assert "выше 100 %" in (await ma.get(f"/api/tasks/{too_high}", as_=MGR))["task"]["auto_note"]
    assert (await ma.get(f"/api/tasks/{by_ai}", as_=EMP))["task"]["auto_note"] is None


async def test_proposal_shows_suggested_weight_and_auto_time(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_team(2)
    created = await ma.post("/api/proposals", as_=EMP, json={
        "title": "Подготовить справку", "expected_result": "Справка по 5 договорам", "deadline": "2026-10-09",
    })
    assert created.status == 201, created.data
    task = (await ma.get(f"/api/tasks/{created['task']['id']}", as_=MGR))["task"]
    assert task["weight_pending"] is True and task["weight"] == 10
    assert "будет принято автоматически 03.10 в 12:00" in task["auto_note"] and "с весом 10 %" in task["auto_note"]
    card = ma.h.last_text(MGR)
    assert "Предлагаемый вес: 10 %" in card and "будет принято автоматически 03.10 в 12:00" in card


async def test_card_offers_revise_after_auto_confirmation(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _) = await ma.seed_team(2)
    task_id, sub_id = await auto_confirmed(ma, emp, mgr)

    card = await ma.get(f"/api/tasks/{task_id}", as_=MGR)
    actions = card["task"]["actions"]
    assert actions["revise"] is True and actions["revise_submission_id"] == sub_id
    assert actions["revise_until_text"] == "09.10 в 12:00"  # 7 дней после подтверждения
    last = card["submissions"][-1]
    assert last["auto_confirmed"] is True and last["decision_label"] == "⏱ подтверждена автоматически"
    assert last["reviewer"] is None and last["final_score"] == 95

    mine = await ma.get(f"/api/tasks/{task_id}", as_=EMP)
    assert mine["task"]["actions"]["revise"] is False
    assert mine["submissions"][-1]["decision_label"] == "⏱ подтверждена автоматически"


async def test_revise_changes_score_once_and_notifies_employee(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _) = await ma.seed_team(2)
    task_id, sub_id = await auto_confirmed(ma, emp, mgr)

    resp = await ma.post(f"/api/submissions/{sub_id}/revise", as_=MGR, json={"score": 70, "comment": "Отчёт неполный"})
    assert resp.status == 200, resp.data
    assert resp["task"]["final_score"] == 70 and resp["task"]["actions"]["revise"] is False
    assert resp["delivered"] is True
    text = ma.h.last_text(EMP)
    assert "70 %" in text and "Отчёт неполный" in text
    task = await ma.task(task_id)
    assert task.status == TaskStatus.DONE and task.final_score == 70
    assert task.submissions[-1].decision == ReviewDecision.CHANGED

    again = await ma.post(f"/api/submissions/{sub_id}/revise", as_=MGR, json={"score": 60})
    assert again.status == 400 and "автоматически подтверждённую" in again.error


async def test_revise_is_refused_for_manager_decisions_late_and_employees(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _) = await ma.seed_team(2)
    _, manual = await ai_scored(ma, emp, mgr)
    assert (await ma.post(f"/api/submissions/{manual}/confirm", as_=MGR)).status == 200
    assert (await ma.post(f"/api/submissions/{manual}/revise", as_=MGR, json={"score": 60})).status == 400

    task_id, sub_id = await auto_confirmed(ma, emp, mgr)
    assert (await ma.post(f"/api/submissions/{sub_id}/revise", as_=EMP, json={"score": 150})).status == 403
    assert (await ma.post(f"/api/submissions/{sub_id}/revise", as_=MGR, json={"score": 151})).status == 400
    frozen.advance(days=7, minutes=5)
    late = await ma.post(f"/api/submissions/{sub_id}/revise", as_=MGR, json={"score": 60})
    assert late.status == 400 and "7 дней" in late.error
    assert (await ma.get(f"/api/tasks/{task_id}", as_=MGR))["task"]["actions"]["revise"] is False
    assert (await ma.task(task_id)).final_score == 95
