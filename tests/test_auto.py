"""Автоподтверждение (bot.services.auto, bot.scheduler.jobs.run_auto_decisions).

Начальник не ответил за 24 часа — оценка AI подтверждается сама, поручение сотрудника принимается само.
Календарь (Asia/Tashkent, UTC+5): «сейчас» фикстуры clock — пятница 02.10.2026 12:00.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram import methods as m
from aiogram.client.default import DefaultBotProperties
from e2e.fakebot import FakeSession
from sqlalchemy import select

from bot.ai import weigh
from bot.db.models import EventType, Priority, ReviewDecision, Submission, Task, TaskEvent, TaskStatus, User
from bot.scheduler import jobs
from bot.services import auto
from bot.services import tasks as svc
from bot.services.errors import DomainError
from bot.utils.dates import to_utc

pytestmark = pytest.mark.usefixtures("clock")


def local(day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Местное время (Ташкент), октябрь 2026 -> naive UTC."""
    return to_utc(datetime(2026, 10, day, hour, minute))


# =====================================================================================================
# Время: когда срабатывает и когда напоминать
# =====================================================================================================


def test_plan_is_24_hours_weekends_count() -> None:
    plan = auto.plan(local(2, 12))  # пятница 12:00
    assert plan is not None
    assert plan.due == local(3, 12)  # суббота 12:00 — выходные считаются
    assert plan.remind_at == local(3, 9)


def test_plan_moves_out_of_quiet_hours() -> None:
    plan = auto.plan(local(2, 23))  # сдано ночью
    assert plan is not None
    assert plan.due == local(4, 8)  # через сутки снова ночь -> в 8:00
    assert plan.remind_at == local(3, 20)  # 5:00 — тихие часы -> вечером накануне, за час до их начала


def test_plan_reminder_before_morning_due() -> None:
    plan = auto.plan(local(2, 9, 30))
    assert plan is not None
    assert plan.due == local(3, 9, 30)
    assert plan.remind_at == local(2, 20)


def test_plan_counts_from_feature_start() -> None:
    plan = auto.plan(local(1, 10), feature_start=local(2, 12))
    assert plan is not None and plan.due == local(3, 12)


def test_plan_disabled(set_env: Callable[..., None]) -> None:
    set_env(AUTO_CONFIRM_HOURS="0")
    assert auto.plan(local(2, 12)) is None
    assert not auto.enabled()


def test_plan_without_reminder_when_window_is_short(set_env: Callable[..., None]) -> None:
    set_env(AUTO_CONFIRM_HOURS="2")
    plan = auto.plan(local(2, 12))
    assert plan is not None and plan.due == local(2, 14) and plan.remind_at is None


# =====================================================================================================
# Сервисы
# =====================================================================================================


async def submitted(session, clock, manager: User, employee: User, *, score: float | None = 95, source: str = "ai"):
    """Задача со сданным результатом и предварительной оценкой."""
    task = await svc.create_task(
        session,
        creator=manager,
        assignee_id=employee.id,
        title="Анализ договоров",
        expected_result="Проверить 100 договоров",
        deadline=clock.now + timedelta(days=3),
        weight=20,
        plan_value=100,
        plan_unit="договоров",
    )
    sub = await svc.submit_result(session, task.id, employee, fact_text="Проверено 95 договоров", fact_value=95)
    if score is not None:
        await svc.record_evaluation(session, sub.id, score=score, rationale="Обоснование", source=source, model="m")
    await session.commit()
    return task, sub


async def proposed(session, clock, employee: User, *, days: float = 3) -> Task:
    task = await svc.propose_task(
        session,
        employee=employee,
        title="Подготовить справку",
        expected_result="Справка по 5 договорам",
        deadline=clock.now + timedelta(days=days),
    )
    await session.commit()
    return task


async def events(session, task_id: int, type_: EventType) -> list[TaskEvent]:
    return [event for event in await svc.task_events(session, task_id) if event.type == type_]


def test_score_block_reasons() -> None:
    assert auto.score_block(None) == auto.BLOCK_NO_SCORE
    assert auto.score_block(Submission(ai_score=None)) == auto.BLOCK_NO_SCORE
    assert auto.score_block(Submission(ai_score=90, ai_source="rules")) == auto.BLOCK_RULES
    assert auto.score_block(Submission(ai_score=101, ai_source="ai")) == auto.BLOCK_HIGH
    assert auto.score_block(Submission(ai_score=100, ai_source="ai")) is None


