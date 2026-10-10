"""Проверка результатов через API: очередь, подтверждение, своя оценка, доработка, гонки, файлы
(docs/MINIAPP_SPEC.md §8.7, §12.2 test_review_api)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiohttp import FormData

from bot.db.models import ReviewDecision, Role, TaskStatus
from bot.webapp import api

from .conftest import EMP, EMP2, MGR, MiniApp


async def submitted_via_api(ma: MiniApp, emp: Any, *, deadline: str = "2026-10-09", files: int = 0) -> tuple[int, int]:
    """Начальник ставит задачу, сотрудник сдаёт через API (правила: 110 %), начальнику пришёл результат."""
    resp = await ma.post("/api/tasks", as_=MGR, json={
        "assignee_id": emp.id, "title": "Анализ договоров", "expected_result": "Проверить 100 договоров",
        "plan_value": 100, "plan_unit": "договоров", "deadline": deadline, "weight": 20,
    })
    assert resp.status == 201, resp.data
    task_id = resp["task"]["id"]
    form = FormData(quote_fields=False, default_to_multipart=True)
    form.add_field("fact_text", "Проверено 110 договоров")
    form.add_field("fact_value", "110")
    for n in range(files):
        form.add_field("files", f"содержимое {n}".encode(), filename=f"Отчёт {n}.pdf", content_type="application/pdf")
    sub = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP if emp.tg_id == EMP else emp.tg_id, data=form)
    assert sub.status == 202, sub.data
    await ma.drain()
    return task_id, sub["submission_id"]


async def test_review_queue_order_and_manager_view(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, emp2) = await ma.seed_team(2)
    first = await ma.seed_task(emp2, mgr, kind="submitted", title="Давняя", deadline=frozen.now + timedelta(days=1))
    second = await ma.seed_task(emp, mgr, kind="submitted", title="Свежая", deadline=frozen.now + timedelta(days=3), files=2)
    await ma.seed_task(emp, mgr, kind="done")
    resp = await ma.get("/api/review", as_=MGR)
    assert resp.status == 200
    assert [item["task"]["id"] for item in resp["items"]] == [first, second]
    item = resp["items"][1]
    assert item["submission"]["ai"]["score"] == 110 and item["submission"]["ai"]["rationale"]
    assert [a["name"] for a in item["submission"]["attachments"]] == ["report0.pdf", "report1.pdf"]
    assert item["task"]["actions"]["review"] is True


async def test_confirm_sets_done_and_notifies_employee(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _) = await ma.seed_team(2)
    task_id, sub_id = await submitted_via_api(ma, emp)
    resp = await ma.post(f"/api/submissions/{sub_id}/confirm", as_=MGR)
    assert resp.status == 200, resp.data
    assert resp["task"]["status"] == "done" and resp["task"]["final_score"] == 110
    assert resp["delivered"] is True and resp["notice"] is None
    text = ma.h.last_text(EMP)
    assert f"🏁 Результат по задаче #{task_id} оценён" in text and "110 %" in text
    task = await ma.task(task_id)
    assert task.status == TaskStatus.DONE and task.submissions[-1].decision == ReviewDecision.APPROVED


async def test_score_boundaries_and_types(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _) = await ma.seed_team(2)
    _, sub_id = await submitted_via_api(ma, emp)
    for bad in (True, "100", None, -1, 151, 1e309):
        resp = await ma.post(f"/api/submissions/{sub_id}/score", as_=MGR, json={"score": bad})
        assert resp.status == 400 and resp.code == "bad_request", bad
    resp = await ma.post(f"/api/submissions/{sub_id}/score", as_=MGR, json={"score": 150, "comment": "Отлично"})
    assert resp.status == 200 and resp["task"]["final_score"] == 150
    text = ma.h.last_text(EMP)
    assert "150 %" in text and "Отлично" in text and "✏️ Оценку выставил начальник." in text

    _, sub2 = await submitted_via_api(ma, emp)
    resp = await ma.post(f"/api/submissions/{sub2}/score", as_=MGR, json={"score": 0})
    assert resp.status == 200 and resp["task"]["final_score"] == 0
    resp = await ma.post(f"/api/submissions/{sub2}/score", as_=MGR, json={"score": 50, "comment": "я" * 2001})
    assert resp.status == 400


async def test_rework_rules_and_notification(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _) = await ma.seed_team(2)
    _, sub_id = await submitted_via_api(ma, emp)
    resp = await ma.post(f"/api/submissions/{sub_id}/rework", as_=MGR, json={})
    assert resp.status == 400 and resp.code == "bad_request"
    resp = await ma.post(f"/api/submissions/{sub_id}/rework", as_=MGR, json={"comment": "Добавьте выводы"})
    assert resp.status == 200, resp.data
    assert resp["task"]["status"] == "rework" and resp["task"]["rework_count"] == 1
    assert "↩️ Задача" in ma.h.last_text(EMP) and "Добавьте выводы" in ma.h.last_text(EMP)
    assert ma.h.buttons(EMP) == ["📤 Сдать результат", "📋 Открыть"]

    # Срок уже прошёл: без нового срока — нельзя, с новым — можно.
    late_task = await ma.seed_task(emp, mgr, kind="submitted", deadline=frozen.now - timedelta(days=1))
    late_sub = (await ma.task(late_task)).submissions[0].id
    resp = await ma.post(f"/api/submissions/{late_sub}/rework", as_=MGR, json={"comment": "Доработать"})
    assert resp.status == 400 and resp.code == "domain" and resp.error == api.DEADLINE_PASSED
    resp = await ma.post(f"/api/submissions/{late_sub}/rework", as_=MGR,
                         json={"comment": "Доработать", "deadline": "2026-10-12T15:00"})
    assert resp.status == 200 and resp["task"]["deadline_local"] == "12.10.2026 15:00"
    assert (await ma.task(late_task)).status == TaskStatus.REWORK


async def test_second_confirm_and_chat_after_api_are_refused(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _) = await ma.seed_team(2)
    task_id, sub_id = await submitted_via_api(ma, emp)
    review = ma.h.find_message(MGR, f"Результат по задаче #{task_id}")
    assert "✅ Подтвердить 110 %" in ma.h.buttons(MGR, review.message_id)

    assert (await ma.post(f"/api/submissions/{sub_id}/confirm", as_=MGR)).status == 200
    again = await ma.post(f"/api/submissions/{sub_id}/confirm", as_=MGR)
    assert again.status == 400 and again.code == "domain" and again.error.startswith("Результат уже обработан")
    other = await ma.post(f"/api/submissions/{sub_id}/score", as_=MGR, json={"score": 90})
    assert other.status == 400 and other.error.startswith("Результат уже обработан")

    # Начальник жмёт «✅ Подтвердить» в старом сообщении чата — решение уже принято в приложении.
    log = await ma.h.press_button(MGR, "Подтвердить 110 %", review.message_id)
    assert "уже обработан" in (log.alert or "").lower()
    task = await ma.task(task_id)
    assert task.status == TaskStatus.DONE and task.final_score == 110 and len(task.submissions) == 1


async def test_chat_confirm_first_then_api_is_refused(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _) = await ma.seed_team(2)
    task_id, sub_id = await submitted_via_api(ma, emp)
    await ma.h.press_button(MGR, "Подтвердить 110 %")
    resp = await ma.post(f"/api/submissions/{sub_id}/rework", as_=MGR, json={"comment": "Поздно"})
    assert resp.status == 400 and resp.error.startswith("Результат уже обработан")
    assert (await ma.task(task_id)).status == TaskStatus.DONE


async def test_own_task_and_missing_ai_score(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, emp2) = await ma.seed_team(2)
    task_id = await ma.seed_task(emp2, mgr, kind="submitted", ai_score=None)
    sub_id = (await ma.task(task_id)).submissions[0].id
    resp = await ma.post(f"/api/submissions/{sub_id}/confirm", as_=MGR)
    assert resp.status == 400 and resp.error == "Предварительной оценки нет — введите оценку вручную"
    assert (await ma.post(f"/api/submissions/{sub_id}/score", as_=MGR, json={"score": 95})).status == 200

    # Исполнителя повысили до начальника: оценивать собственную задачу нельзя.
    own = await ma.seed_task(emp, mgr, kind="submitted")
    own_sub = (await ma.task(own)).submissions[0].id
    async with ma.db() as session:
        user = await session.get(type(emp), emp.id)
        user.role = Role.MANAGER
        await session.commit()
    resp = await ma.post(f"/api/submissions/{own_sub}/confirm", as_=EMP)
    assert resp.status == 400 and resp.error == "Нельзя оценивать результат собственной задачи"


async def test_files_are_sent_to_manager_chat(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _) = await ma.seed_team(2)
    task_id = await ma.seed_task(emp, mgr, kind="submitted", files=2)
    sub_id = (await ma.task(task_id)).submissions[0].id
    before = len(ma.h.files_sent(MGR))
    resp = await ma.post(f"/api/submissions/{sub_id}/files", as_=MGR)
    assert resp.status == 202 and resp["count"] == 2
    await ma.drain()
    sent = ma.h.files_sent(MGR)[before:]
    assert [f.file_id for f in sent] == [f"doc-{emp.id}-0", f"doc-{emp.id}-1"]
    assert not ma.ctx.gate.busy("files", MGR)

    empty = await ma.seed_task(emp, mgr, kind="submitted", title="Без файлов")
    empty_sub = (await ma.task(empty)).submissions[0].id
    resp = await ma.post(f"/api/submissions/{empty_sub}/files", as_=MGR)
    assert resp.status == 400 and resp.error == api.NO_FILES


async def test_files_busy_until_background_finishes(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from bot import notify

    mgr, (emp, _) = await ma.seed_team(2)
    task_id = await ma.seed_task(emp, mgr, kind="submitted", files=1)
    sub_id = (await ma.task(task_id)).submissions[0].id
    release = asyncio.Event()
    original = notify.send_attachments

    async def slow(bot: Any, chat_id: int, sub: Any) -> None:
        await release.wait()
        await original(bot, chat_id, sub)

    monkeypatch.setattr(notify, "send_attachments", slow)
    assert (await ma.post(f"/api/submissions/{sub_id}/files", as_=MGR)).status == 202
    busy = await ma.post(f"/api/submissions/{sub_id}/files", as_=MGR)
    assert busy.status == 429 and busy.error == "⏳ Файлы уже отправляются в чат."
    release.set()
    await ma.drain()
    assert (await ma.post(f"/api/submissions/{sub_id}/files", as_=MGR)).status == 202


async def test_review_blocked_employee_gets_notice(ma: MiniApp, frozen: Any) -> None:
    _, (emp, _) = await ma.seed_team(2)
    _, sub_id = await submitted_via_api(ma, emp)
    ma.api.blocked_chats.add(EMP)
    resp = await ma.post(f"/api/submissions/{sub_id}/confirm", as_=MGR)
    assert resp.status == 200 and resp["delivered"] is False and resp["notice"] == api.NOT_DELIVERED
    assert EMP2  # второй сотрудник не участвует
