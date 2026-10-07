"""Общий конвейер после сдачи: оценка (AI или правила) и уведомление руководителю.

``bot/services/submission_flow.py`` (docs/MINIAPP_SPEC.md §9, §12.3) — им пользуются и чат
(task_submit), и приложение (bot.webapp.api, в фоне). Здесь — сам конвейер на настоящем боте с фейковым
Telegram (фикстура ``app``): правила при выключенном AI, ответ AI, зависший AI, сбой записи оценки,
гонки (задачу отменили / руководитель уже решил, пока AI думал) и «никогда не бросает».
Сети нет: оценка AI подменяется на уровне ``ai_evaluate.evaluate_submission``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from bot.ai import evaluate as ai_evaluate
from bot.ai import evidence as ai_evidence
from bot.ai import provider as ai_provider
from bot.ai.evaluate import Evaluation
from bot.db.models import ReviewDecision, Submission, Task, TaskStatus, User
from bot.handlers import task_submit
from bot.services import submission_flow
from bot.services import tasks as tasks_svc
from bot.utils.dates import utcnow

from .fakebot import MANAGER_TG_ID, BotHarness, RequestLog

EMP = 2001
AI_RATIONALE = "Проверено 110 договоров из 100 — план перевыполнен, отчёт приложен."


async def submitted(app: BotHarness, *, plan: float | None = 100.0, fact: float | None = 110.0) -> tuple[int, int]:
    """Руководитель, сотрудник и сданная задача (submit_result + commit, как перед конвейером)."""
    mgr = await app.seed_user(MANAGER_TG_ID, "Петрова Анна Сергеевна", role="manager")
    emp = await app.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    async with app.db() as session:
        manager = await session.get(User, mgr.id)
        employee = await session.get(User, emp.id)
        task = await tasks_svc.create_task(
            session,
            creator=manager,
            assignee_id=employee.id,
            title="Анализ договоров",
            expected_result="Проверить 100 договоров и представить отчёт",
            deadline=utcnow() + timedelta(days=2),
            weight=20,
            plan_value=plan,
            plan_unit="договоров" if plan is not None else None,
        )
        await session.commit()
        sub = await tasks_svc.submit_result(
            session, task.id, employee, fact_text="Проверено 110 договоров", fact_value=fact
        )
        await session.commit()
        return task.id, sub.id


async def run_flow(app: BotHarness, sub_id: int, **kwargs: Any) -> RequestLog:
    """run_after_submit на свежей сессии (как фон приложения); FlowResult — в log.result."""
    async with app.db() as session:
        sub = await tasks_svc.get_submission(session, sub_id)
        assert sub is not None
        return await app.capture(submission_flow.run_after_submit(app.bot, session, sub.task, sub, **kwargs))


async def load(app: BotHarness, task_id: int, sub_id: int) -> tuple[Task, Submission]:
    async with app.db() as session:
        task = await tasks_svc.get_task(session, task_id)
        assert task is not None
        sub = next(s for s in task.submissions if s.id == sub_id)
        return task, sub


def manager_texts(log: RequestLog) -> str:
    return "\n".join(log.to(MANAGER_TG_ID).texts)


@pytest.fixture
def fake_ai(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Оценка AI без сети: ответ, задержка и «тем временем» задаются в словаре."""
    state: dict[str, Any] = {"calls": 0, "delay": 0.0, "meanwhile": None, "score": 112.0}

    async def evaluate_submission(task: Task, sub: Submission, evidence: Any = None, *, time_budget: Any = None) -> Evaluation:
        state["calls"] += 1
        state["time_budget"] = time_budget
        if state["meanwhile"] is not None:
            await state["meanwhile"]()
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        return Evaluation(score=state["score"], rationale=AI_RATIONALE, source="ai", model="gemini-test")

    async def no_files(bot: Any, attachments: Any) -> list[Any]:
        return []

    monkeypatch.setattr(ai_evaluate, "evaluate_submission", evaluate_submission)
    monkeypatch.setattr(ai_evidence, "collect_evidence", no_files)
    return state


