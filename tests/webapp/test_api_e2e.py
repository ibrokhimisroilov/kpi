"""Полный цикл «приложение + чат» на одном боте и одной базе (docs/MINIAPP_SPEC.md §12.2, e2e):
действия в приложении видны в чате и наоборот, уведомления — те же."""

from __future__ import annotations

from typing import Any

from aiohttp import FormData
from sqlalchemy import select

from bot.db.models import Submission, Task, TaskStatus
from bot.ui.texts import BTN_NEW_TASK, BTN_SUBMIT

from .conftest import EMP, MGR, MiniApp

PDF = b"%PDF-1.4 report"


def submission_form(files: int = 1) -> FormData:
    form = FormData(quote_fields=False, default_to_multipart=True)
    form.add_field("fact_text", "Проверено 110 договоров, в 12 выявлены нарушения")
    form.add_field("result_text", "Подготовлен отчёт")
    form.add_field("fact_value", "110")
    for n in range(files):
        form.add_field("files", PDF, filename=f"Отчёт {n}.pdf", content_type="application/pdf")
    return form


async def start_both(ma: MiniApp) -> tuple[Any, Any]:
    mgr, (emp, _) = await ma.seed_team(2)
    await ma.h.send_command(MGR, "start")
    await ma.h.send_command(EMP, "start")
    return mgr, emp


async def test_full_cycle_app_and_chat(ma: MiniApp, frozen: Any) -> None:
    _, emp = await start_both(ma)
    h = ma.h

    # 1. Руководитель ставит задачу в приложении -> сотруднику в чат пришла карточка.
    created = await ma.post("/api/tasks", as_=MGR, json={
        "assignee_id": emp.id, "title": "Анализ договоров", "expected_result": "Проверить 100 договоров",
        "plan_value": 100, "plan_unit": "договоров", "deadline": "2026-10-03", "weight": 20,
    })
    assert created.status == 201
    task_id = created["task"]["id"]
    assert "🆕 Вам поставлена новая задача" in h.last_text(EMP)

    # 2. Сотрудник нажимает «✅ Принял в работу» в чате -> карточка в приложении: принята.
    await h.press_button(EMP, "Принял в работу")
    card = await ma.get(f"/api/tasks/{task_id}", as_=EMP)
    assert card["task"]["accepted"] is True and card["task"]["actions"]["accept"] is False
    assert "accepted" in [ev["type"] for ev in card["events"]]

    # 3. Сдаёт в приложении (1 файл) -> руководителю в чат пришёл результат с кнопками и файлом.
    submitted = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=submission_form())
    assert submitted.status == 202
    await ma.drain()
    review = h.find_message(MGR, f"Результат по задаче #{task_id}")
    assert "✅ Подтвердить 110 %" in review.button_texts
    assert [f.file_name for f in h.files_sent(MGR)] == ["Отчёт 0.pdf"]

    # 4. Руководитель подтверждает в приложении -> сотруднику пришла оценка.
    confirmed = await ma.post(f"/api/submissions/{submitted['submission_id']}/confirm", as_=MGR)
    assert confirmed.status == 200 and confirmed["task"]["final_score"] == 110
    assert f"🏁 Результат по задаче #{task_id} оценён" in h.last_text(EMP)

    # 5. KPI: дашборд и карточка сотрудника — 110 %, задача в истории.
    dash = await ma.get("/api/dashboard", as_=MGR)
    row = next(r for r in dash["rows"] if r["user"]["id"] == emp.id)
    assert row["kpi"] == 110 and row["kpi_text"] == "110 %" and dash["team"]["kpi_text"] == "110 %"
    card = await ma.get(f"/api/users/{emp.id}/kpi", as_=MGR)
    assert card["current"]["kpi_text"] == "110 %"
    assert [item["task_id"] for item in card["history"]["items"]] == [task_id]
    mine = await ma.get("/api/dashboard", as_=EMP)
    assert mine["current"]["kpi"] == 110 and mine["week"]["kpi"] == 110


async def test_task_created_in_chat_is_visible_in_app_and_reviewed_there(ma: MiniApp) -> None:
    _, emp = await start_both(ma)
    h = ma.h
    await h.press_menu(MGR, BTN_NEW_TASK)
    await h.press_button(MGR, "Иванов")
    await h.send_text(MGR, "Провести анализ договоров")
    await h.send_text(MGR, "проверить 100 договоров и представить отчёт")
    await h.press_button(MGR, "Принять")
    if "Плановое число" in (h.last_text(MGR) or ""):
        await h.press_button(MGR, "Пропустить")
    await h.press_button(MGR, "Завтра")
    await h.press_button(MGR, "Средний")
    await h.press_button(MGR, "20 %")
    await h.press_button(MGR, "Создать")
    task = (await h.scalars(select(Task)))[0]

    listed = await ma.get("/api/tasks", as_=EMP)
    assert [item["id"] for item in listed["items"]] == [task.id]
    assert listed["items"][0]["title"] == "Провести анализ договоров"

    # Сдача через чат -> проверка через приложение.
    await h.press_menu(EMP, BTN_SUBMIT)
    await h.press_button(EMP, "Провести анализ договоров")
    await h.send_text(EMP, "Проверено 110 договоров")
    await h.send_text(EMP, "Отчёт готов")
    if "Фактическое значение" in (h.last_text(EMP) or ""):
        await h.send_text(EMP, "110")
    await h.press_button(EMP, "Без файлов")
    await h.press_button(EMP, "Отправить")
    sub = (await h.scalars(select(Submission)))[0]
    queue = await ma.get("/api/review", as_=MGR)
    assert [item["submission"]["id"] for item in queue["items"]] == [sub.id]
    resp = await ma.post(f"/api/submissions/{sub.id}/score", as_=MGR, json={"score": 95, "comment": "Хорошо"})
    assert resp.status == 200 and resp["task"]["final_score"] == 95
    assert "95 %" in h.last_text(EMP) and "Хорошо" in h.last_text(EMP)


async def test_api_submission_reviewed_in_chat(ma: MiniApp, frozen: Any) -> None:
    mgr, emp = await start_both(ma)
    task_id = await ma.seed_task(emp, mgr)
    assert (await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=submission_form(files=0))).status == 202
    await ma.drain()
    await ma.h.press_button(MGR, "Подтвердить 110 %")
    task = await ma.task(task_id)
    assert task.status == TaskStatus.DONE and task.final_score == 110
    card = await ma.get(f"/api/tasks/{task_id}", as_=EMP)
    assert card["submissions"][-1]["decision"] == "approved" and card["submissions"][-1]["ai"]["score"] == 110


async def test_proposal_from_app_approved_in_chat(ma: MiniApp, frozen: Any) -> None:
    await start_both(ma)
    h = ma.h
    created = await ma.post("/api/proposals", as_=EMP, json={
        "title": "Подготовить справку", "expected_result": "Справка по 5 договорам", "deadline": "2026-10-09",
    })
    assert created.status == 201 and created["notified"] == 1
    await h.press_button(MGR, "Подтвердить")
    await h.press_button(MGR, "20 %")
    await h.press_button(MGR, "Средний")
    task = await ma.task(created["task"]["id"])
    assert task.status == TaskStatus.ACTIVE and task.weight == 20
    card = await ma.get(f"/api/tasks/{task.id}", as_=EMP)
    assert card["task"]["status"] == "active" and card["task"]["actions"]["submit"] is True
    assert "✅ Руководитель подтвердил ваше поручение" in h.last_text(EMP)
