"""Работа через сайт целиком (docs/MINIAPP_SPEC.md §15): те же действия, что в Mini App, но человек вошёл
через подтверждение в боте и ходит в API по cookie сессии.

Сквозной цикл: начальник ставит задачу → сотрудник принимает, диктует ответ голосом и сдаёт результат с
файлами → начальник видит оценку и подтверждает её → сотрудник видит итог. Плюс поручение сотрудника и его
подтверждение. Уведомления уходят в чат как обычно: сайт их не заменяет.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import methods as m

from bot.db.models import TaskStatus

from .conftest import EMP, MGR, MiniApp
from .test_api_submit import JPEG, PDF, form
from .test_api_voice import WEBM, Ear, ear  # noqa: F401 - фикстура ear
from .test_weblogin import Browser, sign_in


async def test_full_cycle_through_the_site(ma: MiniApp, ear: Ear) -> None:  # noqa: F811
    mgr, (emp,) = await ma.seed_team(1)
    boss = await sign_in(ma, MGR)
    worker = await sign_in(ma, EMP, Browser(ma, agent="Mozilla/5.0 (Linux; Android 14) Chrome/141.0 Mobile Safari/537.36"))

    # Начальник ставит задачу — сотруднику приходит уведомление в чат.
    me = await boss.get("/api/me")
    created = await boss.post(
        "/api/tasks",
        json={
            "assignee_id": emp.id,
            "title": "Анализ договоров",
            "expected_result": "Проверить 100 договоров и представить отчёт",
            "plan_value": 100,
            "plan_unit": "договоров",
            "deadline": me["deadline_options"][0]["date"],
            "priority": "high",
            "weight": 20,
        },
    )
    assert created.status == 201, created.data
    task_id = created["task"]["id"]
    assert any("Анализ договоров" in text for text in ma.h.sent_to(EMP))

    # Сотрудник видит задачу, принимает её.
    listed = await worker.get("/api/tasks")
    assert [item["id"] for item in listed["items"]] == [task_id]
    assert (await worker.post(f"/api/tasks/{task_id}/accept")).status == 200

    # Голосовой ввод — тот же маршрут и тот же код, что в Mini App.
    voiced = await worker.post("/api/voice", data=WEBM, headers={"Content-Type": "audio/webm;codecs=opus"})
    assert voiced.status == 200 and voiced.data == {"text": ear.heard}

    # Сдача результата с файлами: они, как и раньше, уходят на хранение в чат сотрудника с ботом.
    body = form(voiced["text"], value="110", files=[("Фото.jpg", JPEG, "image/jpeg"), ("Отчёт.pdf", PDF, "application/pdf")])
    submitted = await worker.post(f"/api/tasks/{task_id}/submit", data=body)
    assert submitted.status == 202, submitted.data
    assert submitted["files"] == 2
    await ma.drain()
    uploads = [r for r in ma.api.requests if isinstance(r, m.SendPhoto | m.SendDocument) and r.chat_id == EMP]
    assert len(uploads) == 2
    assert any("Анализ договоров" in text for text in ma.h.sent_to(MGR))  # начальнику — уведомление в чат

    # Сотрудник не видит оценку до решения начальника и не может подтвердить её сам.
    sub_id = submitted["submission_id"]
    card = await worker.get(f"/api/tasks/{task_id}")
    assert card["task"]["status"] == "submitted" and card["task"]["final_score"] is None
    [own_submission] = card["submissions"]
    assert own_submission["ai"] is None and own_submission["ai_hidden"] is True  # оценка AI скрыта до решения
    refused = await worker.post(f"/api/submissions/{sub_id}/confirm")
    assert refused.status == 403 and refused.code == "forbidden"

    # Начальник видит очередь проверки и подтверждает оценку.
    queue = await boss.get("/api/review")
    assert [item["submission"]["id"] for item in queue["items"]] == [sub_id]
    confirmed = await boss.post(f"/api/submissions/{sub_id}/confirm")
    assert confirmed.status == 200, confirmed.data
    assert (await ma.task(task_id)).status == TaskStatus.DONE

    # Итог виден сотруднику на сайте — и в его KPI.
    card = await worker.get(f"/api/tasks/{task_id}")
    assert card["task"]["status"] == "done" and card["task"]["final_score"] is not None
    kpi = await worker.get("/api/dashboard")
    assert kpi.status == 200 and kpi["scope"] == "self"


async def test_proposal_through_the_site(ma: MiniApp) -> None:
    """Поручение сотрудника: он вносит его на сайте, начальник подтверждает на сайте — обычным путём."""
    mgr, (emp,) = await ma.seed_team(1)
    boss = await sign_in(ma, MGR)
    worker = await sign_in(ma, EMP)
    me = await worker.get("/api/me")

    proposed = await worker.post(
        "/api/proposals",
        json={"title": "Сверка актов", "expected_result": "Сверить 20 актов", "plan_value": 20, "plan_unit": "актов",
              "deadline": me["deadline_options"][0]["date"]},
    )
    assert proposed.status == 201, proposed.data
    task_id = proposed["task"]["id"]
    assert proposed["task"]["status"] == "proposed"
    # Сам себе подтвердить поручение сотрудник не может.
    assert (await worker.post(f"/api/tasks/{task_id}/approve", json={"weight": 10, "priority": "medium"})).status == 403

    listed = await boss.get("/api/proposals")
    assert [item["task"]["id"] for item in listed["items"]] == [task_id]
    approved = await boss.post(f"/api/tasks/{task_id}/approve", json={"weight": 10, "priority": "medium"})
    assert approved.status == 200, approved.data
    assert (await ma.task(task_id)).status == TaskStatus.ACTIVE


@pytest.mark.parametrize("lang", ["ru", "uz"])
async def test_language_choice_on_the_site_is_the_same_setting(ma: MiniApp, lang: str) -> None:
    """Язык, выбранный на сайте, — тот же язык человека, что в Mini App и в чате."""
    await ma.seed_team(1)
    browser = await sign_in(ma, EMP)
    assert (await browser.post("/api/lang", json={"lang": lang})).status == 200
    assert (await browser.get("/api/me"))["lang"] == lang
    assert (await ma.get("/api/me", as_=EMP))["lang"] == lang
    labels: Any = (await browser.get("/api/me"))["deadline_options"]
    assert labels and all(isinstance(option["label"], str) and option["label"] for option in labels)