async def test_auto_confirm_makes_ai_score_final(session, clock, manager, employee) -> None:
    task, sub = await submitted(session, clock, manager, employee)
    clock.advance(hours=24)

    done = await svc.review_auto_confirm(session, sub.id)

    assert done.status == TaskStatus.DONE and done.final_score == 95
    assert sub.decision == ReviewDecision.APPROVED and sub.final_score == 95
    assert sub.reviewer is None and sub.reviewed_at == clock.now
    assert sub.auto_confirmed
    (event,) = await events(session, task.id, EventType.SCORE_CONFIRMED)
    assert event.actor is None and event.data["auto"] is True and event.data["score"] == 95


@pytest.mark.parametrize(("score", "source"), [(90, "rules"), (120, "ai"), (None, "ai")])
async def test_auto_confirm_refuses_rules_high_and_missing_scores(
    session, clock, manager, employee, score, source
) -> None:
    task, sub = await submitted(session, clock, manager, employee, score=score, source=source)
    with pytest.raises(DomainError):
        await svc.review_auto_confirm(session, sub.id)
    assert task.status == TaskStatus.SUBMITTED and sub.decision is None


async def test_auto_confirm_refuses_decided_submission(session, clock, manager, employee) -> None:
    _task, sub = await submitted(session, clock, manager, employee)
    await svc.review_set_score(session, sub.id, manager, 80)
    with pytest.raises(DomainError, match="уже обработан"):
        await svc.review_auto_confirm(session, sub.id)
    assert not sub.auto_confirmed


async def test_manager_can_revise_auto_confirmed_score_within_week(session, clock, manager, employee) -> None:
    task, sub = await submitted(session, clock, manager, employee)
    await svc.review_auto_confirm(session, sub.id)
    assert auto.can_revise(task, sub, clock.now)
    assert auto.revise_until(sub) == clock.now + timedelta(days=7)

    clock.advance(days=6)
    await svc.review_revise_auto(session, sub.id, manager, 70, "Отчёт неполный")

    assert task.status == TaskStatus.DONE and task.final_score == 70
    assert sub.final_score == 70 and sub.decision == ReviewDecision.CHANGED
    assert sub.reviewer is manager and sub.review_comment == "Отчёт неполный"
    assert not sub.auto_confirmed and not auto.can_revise(task, sub, clock.now)
    (event,) = await events(session, task.id, EventType.SCORE_CHANGED)
    assert event.data["score"] == 70 and event.data["previous"] == 95 and event.data["after_auto"] is True


async def test_revise_is_closed_after_a_week_and_for_manual_decisions(session, clock, manager, employee) -> None:
    task, sub = await submitted(session, clock, manager, employee)
    await svc.review_auto_confirm(session, sub.id)
    clock.advance(days=7, minutes=1)
    assert not auto.can_revise(task, sub, clock.now)
    with pytest.raises(DomainError, match="7 дней"):
        await svc.review_revise_auto(session, sub.id, manager, 70)
    assert task.final_score == 95

    _task2, sub2 = await submitted(session, clock, manager, employee)
    await svc.review_confirm(session, sub2.id, manager)
    with pytest.raises(DomainError):
        await svc.review_revise_auto(session, sub2.id, manager, 70)


async def test_revise_validates_score(session, clock, manager, employee) -> None:
    _task, sub = await submitted(session, clock, manager, employee)
    await svc.review_auto_confirm(session, sub.id)
    with pytest.raises(DomainError, match="от 0 до 150"):
        await svc.review_revise_auto(session, sub.id, manager, 151)


async def test_proposal_weight_suggestion_is_stored(session, clock, employee) -> None:
    task = await proposed(session, clock, employee)

    await svc.set_proposal_weight(session, task.id, weight=15, source="ai", note="Небольшая справка")

    assert task.weight == 15 and task.status == TaskStatus.PROPOSED
    (event,) = await events(session, task.id, EventType.WEIGHT_SUGGESTED)
    assert event.actor is None and event.data == {"weight": 15, "source": "ai", "note": "Небольшая справка"}


