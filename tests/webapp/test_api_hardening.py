"""Устойчивость API приложения: тело запроса читается до соединения с базой и не дольше срока, фоновая
оценка не держит соединение пула, лимиты частоты (подсказка AI, поручения), гонки сдачи и принятия,
округление оценки руководителя (docs/MINIAPP_SPEC.md §6.2, §6.3, §8.4–§8.7).

Тесты идут и на SQLite, и на PostgreSQL (TEST_DATABASE_URL — настоящий пул 3 + 1, как в проде).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event

from bot.ai import evaluate as ai_evaluate
from bot.ai import formulate
from bot.ai.formulate import ResultSuggestion
from bot.db.models import EventType, TaskStatus, User
from bot.services import tasks as tasks_svc
from bot.webapp import RateLimiter, api
from bot.webapp.auth import INIT_DATA_HEADER

from .conftest import EMP, EMP2, MGR, MiniApp
from .test_api_submit import PDF, form

PROPOSAL = {"title": "Подготовить справку", "expected_result": "Справка по 5 договорам", "deadline": "2026-10-09"}


# --- RateLimiter -----------------------------------------------------------------------------------------


def test_rate_limiter_counts_windows_and_undo() -> None:
    now = [1000.0]
    limiter = RateLimiter(clock=lambda: now[0])
    limits = ((2, 60.0), (3, 3600.0))
    assert limiter.hit("x", 1, limits) is None
    assert limiter.hit("x", 1, limits) is None
    assert limiter.hit("x", 1, limits) == pytest.approx(60.0)  # третья за минуту — нельзя, ждать минуту
    assert limiter.hit("x", 2, limits) is None  # другой пользователь — свой счёт
    now[0] += 61
    assert limiter.hit("x", 1, limits) is None  # минута прошла: третья за час
    wait = limiter.hit("x", 1, limits)
    assert wait == pytest.approx(3600.0 - 61)  # часовой лимит: освободится, когда выйдет первая попытка
    limiter.undo("x", 1)  # последняя засчитанная (третья) — не в счёт
    assert limiter.hit("x", 1, limits) is None


def test_wait_text() -> None:
    assert api._wait_text(0.2) == "1 с"
    assert api._wait_text(40.1) == "41 с"
    assert api._wait_text(59.4) == "1 мин"
    assert api._wait_text(61) == "2 мин"
    assert api._wait_text(599.9) == "10 мин"
    assert api._wait_text(3 * 3600 - 5) == "3 ч"
    assert api._wait_text(api.DAY_SEC - 0.1) == "24 ч"


# --- Тело запроса — до соединения с базой ----------------------------------------------------------------


@pytest.fixture
def opened_sessions(ma: MiniApp, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Сколько сессий БД открыли запросы API (подмена ctx.sessionmaker)."""
    real = ma.ctx.sessionmaker
    opened: list[int] = []

    def spy() -> Any:
        opened.append(1)
        return real()

    monkeypatch.setattr(ma.ctx, "sessionmaker", spy)
    yield opened


