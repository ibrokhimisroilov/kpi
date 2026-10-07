"""/api/me, подсказка AI «Сделать измеримым», Excel-отчёт, сотрудники (docs/MINIAPP_SPEC.md §8.3, §8.4, §8.8,
§12.2 test_misc_api)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from bot.ai import formulate
from bot.ai import provider as ai_provider
from bot.ai.formulate import ResultSuggestion
from bot.services import export as export_service
from bot.utils import dateparse
from bot.webapp import api

from .conftest import EMP, EMP2, MGR, MiniApp


# --- /api/me -----------------------------------------------------------------------------------------------


async def test_me_manager_counts_and_config(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, emp2) = await ma.seed_team(2)
    await ma.seed_task(emp, mgr, kind="submitted")
    await ma.seed_task(emp, mgr, kind="overdue")
    await ma.seed_task(emp2, mgr)
    await ma.seed_task(emp2, mgr, kind="rework")
    await ma.seed_task(emp, mgr, kind="proposed")
    await ma.seed_task(emp, mgr, kind="done")
    await ma.seed_user(3001, "Новиков Новик", status="pending")
    await ma.seed_user(3002, "", status="pending")  # анкета не заполнена — не заявка
    resp = await ma.get("/api/me", as_=MGR)
    assert resp.status == 200
    assert resp["access"] == "active" and resp["role"] == "manager" and resp["message"] is None
    assert resp["user"]["tg_id"] == MGR and resp["user"]["full_name"] == "Петрова Анна Сергеевна"
    assert resp["counts"] == {"review": 1, "proposals": 1, "open": 3, "overdue": 1, "pending_users": 1}
    assert resp["now"] == "2026-10-02T07:00:00Z" and resp["today"] == "2026-10-02"
    config = resp["config"]
    assert config["max_score"] == 150 and config["timezone"] == "Asia/Tashkent"
    assert config["default_deadline_time"] == "18:00" and config["ai_enabled"] is False
    assert config["period_kinds"] == ["week", "month", "quarter", "year"]
    assert (config["max_files"], config["max_file_mb"], config["max_total_mb"]) == (10, 20, 50)
    assert config["weight_options"] == list(api.WEIGHT_OPTIONS) and config["score_options"] == list(api.SCORE_OPTIONS)
    assert config["history_page_size"] == 10 and config["trend_weeks"] == 8
    expected = [{"label": label, "date": day} for label, day in dateparse.quick_deadline_options()]
    assert resp["deadline_options"] == expected and resp["deadline_options"]


async def test_me_employee_counts(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, emp2) = await ma.seed_team(2)
    await ma.seed_task(emp, mgr, accepted=False)
    await ma.seed_task(emp, mgr, kind="overdue")
    await ma.seed_task(emp, mgr, kind="rework")
    await ma.seed_task(emp, mgr, kind="submitted")
    await ma.seed_task(emp, mgr, kind="proposed")
    await ma.seed_task(emp2, mgr, accepted=False)
    resp = await ma.get("/api/me", as_=EMP)
    assert resp["role"] == "employee" and resp["user"]["position"] == "Юрист"
    assert resp["counts"] == {"open": 3, "overdue": 1, "unaccepted": 1, "rework": 1, "review": 1, "proposed": 1}


@pytest.mark.parametrize(
    ("status", "full_name", "access", "message"),
    [("pending", "Заявкин Пётр", "pending", api.TXT_PENDING), ("blocked", "Блокин", "blocked", api.TXT_BLOCKED),
     ("pending", "", "unregistered", api.NOT_REGISTERED)],
)
async def test_me_inactive(ma: MiniApp, status: str, full_name: str, access: str, message: str) -> None:
    await ma.seed_user(3001, full_name, status=status)
    resp = await ma.get("/api/me", as_=3001)
    assert resp.status == 200 and resp["access"] == access and resp["message"] == message
    assert resp["role"] is None and resp["counts"] is None and resp["config"]["max_files"] == 10
    assert (resp["user"] is None) == (access == "unregistered")


async def test_me_never_creates_users(ma: MiniApp) -> None:
    resp = await ma.get("/api/me", headers=ma.auth(4242, first_name="Хакер"))
    assert resp["access"] == "unregistered" and resp["user"] is None
    assert await ma.h.get_user(4242) is None


# --- Подсказка AI ------------------------------------------------------------------------------------------


async def test_formulate_rules_without_ai(ma: MiniApp) -> None:
    await ma.seed_team(1)
    resp = await ma.post("/api/ai/formulate", as_=MGR, json={"title": "Анализ договоров",
                                                             "raw_result": "проверить 100 договоров"})
    assert resp.status == 200, resp.data
    expected = formulate.rules_suggestion("Анализ договоров", "проверить 100 договоров")
    assert resp.data == {"expected_result": expected.expected_result, "plan_value": 100, "plan_unit": "договоров",
                         "note": expected.note, "source": "rules", "notice": None}
    resp = await ma.post("/api/ai/formulate", as_=EMP, json={"title": "Анализ", "raw_result": "проверить 100 договоров",
                                                             "previous": "Проверить 100 договоров"})
    assert resp.status == 200 and resp["source"] == "rules" and resp["notice"] == api.AI_RETRY_NOTICE


async def test_formulate_validation(ma: MiniApp) -> None:
    await ma.seed_team(1)
    for data in ({}, {"title": "x"}, {"title": "x", "raw_result": "ab"}, {"title": "я" * 256, "raw_result": "abc"},
                 {"title": "x", "raw_result": "abc", "previous": "я" * 1001}, {"title": "x", "raw_result": "abc", "z": 1}):
        resp = await ma.post("/api/ai/formulate", as_=MGR, json=data)
        assert resp.status == 400 and resp.code == "bad_request", data


async def test_formulate_with_ai(ma: MiniApp, monkeypatch: pytest.MonkeyPatch) -> None:
    await ma.seed_team(1)
    calls: list[str] = []

    async def fake(title: str, raw: str, deadline_text: str | None = None) -> ResultSuggestion:
        calls.append(raw)
        return ResultSuggestion("Проверить 100 договоров и представить отчёт", 100.0, "договоров", None, "ai")

    monkeypatch.setattr(formulate, "suggest_expected_result", fake)
    resp = await ma.post("/api/ai/formulate", as_=MGR, json={"title": "Анализ", "raw_result": "проверить договоры",
                                                             "previous": "Проверить договоры"})
    assert resp["source"] == "ai" and resp["notice"] is None
    assert resp["expected_result"] == "Проверить 100 договоров и представить отчёт"
    assert calls == ["проверить договоры\n\nПредыдущий вариант: Проверить договоры. Предложи другую формулировку."]


async def test_formulate_hanging_ai_falls_back_to_rules(ma: MiniApp, monkeypatch: pytest.MonkeyPatch) -> None:
    await ma.seed_team(1)

    async def hanging(*args: Any, **kwargs: Any) -> ResultSuggestion:
        await asyncio.sleep(30)
        raise AssertionError("не должно дойти")

    monkeypatch.setattr(formulate, "suggest_expected_result", hanging)
    monkeypatch.setattr(ai_provider, "chain_budget_sec", lambda *a, **k: 0.05)
    monkeypatch.setattr(api, "FORMULATE_EXTRA_SEC", 0)
    resp = await ma.post("/api/ai/formulate", as_=MGR, json={"title": "Анализ", "raw_result": "проверить 100 договоров"})
    assert resp.status == 200 and resp["source"] == "rules" and resp["plan_value"] == 100


async def test_formulate_second_concurrent_is_busy(ma: MiniApp, monkeypatch: pytest.MonkeyPatch) -> None:
    await ma.seed_team(1)
    release = asyncio.Event()

    async def slow(title: str, raw: str, deadline_text: str | None = None) -> ResultSuggestion:
        await release.wait()
        return formulate.rules_suggestion(title, raw)

    monkeypatch.setattr(formulate, "suggest_expected_result", slow)
    body = {"title": "Анализ", "raw_result": "проверить 100 договоров"}
    first = asyncio.create_task(ma.post("/api/ai/formulate", as_=MGR, json=body))
    for _ in range(200):
        if ma.ctx.gate.busy("formulate", MGR):
            break
        await asyncio.sleep(0.01)
    second = await ma.post("/api/ai/formulate", as_=MGR, json=body)
    assert second.status == 429 and second.error == "⏳ Подождите, формулирую вариант…"
    # Другой пользователь этим запросом не занят: его подсказка идёт параллельно.
    other_user = asyncio.create_task(ma.post("/api/ai/formulate", as_=EMP, json=body))
    for _ in range(200):
        if ma.ctx.gate.busy("formulate", EMP):
            break
        await asyncio.sleep(0.01)
    assert ma.ctx.gate.busy("formulate", EMP)
    release.set()
    assert (await first).status == 200 and (await other_user).status == 200
    assert not ma.ctx.gate.busy("formulate", MGR)


# --- Excel-отчёт -----------------------------------------------------------------------------------------


async def test_export_sends_document_like_chat(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, _) = await ma.seed_team(2)
    await ma.seed_task(emp, mgr, kind="done", deadline=frozen.now - timedelta(hours=2))
    resp = await ma.post("/api/export", as_=MGR, params={"kind": "week", "offset": 0})
    assert resp.status == 202 and resp["status"] == "started"
    assert resp["period"]["label"] == "Неделя 28.09–04.10.2026"
    assert resp["message"] == "📤 Отчёт «Неделя 28.09–04.10.2026» придёт в чат с ботом через несколько секунд."
    await ma.drain()
    docs = ma.h.documents_sent(MGR)
    assert len(docs) == 1 and docs[0].file_name == "kpi_week_20260928.xlsx"
    assert docs[0].caption == "📊 Отчёт: Неделя 28.09–04.10.2026"
    assert docs[0].content[:2] == b"PK"  # xlsx — zip
    assert not ma.ctx.gate.busy("export", MGR)


async def test_export_failure_and_busy(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    await ma.seed_team(1)
    release = asyncio.Event()

    async def broken(*args: Any, **kwargs: Any) -> bytes:
        await release.wait()
        raise RuntimeError("сбой сборки")

    monkeypatch.setattr(export_service, "build_report_xlsx", broken)
    assert (await ma.post("/api/export", as_=MGR, params={"kind": "month"})).status == 202
    busy = await ma.post("/api/export", as_=MGR)
    assert busy.status == 429 and busy.error == "⏳ Отчёт уже готовится — он придёт в чат с ботом."
    release.set()
    await ma.drain()
    assert ma.h.last_text(MGR) == api.EXPORT_FAILED
    assert (await ma.post("/api/export", as_=MGR, params={"kind": "x"})).status == 400


async def test_export_send_failure(ma: MiniApp, frozen: Any) -> None:
    await ma.seed_team(1)
    original = ma.h.bot.send_document

    async def failing(*args: Any, **kwargs: Any) -> Any:
        from aiogram import methods as m
        from aiogram.exceptions import TelegramBadRequest

        raise TelegramBadRequest(method=m.SendDocument(chat_id=1, document="x"), message="Bad Request: file too big")

    ma.h.bot.send_document = failing  # type: ignore[method-assign]
    try:
        assert (await ma.post("/api/export", as_=MGR)).status == 202
        await ma.drain()
    finally:
        ma.h.bot.send_document = original  # type: ignore[method-assign]
    assert ma.h.last_text(MGR) == api.EXPORT_SEND_FAILED
    assert EMP2  # (второй сотрудник в этом тесте не нужен)