async def test_auto_approve_proposal(session, clock, manager, employee) -> None:
    task = await proposed(session, clock, employee)
    await svc.set_proposal_weight(session, task.id, weight=15, source="ai", note=None)
    clock.advance(hours=24)

    approved = await svc.auto_approve_proposal(session, task.id)

    assert approved.status == TaskStatus.ACTIVE
    assert approved.weight == 15 and approved.priority == Priority.MEDIUM
    assert approved.manager_id is None  # решал не человек: результат получат все начальники
    assert approved.approved_at == clock.now and approved.accepted_at == clock.now
    (event,) = await events(session, task.id, EventType.APPROVED)
    assert event.actor is None and event.data["auto"] is True and event.data["weight"] == 15


async def test_auto_approve_refuses_passed_deadline_and_processed(session, clock, manager, employee) -> None:
    task = await proposed(session, clock, employee, days=0.5)
    clock.advance(hours=24)
    with pytest.raises(DomainError, match="Срок"):
        await svc.auto_approve_proposal(session, task.id)
    assert task.status == TaskStatus.PROPOSED

    other = await proposed(session, clock, employee)
    await svc.reject_proposal(session, other.id, manager)
    with pytest.raises(DomainError, match="уже обработано"):
        await svc.auto_approve_proposal(session, other.id)


async def test_week_tasks_lists_other_tasks_of_the_week(session, clock, manager, employee) -> None:
    task, _sub = await submitted(session, clock, manager, employee)
    proposal = await proposed(session, clock, employee)
    assert await svc.week_tasks(session, employee.id, proposal.deadline, exclude_task_id=proposal.id) == [
        (task.title, 20)
    ]


# =====================================================================================================
# Вес поручения (bot.ai.weigh)
# =====================================================================================================


async def test_weight_without_ai_is_default() -> None:
    suggestion = await weigh.suggest_weight(title="Справка", expected_result="Справка", plan=None, week_tasks=[])
    assert suggestion == weigh.WeightSuggestion(weight=10, source="rules")


@pytest.mark.parametrize(("raw", "expected"), [(17, 15), (18, 20), (2, 5), (90, 50), (20.0, 20)])
async def test_weight_from_ai_is_rounded_and_clamped(monkeypatch, set_env, raw, expected) -> None:
    set_env(AI_PROVIDER="auto", GEMINI_API_KEY="k" * 20)
    seen: dict[str, Any] = {}

    async def fake_generate_json(**kwargs: Any) -> tuple[dict, str]:
        seen.update(kwargs)
        return {"weight": raw, "note": "  Сопоставимо   с другими задачами "}, "model"

    monkeypatch.setattr(weigh, "generate_json", fake_generate_json)
    suggestion = await weigh.suggest_weight(
        title="Справка", expected_result="Справка по 5 договорам", plan="5 договоров",
        week_tasks=[("Анализ договоров", 20)], time_budget=8,
    )
    assert suggestion == weigh.WeightSuggestion(weight=expected, source="ai", note="Сопоставимо с другими задачами")
    assert seen["time_budget"] == 8
    assert "Анализ договоров" in seen["parts"][0] and "сумма весов 20 %" in seen["parts"][0]


@pytest.mark.parametrize("raw", [None, "много", 0, -5, True, float("nan")])
async def test_weight_bad_ai_answer_falls_back(monkeypatch, set_env, raw) -> None:
    set_env(AI_PROVIDER="auto", GEMINI_API_KEY="k" * 20)

    async def fake_generate_json(**kwargs: Any) -> tuple[dict, str]:
        return {"weight": raw, "note": None}, "model"

    monkeypatch.setattr(weigh, "generate_json", fake_generate_json)
    suggestion = await weigh.suggest_weight(title="Справка", expected_result="Справка", plan=None, week_tasks=[])
    assert suggestion.source == "rules" and suggestion.weight == 10


# =====================================================================================================
# Задание по расписанию
# =====================================================================================================


@pytest_asyncio.fixture
async def bot() -> AsyncIterator[Bot]:
    fake = Bot("42:TEST", session=FakeSession(), default=DefaultBotProperties(parse_mode="HTML"))
    try:
        yield fake
    finally:
        await fake.session.close()