# --- Оценка ---------------------------------------------------------------------------------------------


async def test_rules_when_ai_is_off_and_manager_notified(app: BotHarness) -> None:
    task_id, sub_id = await submitted(app)
    log = await run_flow(app, sub_id, budget_sec=5, use_ai=False)
    result = log.result
    assert result == submission_flow.FlowResult(task_id, sub_id, TaskStatus.SUBMITTED, "rules", True)
    task, sub = await load(app, task_id, sub_id)
    assert sub.ai_source == "rules" and sub.ai_score == 110
    assert sub.ai_rationale.startswith(submission_flow.RULES_PREFIX)
    assert task.ai_score == 110
    assert f"Результат по задаче #{task_id}" in manager_texts(log)
    assert not log.to(EMP)  # сотруднику конвейер ничего не пишет


async def test_ai_answer_recorded_and_manager_notified(app: BotHarness, fake_ai: dict[str, Any]) -> None:
    task_id, sub_id = await submitted(app)
    log = await run_flow(app, sub_id, budget_sec=30, use_ai=True)
    assert log.result.source == "ai" and log.result.notified is True
    assert fake_ai["calls"] == 1
    # Время на перебор моделей — общий срок минус запас.
    assert fake_ai["time_budget"] <= 30 - submission_flow.AI_BUDGET_MARGIN_SEC + 0.01
    _, sub = await load(app, task_id, sub_id)
    assert (sub.ai_source, sub.ai_score, sub.ai_model) == ("ai", 112, "gemini-test")
    assert sub.ai_rationale == AI_RATIONALE
    assert f"Результат по задаче #{task_id}" in manager_texts(log)


async def test_hanging_ai_falls_back_to_rules_by_budget(app: BotHarness, fake_ai: dict[str, Any]) -> None:
    fake_ai["delay"] = 5
    task_id, sub_id = await submitted(app)
    log = await run_flow(app, sub_id, budget_sec=0.05, use_ai=True)
    assert log.result.source == "rules" and log.result.notified is True
    _, sub = await load(app, task_id, sub_id)
    assert sub.ai_source == "rules" and sub.ai_rationale.startswith(submission_flow.RULES_PREFIX)