async def _read_response(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    length = next(int(line.split(":", 1)[1]) for line in lines if line.lower().startswith("content-length:"))
    return status, await asyncio.wait_for(reader.readexactly(length), timeout=10)


async def test_stalled_json_body_takes_no_db_connection(
    ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch, opened_sessions: list[int]
) -> None:
    """Клиент прислал заголовки (Content-Length: 200) и не присылает тело: сессия БД не открывается,
    остальные запросы работают, через BODY_READ_TIMEOUT_SEC — 408 request_timeout."""
    await ma.seed_team(1)
    monkeypatch.setattr(api, "BODY_READ_TIMEOUT_SEC", 0.5)
    server = ma.client.server
    stalled = []
    for path in ("/api/ai/formulate", "/api/proposals", "/api/tasks"):
        reader, writer = await asyncio.open_connection(server.host, server.port)
        writer.write(
            (
                f"POST {path} HTTP/1.1\r\nHost: {server.host}:{server.port}\r\n"
                f"{INIT_DATA_HEADER}: {ma.init_data(EMP)}\r\n"
                "Content-Type: application/json\r\nContent-Length: 200\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        stalled.append((reader, writer))
    await asyncio.sleep(0.15)
    assert opened_sessions == []  # тела нет — соединения с базой тоже
    me = await ma.get("/api/me", as_=MGR)
    assert me.status == 200 and me["access"] == "active"
    for reader, writer in stalled:
        status, body = await _read_response(reader)
        assert status == 408 and b'"code":"request_timeout"' in body
        assert api.REQUEST_TIMEOUT.encode() in body
        writer.close()
    assert len(opened_sessions) == 1  # только /api/me


async def test_json_body_still_parsed_after_middleware_read(ma: MiniApp, frozen: Any) -> None:
    """Тело уже прочитано middleware — обработчик получает те же байты (и прежние ошибки формата)."""
    await ma.seed_team(1)
    resp = await ma.post("/api/proposals", as_=EMP, json=PROPOSAL)
    assert resp.status == 201, resp.data
    resp = await ma.request("POST", "/api/proposals", as_=EMP, data=b"[1, 2]",
                            headers={"Content-Type": "application/json"})
    assert resp.status == 400 and resp.error == api.BAD_JSON
    resp = await ma.request("POST", "/api/proposals", as_=EMP, data=b"x" * (1024 * 1024 + 10),
                            headers={"Content-Type": "application/json"})
    assert resp.status == 413 and resp.code == "too_large"


# --- Фоновая оценка не держит соединение пула ------------------------------------------------------------


async def test_background_evaluation_holds_no_db_connection(
    ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пока идёт оценка сдачи (до пары минут с AI), соединение с базой возвращено в пул — как в чате."""
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr)
    engine = ma.sessionmaker.kw["bind"].sync_engine
    held = [0]

    def checkout(*_: Any) -> None:
        held[0] += 1

    def checkin(*_: Any) -> None:
        held[0] -= 1

    seen: list[int] = []
    real = ai_evaluate.evaluate_submission

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(held[0])
        return await real(*args, **kwargs)

    monkeypatch.setattr(ai_evaluate, "evaluate_submission", spy)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form(value="110"))
        assert resp.status == 202, resp.data
        await ma.drain()
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
    assert seen == [0], "во время оценки соединение с базой должно быть свободно"
    assert (await ma.task(task_id)).submissions[-1].ai_score == 110  # оценка всё равно записана


# --- Лимиты частоты ------------------------------------------------------------------------------------


@pytest.fixture
def fake_formulate(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake(title: str, raw: str, deadline_text: Any = None) -> ResultSuggestion:
        calls.append(raw)
        return ResultSuggestion(expected_result="Проверить 100 договоров до пятницы", plan_value=100.0,
                                plan_unit="договоров", note=None, source="ai")

    monkeypatch.setattr(formulate, "suggest_expected_result", fake)
    return calls


BODY = {"title": "Анализ договоров", "raw_result": "проверить договоры"}


async def test_formulate_rate_limited_per_user(ma: MiniApp, frozen: Any, fake_formulate: list[str]) -> None:
    await ma.seed_team(2)
    per_minute = api.FORMULATE_LIMITS_EMPLOYEE[0][0]
    for _ in range(per_minute):
        resp = await ma.post("/api/ai/formulate", as_=EMP, json=BODY)
        assert resp.status == 200 and resp["source"] == "ai"
    resp = await ma.post("/api/ai/formulate", as_=EMP, json=BODY)
    assert resp.status == 429 and resp.code == "rate_limited"
    assert resp.error == api.FORMULATE_TOO_OFTEN.format(wait="1 мин")
    assert len(fake_formulate) == per_minute  # отказ — без обращения к AI
    assert not ma.ctx.gate.busy("formulate", EMP)
    resp = await ma.post("/api/ai/formulate", as_=EMP2, json=BODY)  # у другого сотрудника — свой счёт
    assert resp.status == 200
    resp = await ma.post("/api/ai/formulate", as_=EMP, json={"title": "x"})  # ошибка данных — 400, не 429
    assert resp.status == 400


async def test_formulate_daily_limit_is_tighter_for_employees(
    ma: MiniApp, frozen: Any, fake_formulate: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await ma.seed_team(1)
    monkeypatch.setattr(api, "FORMULATE_LIMITS_EMPLOYEE", ((100, 60.0), (2, api.DAY_SEC)))
    monkeypatch.setattr(api, "FORMULATE_LIMITS_MANAGER", ((100, 60.0), (3, api.DAY_SEC)))
    assert [(await ma.post("/api/ai/formulate", as_=EMP, json=BODY)).status for _ in range(3)] == [200, 200, 429]
    assert [(await ma.post("/api/ai/formulate", as_=MGR, json=BODY)).status for _ in range(4)] == [200, 200, 200, 429]
    last = await ma.post("/api/ai/formulate", as_=EMP, json=BODY)
    assert last.error.endswith("сформулируйте результат сами.") and "24 ч" in last.error


async def test_formulate_team_daily_cap_falls_back_to_rules(
    ma: MiniApp, frozen: Any, fake_formulate: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Суточный лимит подсказок команды: дальше — правила с пояснением, квоты AI остаются оценке сдач."""
    await ma.seed_team(2)
    monkeypatch.setattr(api, "FORMULATE_TEAM_PER_DAY", 2)
    monkeypatch.setattr(api.ai_provider, "ai_available", lambda: False)
    for _ in range(3):  # AI выключен — счётчик команды не идёт (иначе «подсказки закончились» без AI)
        assert (await ma.post("/api/ai/formulate", as_=MGR, json=BODY))["notice"] is None
    monkeypatch.setattr(api.ai_provider, "ai_available", lambda: True)
    for who in (EMP, EMP2):
        assert (await ma.post("/api/ai/formulate", as_=who, json=BODY))["source"] == "ai"
    resp = await ma.post("/api/ai/formulate", as_=MGR, json=BODY)
    assert resp.status == 200 and resp["source"] == "rules" and resp["notice"] == api.AI_TEAM_LIMIT_NOTICE
    assert resp["expected_result"] == formulate.rules_suggestion(BODY["title"], BODY["raw_result"]).expected_result
    resp = await ma.post("/api/ai/formulate", as_=MGR, json={**BODY, "previous": "Вариант 1"})
    assert resp["source"] == "rules" and resp["notice"] == api.AI_TEAM_LIMIT_NOTICE
    assert len(fake_formulate) == 3 + 2  # после лимита команды — без обращения к AI


async def test_proposals_rate_limited(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Поручения — не чаще лимита: каждое рассылается всем руководителям. Отказ сервиса (прошлый срок)
    в счёт не идёт."""
    await ma.seed_team(1)
    monkeypatch.setattr(api, "PROPOSAL_LIMITS", ((2, 600.0), (20, api.DAY_SEC)))
    resp = await ma.post("/api/proposals", as_=EMP, json={**PROPOSAL, "deadline": "2026-09-01"})
    assert resp.status == 400 and resp.code == "domain"
    for _ in range(2):
        assert (await ma.post("/api/proposals", as_=EMP, json=PROPOSAL)).status == 201
    before = len(ma.h.sent_to(MGR))
    resp = await ma.post("/api/proposals", as_=EMP, json=PROPOSAL)
    assert resp.status == 429 and resp.code == "rate_limited"
    assert resp.error == api.PROPOSE_TOO_OFTEN.format(wait="10 мин")
    assert len(ma.h.sent_to(MGR)) == before  # руководителю ничего не ушло
    assert not ma.ctx.gate.busy("propose", EMP)


# --- Оценка руководителя — целая, как в чате --------------------------------------------------------------


@pytest.mark.parametrize(("raw", "stored"), [(95.5, 96.0), (95.4, 95.0), (149.6, 150.0), (0.4, 0.0)])
async def test_manager_score_rounded_like_chat(ma: MiniApp, frozen: Any, raw: float, stored: float) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr, kind="submitted")
    sub_id = (await ma.task(task_id)).submissions[0].id
    resp = await ma.post(f"/api/submissions/{sub_id}/score", as_=MGR, json={"score": raw})
    assert resp.status == 200, resp.data
    task = await ma.task(task_id)
    assert task.status == TaskStatus.DONE and task.final_score == stored
    assert task.submissions[0].final_score == stored
    assert resp["task"]["final_score_text"] == f"{int(stored)} %"


async def test_service_rounds_manager_score(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr, kind="submitted")
    async with ma.db() as session:
        sub = (await tasks_svc.get_task(session, task_id)).submissions[0]
        task = await tasks_svc.review_set_score(session, sub.id, await session.get(User, mgr.id), 89.5)
        await session.commit()
        assert task.final_score == 90.0 and sub.final_score == 90.0
        events = await tasks_svc.task_events(session, task_id)
    assert events[-1].type == EventType.SCORE_CHANGED and events[-1].data["score"] == 90.0


# --- Гонки: сдача из приложения после сдачи из чата, принятие с двух сторон ------------------------------


async def test_app_submit_rereads_task_after_upload(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Пока грузились файлы, задачу сдали из чата и вернули на доработку: сдача из приложения — попытка 3,
    а не вторая «попытка 2»."""
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr, kind="rework")
    original = api._upload_files

    async def chat_round_meanwhile(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        async with ma.db() as session:
            sub = await tasks_svc.submit_result(session, task_id, await session.get(User, emp.id), fact_text="Сдал из чата")
            await session.commit()
            await tasks_svc.review_rework(session, sub.id, await session.get(User, mgr.id), "Добавьте выводы")
            await session.commit()
        return result

    monkeypatch.setattr(api, "_upload_files", chat_round_meanwhile)
    resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form(files=[("a.pdf", PDF, None)]))
    assert resp.status == 202, resp.data
    assert resp["attempt"] == 3
    await ma.drain()
    task = await ma.task(task_id)
    assert [s.attempt for s in task.submissions] == [1, 2, 3]
    assert task.status == TaskStatus.SUBMITTED
    async with ma.db() as session:
        events = await tasks_svc.task_events(session, task_id)
    assert [e.data["attempt"] for e in events if e.type == EventType.SUBMITTED] == [2, 3]


async def test_accept_twice_in_parallel_writes_one_event(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr, accepted=False)
    first, second = await asyncio.gather(
        ma.post(f"/api/tasks/{task_id}/accept", as_=EMP), ma.post(f"/api/tasks/{task_id}/accept", as_=EMP)
    )
    assert first.status == second.status == 200
    assert first["task"]["accepted"] and second["task"]["accepted"]
    async with ma.db() as session:
        events = await tasks_svc.task_events(session, task_id)
    assert [e.type for e in events].count(EventType.ACCEPTED) == 1


async def test_accept_with_stale_task_writes_no_second_event(ma: MiniApp, frozen: Any) -> None:
    """Две сессии прочитали задачу до принятия (чат и приложение); вторая принимает уже принятую — без
    второго события, с актуальным временем принятия."""
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr, accepted=False)
    async with ma.db() as stale:
        employee = await stale.get(User, emp.id)
        seen = await tasks_svc.get_task(stale, task_id)  # ссылка держит объект в сессии (identity map — слабая)
        assert seen.accepted_at is None
        await stale.commit()
        async with ma.db() as other:
            accepted = await tasks_svc.accept_task(other, task_id, await other.get(User, emp.id))
            await other.commit()
            when = accepted.accepted_at
        again = await tasks_svc.accept_task(stale, task_id, employee)
        await stale.commit()
        assert again is seen and again.accepted_at == when
        events = await tasks_svc.task_events(stale, task_id)
    assert [e.type for e in events].count(EventType.ACCEPTED) == 1