def messages(bot: Bot, chat_id: int) -> list[m.SendMessage]:
    assert isinstance(bot.session, FakeSession)
    return [r for r in bot.session.requests if isinstance(r, m.SendMessage) and r.chat_id == chat_id]


def button_texts(message: m.SendMessage) -> list[str]:
    markup = message.reply_markup
    return [button.text for row in getattr(markup, "inline_keyboard", []) for button in row]


async def tick(bot: Bot, sessionmaker, clock, moment: datetime) -> dict[str, int]:
    clock.set(moment)
    return await jobs.run_auto_decisions(bot, sessionmaker, moment)


async def reload_task(sessionmaker, task_id: int) -> Task:
    async with sessionmaker() as fresh:
        task = await svc.get_task(fresh, task_id)
        assert task is not None
        return task


async def test_job_reminds_then_confirms_ai_score(session, sessionmaker, clock, manager, employee, bot) -> None:
    task, _sub = await submitted(session, clock, manager, employee)
    assert await tick(bot, sessionmaker, clock, local(2, 12)) == {"confirmed": 0, "approved": 0, "reminded": 0}

    assert (await tick(bot, sessionmaker, clock, local(3, 8, 55)))["reminded"] == 0
    assert (await tick(bot, sessionmaker, clock, local(3, 9, 5)))["reminded"] == 1
    (reminder,) = messages(bot, manager.tg_id)
    assert "подтвердится автоматически" in reminder.text and "95 %" in reminder.text
    assert (await tick(bot, sessionmaker, clock, local(3, 9, 10)))["reminded"] == 0  # второй раз не напоминаем
    assert (await reload_task(sessionmaker, task.id)).status == TaskStatus.SUBMITTED

    assert (await tick(bot, sessionmaker, clock, local(3, 12, 1)))["confirmed"] == 1
    done = await reload_task(sessionmaker, task.id)
    assert done.status == TaskStatus.DONE and done.final_score == 95
    assert done.last_submission is not None and done.last_submission.auto_confirmed

    to_manager = messages(bot, manager.tg_id)[-1]
    assert "подтверждена автоматически" in to_manager.text and "95 %" in to_manager.text
    assert any("Изменить оценку" in text for text in button_texts(to_manager))
    (to_employee,) = messages(bot, employee.tg_id)
    assert "95 %" in to_employee.text and "автоматически" in to_employee.text

    assert await tick(bot, sessionmaker, clock, local(3, 12, 6)) == {"confirmed": 0, "approved": 0, "reminded": 0}


@pytest.mark.parametrize(("score", "source"), [(90, "rules"), (120, "ai")])
async def test_job_leaves_rules_and_high_scores_to_manager(
    session, sessionmaker, clock, manager, employee, bot, score, source
) -> None:
    task, _sub = await submitted(session, clock, manager, employee, score=score, source=source)
    await tick(bot, sessionmaker, clock, local(2, 12))
    result = await tick(bot, sessionmaker, clock, local(5, 12))
    assert result == {"confirmed": 0, "approved": 0, "reminded": 0}
    assert (await reload_task(sessionmaker, task.id)).status == TaskStatus.SUBMITTED
    assert messages(bot, manager.tg_id) == []


async def test_job_waits_for_morning(session, sessionmaker, clock, manager, employee, bot) -> None:
    clock.set(local(2, 23))
    task, _sub = await submitted(session, clock, manager, employee)
    await tick(bot, sessionmaker, clock, local(2, 23))
    assert (await tick(bot, sessionmaker, clock, local(3, 23, 30)))["confirmed"] == 0  # тихие часы
    assert (await tick(bot, sessionmaker, clock, local(4, 7, 55)))["confirmed"] == 0
    assert (await tick(bot, sessionmaker, clock, local(4, 8, 1)))["confirmed"] == 1
    assert (await reload_task(sessionmaker, task.id)).status == TaskStatus.DONE