async def test_failed_ai_record_rolls_back_and_uses_rules(
    app: BotHarness, fake_ai: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_record = tasks_svc.record_evaluation
    attempts: list[str] = []

    async def broken_once(session: Any, sub_id: int, **kwargs: Any) -> Any:
        attempts.append(kwargs["source"])
        if kwargs["source"] == "ai":
            await real_record(session, sub_id, **kwargs)
            raise RuntimeError("ошибка базы при записи оценки AI")
        return await real_record(session, sub_id, **kwargs)

    monkeypatch.setattr(tasks_svc, "record_evaluation", broken_once)
    task_id, sub_id = await submitted(app)
    log = await run_flow(app, sub_id, budget_sec=30, use_ai=True)
    assert attempts == ["ai", "rules"]
    assert log.result.source == "rules" and log.result.notified is True
    _, sub = await load(app, task_id, sub_id)
    assert sub.ai_source == "rules" and sub.ai_model is None  # запись AI откатилась целиком


# --- Гонки: пока AI думал --------------------------------------------------------------------------------


async def test_task_cancelled_during_evaluation_no_notification(app: BotHarness, fake_ai: dict[str, Any]) -> None:
    task_id, sub_id = await submitted(app)

    async def cancel() -> None:
        async with app.db() as session:
            manager = await app.get_user(MANAGER_TG_ID)
            await tasks_svc.cancel_task(session, task_id, await session.get(User, manager.id), "Не актуально")
            await session.commit()

    fake_ai["meanwhile"] = cancel
    log = await run_flow(app, sub_id, budget_sec=30, use_ai=True)
    assert log.result.status == TaskStatus.CANCELLED
    assert log.result.notified is False
    assert "Результат по задаче" not in manager_texts(log)


async def test_decision_already_made_no_notification(app: BotHarness, fake_ai: dict[str, Any]) -> None:
    task_id, sub_id = await submitted(app)

    async def decide() -> None:
        async with app.db() as session:
            manager = await app.get_user(MANAGER_TG_ID)
            await tasks_svc.review_set_score(session, sub_id, await session.get(User, manager.id), 95, "Хорошо")
            await session.commit()

    fake_ai["meanwhile"] = decide
    log = await run_flow(app, sub_id, budget_sec=30, use_ai=True)
    assert log.result.status == TaskStatus.DONE and log.result.notified is False
    assert "Результат по задаче" not in manager_texts(log)
    task, sub = await load(app, task_id, sub_id)
    assert task.final_score == 95 and sub.decision == ReviewDecision.CHANGED


# --- Устойчивость ------------------------------------------------------------------------------------------


async def test_never_raises_when_services_fail(app: BotHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("база недоступна")

    task_id, sub_id = await submitted(app)
    monkeypatch.setattr(tasks_svc, "record_evaluation", broken)
    monkeypatch.setattr(tasks_svc, "get_task", broken)
    log = await run_flow(app, sub_id, budget_sec=5, use_ai=False)
    assert log.result == submission_flow.FlowResult(task_id, sub_id, None, None, False)
    assert not log  # ни уведомлений, ни других запросов к Telegram


async def test_notification_failure_does_not_raise(app: BotHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    from bot import notify

    async def broken_notify(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("сбой уведомления")

    monkeypatch.setattr(notify, "notify_submission", broken_notify)
    _, sub_id = await submitted(app)
    log = await run_flow(app, sub_id, budget_sec=5, use_ai=False)
    assert log.result.source == "rules" and log.result.notified is True


async def test_cancelled_error_is_propagated(app: BotHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    async def cancelled(*args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(ai_evaluate, "evaluate_submission", cancelled)
    _, sub_id = await submitted(app)
    with pytest.raises(asyncio.CancelledError):
        await run_flow(app, sub_id, budget_sec=5, use_ai=False)


async def test_defaults_from_settings(app: BotHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """budget_sec/use_ai не заданы — default_budget_sec() и provider.ai_available()."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(submission_flow, "default_budget_sec", lambda: seen.setdefault("budget", 7.0))
    monkeypatch.setattr(ai_provider, "ai_available", lambda: seen.setdefault("ai", False))
    _, sub_id = await submitted(app)
    log = await run_flow(app, sub_id)
    assert seen == {"budget": 7.0, "ai": False}
    assert log.result.source == "rules"


# --- Текст результата и совместимость с чатом -------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "notes", "expected"),
    [
        (None, [], None),
        ("", [], None),
        ("Отчёт готов", [], "Отчёт готов"),
        (None, ["ссылка на папку"], "Подтверждающие материалы: ссылка на папку"),
        ("Отчёт готов", ["папка", "почта"], "Отчёт готов\n\nПодтверждающие материалы: папка; почта"),
    ],
)
def test_result_with_notes(result: str | None, notes: list[str], expected: str | None) -> None:
    assert submission_flow.result_with_notes(result, notes) == expected
    assert task_submit._result_with_notes(result, notes) == expected  # чат — тот же текст


def test_chat_keeps_names_tests_rely_on() -> None:
    assert task_submit.RULES_PREFIX == submission_flow.RULES_PREFIX == ai_evaluate.RULES_PREFIX
    assert callable(task_submit._ai_budget_sec) and callable(task_submit.ai_available)


def test_awaits_review_matrix() -> None:
    sub = Submission(id=1, decision=None)
    task = Task(status=TaskStatus.SUBMITTED)
    task.submissions = [sub]
    assert submission_flow.awaits_review(task, sub) is True
    sub.decision = ReviewDecision.APPROVED
    assert submission_flow.awaits_review(task, sub) is False
    sub.decision = None
    task.status = TaskStatus.CANCELLED
    assert submission_flow.awaits_review(task, sub) is False
    task.status = TaskStatus.SUBMITTED
    newer = Submission(id=2, decision=None)
    task.submissions = [sub, newer]
    assert submission_flow.awaits_review(task, sub) is False
