"""Автоподтверждение в чате (SPEC.md §12.2): бот подтверждает оценку AI за начальника, начальник меняет её
кнопкой «✏️ Изменить оценку»; поручение сотрудника приходит начальнику с предложенным весом.

Бот целиком на фейковом Telegram API (tests/e2e/fakebot.py). Тихие часы выключены: задание запускается
по настоящим часам, а ночью (21:00–8:00) оно ничего не делает.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import select

from bot.db.models import Priority, ReviewDecision, Submission, Task, TaskSource, TaskStatus, User
from bot.scheduler import jobs
from bot.services import proposal_flow
from bot.services import tasks as tasks_svc
from bot.utils.dates import utcnow

from .fakebot import MANAGER_TG_ID, BotHarness

pytestmark = pytest.mark.asyncio

MGR = MANAGER_TG_ID
EMP = 2001


@pytest.fixture(autouse=True)
def _no_quiet_hours(set_env: Callable[..., None]) -> None:
    set_env(QUIET_HOURS_START="0", QUIET_HOURS_END="0")


async def _team(h: BotHarness) -> tuple[User, User]:
    mgr = await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    emp = await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    await h.send_command(MGR, "start")
    await h.send_command(EMP, "start")
    return mgr, emp


async def _submitted(h: BotHarness, mgr: User, emp: User, *, ai_score: float = 95) -> tuple[int, int]:
    """Задача Иванова со сданным результатом и оценкой AI (как после «📤 Отправить»)."""
    async with h.db() as s:
        task = Task(
            title="Анализ договоров",
            expected_result="Проверить 100 договоров и представить отчёт",
            plan_value=100.0,
            plan_unit="договоров",
            deadline=utcnow() + timedelta(days=3),
            priority=Priority.MEDIUM,
            weight=20,
            status=TaskStatus.ACTIVE,
            source=TaskSource.MANAGER,
            assignee_id=emp.id,
            created_by_id=mgr.id,
            manager_id=mgr.id,
        )
        s.add(task)
        await s.flush()
        employee = await s.get(User, emp.id)
        sub = await tasks_svc.submit_result(s, task.id, employee, fact_text="Проверено 95 договоров", fact_value=95)
        await tasks_svc.record_evaluation(s, sub.id, score=ai_score, rationale="План выполнен на 95 %.", source="ai")
        await s.commit()
        return task.id, sub.id


async def _auto_confirm(h: BotHarness) -> None:
    """Сутки прошли, начальник не ответил: первый запуск задания отмечает начало, второй — подтверждает."""
    now = utcnow()
    await jobs.run_auto_decisions(h.bot, h.sessionmaker, now)
    log = await h.capture(jobs.run_auto_decisions(h.bot, h.sessionmaker, now + timedelta(hours=24, minutes=1)))
    assert log.result == {"confirmed": 1, "approved": 0, "reminded": 0}, log.result


async def test_manager_revises_auto_confirmed_score(app: BotHarness) -> None:
    h = app
    mgr, emp = await _team(h)
    task_id, sub_id = await _submitted(h, mgr, emp)
    await _auto_confirm(h)

    to_employee = h.last_text(EMP)
    assert "Итоговая оценка: 95 %" in to_employee and "подтверждена автоматически" in to_employee
    notice = h.find_message(MGR, "Оценка подтверждена автоматически: 95 %")
    assert "Изменить её можно до" in notice.content
    assert any("Изменить оценку" in text for text in notice.button_texts)

    await h.press_button(MGR, "Изменить оценку", notice.message_id)
    prompt = h.last_text(MGR)
    assert "Изменение автоматически подтверждённой оценки" in prompt and "Сейчас: 95 %" in prompt
    await h.press_button(MGR, "80 %")
    assert "Комментарий к оценке?" in h.last_text(MGR)
    log = await h.send_text(MGR, "Отчёт без перечня нарушений")

    assert "Оценка изменена: 95 % → 80 %" in log.to(MGR).text
    to_employee = log.to(EMP).text
    assert "Итоговая оценка: 80 %" in to_employee and "Отчёт без перечня нарушений" in to_employee
    assert await h.get_state(MGR) is None
    task = await h.get_task(task_id)
    sub = await h.scalar(select(Submission).where(Submission.id == sub_id))
    assert task.status == TaskStatus.DONE and task.final_score == 80
    assert sub.decision == ReviewDecision.CHANGED and sub.reviewer_id == mgr.id

    # Второй раз изменить нельзя: оценку уже выставил начальник.
    again = await h.press_button(MGR, "Изменить оценку", notice.message_id)
    assert "автоматически подтверждённую" in again.alert
    assert (await h.get_task(task_id)).final_score == 80


async def test_revise_button_in_done_task_card(app: BotHarness) -> None:
    h = app
    mgr, emp = await _team(h)
    task_id, _sub_id = await _submitted(h, mgr, emp)
    await _auto_confirm(h)

    await h.press_button(MGR, "Открыть", h.find_message(MGR, "Оценка подтверждена автоматически").message_id)
    card = h.last_text(MGR)
    assert f"Задача #{task_id}" in card and "подтверждена автоматически" in card
    assert "✏️ Изменить оценку" in h.buttons(MGR)
    # Исполнитель оценку менять не может: такой кнопки у него нет.
    await h.press_button(EMP, "Открыть")
    assert "✏️ Изменить оценку" not in h.buttons(EMP)


async def test_proposal_reaches_manager_with_suggested_weight(app: BotHarness) -> None:
    h = app
    _mgr, emp = await _team(h)
    async with h.db() as s:
        employee = await s.get(User, emp.id)
        task = await tasks_svc.propose_task(
            s, employee=employee, title="Подготовить справку", expected_result="Справка по 5 договорам",
            deadline=utcnow() + timedelta(days=3),
        )
        await s.commit()
        log = await h.capture(proposal_flow.run_after_propose(h.bot, s, task))
    assert log.result == 1
    card = log.to(MGR).text
    assert "Предлагаемый вес: 10 %" in card and "будет принято автоматически" in card

    await h.press_button(MGR, "Подтвердить")
    assert "Предлагаемый вес: 10 %" in h.last_text(MGR)
    assert "💡 10 %" in h.buttons(MGR)
    await h.press_button(MGR, "💡 10 %")