async def test_job_counts_old_items_from_first_run(session, sessionmaker, clock, manager, employee, bot) -> None:
    """Сдача ждала ещё до включения автоподтверждения — сутки отсчитываются от первого запуска задания."""
    task, _sub = await submitted(session, clock, manager, employee)
    assert (await tick(bot, sessionmaker, clock, local(5, 12)))["confirmed"] == 0
    assert (await reload_task(sessionmaker, task.id)).status == TaskStatus.SUBMITTED
    assert (await tick(bot, sessionmaker, clock, local(6, 12, 1)))["confirmed"] == 1


async def test_job_skips_when_manager_decided(session, sessionmaker, clock, manager, employee, bot) -> None:
    task, sub = await submitted(session, clock, manager, employee)
    await tick(bot, sessionmaker, clock, local(2, 12))
    await svc.review_set_score(session, sub.id, manager, 60)
    await session.commit()
    assert await tick(bot, sessionmaker, clock, local(3, 12, 1)) == {"confirmed": 0, "approved": 0, "reminded": 0}
    assert (await reload_task(sessionmaker, task.id)).final_score == 60


async def test_job_approves_proposal_with_suggested_weight(
    session, sessionmaker, clock, manager, employee, bot
) -> None:
    task = await proposed(session, clock, employee)
    await svc.set_proposal_weight(session, task.id, weight=15, source="ai", note=None)
    await session.commit()
    await tick(bot, sessionmaker, clock, local(2, 12))

    assert (await tick(bot, sessionmaker, clock, local(3, 9, 5)))["reminded"] == 1
    (reminder,) = messages(bot, manager.tg_id)
    assert "будет принято автоматически" in reminder.text and "15 %" in reminder.text
    assert any("Подтвердить" in text for text in button_texts(reminder))

    assert (await tick(bot, sessionmaker, clock, local(3, 12, 1)))["approved"] == 1
    active = await reload_task(sessionmaker, task.id)
    assert active.status == TaskStatus.ACTIVE and active.weight == 15 and active.priority == Priority.MEDIUM

    to_manager = messages(bot, manager.tg_id)[-1]
    assert "принято автоматически" in to_manager.text and "15 %" in to_manager.text
    (to_employee,) = messages(bot, employee.tg_id)
    assert "принято автоматически" in to_employee.text


async def test_job_asks_ai_for_weight_when_none_was_stored(
    session, sessionmaker, clock, manager, employee, bot, monkeypatch
) -> None:
    task = await proposed(session, clock, employee)
    await tick(bot, sessionmaker, clock, local(2, 12))

    async def fake_suggest(**kwargs: Any) -> weigh.WeightSuggestion:
        assert kwargs["title"] == "Подготовить справку"
        return weigh.WeightSuggestion(weight=25, source="ai", note="Большая работа")

    monkeypatch.setattr(weigh, "suggest_weight", fake_suggest)
    assert (await tick(bot, sessionmaker, clock, local(3, 12, 1)))["approved"] == 1
    assert (await reload_task(sessionmaker, task.id)).weight == 25


async def test_job_leaves_proposal_with_passed_deadline(session, sessionmaker, clock, manager, employee, bot) -> None:
    task = await proposed(session, clock, employee, days=0.5)
    await tick(bot, sessionmaker, clock, local(2, 12))
    assert (await tick(bot, sessionmaker, clock, local(3, 12, 1)))["approved"] == 0
    assert (await reload_task(sessionmaker, task.id)).status == TaskStatus.PROPOSED


async def test_job_disabled(session, sessionmaker, clock, manager, employee, bot, set_env) -> None:
    set_env(AUTO_CONFIRM_HOURS="0")
    task, _sub = await submitted(session, clock, manager, employee)
    assert await tick(bot, sessionmaker, clock, local(9, 12)) == {"confirmed": 0, "approved": 0, "reminded": 0}
    assert (await reload_task(sessionmaker, task.id)).status == TaskStatus.SUBMITTED


async def test_feature_start_is_recorded_once(sessionmaker, clock, bot) -> None:
    await tick(bot, sessionmaker, clock, local(2, 12))
    await tick(bot, sessionmaker, clock, local(4, 12))
    async with sessionmaker() as fresh:
        rows = list(await fresh.scalars(select(jobs.JobLog).where(jobs.JobLog.job == jobs.JOB_AUTO)))
    assert len(rows) == 1 and rows[0].created_at == local(2, 12)
