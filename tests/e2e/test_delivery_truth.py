"""Руководитель видит правду о доставке уведомлений сотруднику.

После каждого решения руководителя бот пишет сотруднику (новая задача, правка, отмена, решение
по поручению, итог проверки, доработка). Если сотрудник заблокировал бота или у него больше нет
доступа (заблокирован в боте), сообщение не доходит — и руководитель не должен читать
«Исполнитель получил уведомление». Вместо этого — «⚠️ Уведомление не доставлено … сообщите лично».
Когда сотрудник на связи, всё как раньше: «… получил уведомление».

Бот целиком (bot.main.build_dispatcher) на фейковом Telegram API, AI выключен (правила).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from bot.db.models import Priority, Task, TaskSource, TaskStatus, User, UserStatus
from bot.services import tasks as tasks_svc
from bot.ui.callbacks import TaskCB
from bot.ui.texts import BTN_NEW_TASK
from bot.utils.dates import utcnow

from .fakebot import MANAGER_TG_ID, BotHarness

pytestmark = pytest.mark.asyncio

MGR = MANAGER_TG_ID  # Петрова — руководитель
EMP = 2001           # Иванов — сотрудник
NOT_DELIVERED = "Уведомление не доставлено"
DELIVERED = "получил уведомление"


async def office(h: BotHarness) -> tuple[User, User]:
    mgr = await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    emp = await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    await h.send_command(MGR, "start")
    await h.send_command(EMP, "start")
    return mgr, emp


async def add_task(h: BotHarness, mgr: User, emp: User, title: str = "Анализ договоров") -> int:
    async with h.db() as s:
        task = Task(
            title=title,
            expected_result="Проверить 100 договоров и представить отчёт",
            plan_value=100,
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
        await s.commit()
        return task.id


async def submitted(h: BotHarness, mgr: User, emp: User, title: str) -> int:
    """Задача, по которой Иванов уже сдал результат, а правила предложили 110 %."""
    task_id = await add_task(h, mgr, emp, title)
    async with h.db() as s:
        employee = await s.get(User, emp.id)
        sub = await tasks_svc.submit_result(s, task_id, employee, fact_text="Проверено 110 договоров", fact_value=110)
        await tasks_svc.record_evaluation(s, sub.id, score=110, rationale="План перевыполнен.", source="rules")
        await s.commit()
    return task_id


async def proposal(h: BotHarness, emp: User, title: str) -> int:
    async with h.db() as s:
        employee = await s.get(User, emp.id)
        task = await tasks_svc.propose_task(
            s, employee=employee, title=title, expected_result="Подготовить справку",
            deadline=utcnow() + timedelta(days=5),
        )
        await s.commit()
        return task.id


async def set_status(h: BotHarness, tg_id: int, status: UserStatus) -> None:
    """Статус пользователя поменялся (например, руководитель заблокировал его в «👥 Сотрудники»)."""
    user_id = (await h.get_user(tg_id)).id
    async with h.db() as s:
        (await s.get(User, user_id)).status = status
        await s.commit()


async def test_new_task_for_employee_who_blocked_the_bot(app):
    """Иванов заблокировал бота. Петрова ставит ему задачу: задача создана, но в итоговой карточке
    честно сказано, что уведомление не дошло и сообщить о задаче нужно лично."""
    h = app
    await office(h)
    h.api.blocked_chats.add(EMP)

    await h.press_menu(MGR, BTN_NEW_TASK)
    await h.press_button(MGR, "Иванов")
    await h.send_text(MGR, "Анализ договоров")
    await h.send_text(MGR, "проверить 100 договоров и представить отчёт")
    await h.press_button(MGR, "Принять")
    await h.press_button(MGR, "Завтра")
    await h.press_button(MGR, "Средний")
    await h.press_button(MGR, "20 %")
    log = await h.press_button(MGR, "Создать")

    assert log.alert == "✅ Задача поставлена"
    card = h.last_text(MGR)
    assert card.startswith("✅ Задача #1 поставлена")
    assert NOT_DELIVERED in card and "сообщите ему лично" in card
    assert (await h.get_task(1)).status == TaskStatus.ACTIVE


async def test_edit_and_cancel_tell_whether_employee_was_notified(app):
    """Петрова меняет вес задачи: пока Иванов заблокировал бота — «уведомление не доставлено»;
    он снова на связи — «Исполнитель получил уведомление». Потом Иванова блокируют в самом боте
    и Петрова отменяет задачу — писать ему нельзя, карточка так и говорит."""
    h = app
    mgr, emp = await office(h)
    task_id = await add_task(h, mgr, emp)
    h.api.blocked_chats.add(EMP)

    await h.press(MGR, TaskCB(action="open", task_id=task_id))
    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "Вес")
    log = await h.press_button(MGR, "30 %")
    card = h.last_text(MGR)
    assert card.startswith("✅ Изменено: вес.")
    assert NOT_DELIVERED in card and DELIVERED not in card
    assert log.answers  # на нажатие ответили до попытки уведомить
    assert (await h.get_task(task_id)).weight == 30

    h.api.blocked_chats.discard(EMP)
    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "Вес")
    log = await h.press_button(MGR, "40 %")
    assert h.last_text(MGR).startswith("✅ Изменено: вес. Исполнитель получил уведомление.")
    assert "• Вес: 30 % → 40 %" in log.to(EMP).text

    await set_status(h, EMP, UserStatus.BLOCKED)
    await h.press_button(MGR, "Отменить")
    await h.press_button(MGR, "Да, отменить")
    log = await h.press_button(MGR, "Без причины")
    card = h.last_text(MGR)
    assert card.startswith(f"🚫 Задача #{task_id} отменена.")
    assert NOT_DELIVERED in card
    assert not log.to(EMP).texts
    assert (await h.get_task(task_id)).status == TaskStatus.CANCELLED


async def test_review_decisions_when_employee_blocked_the_bot(app):
    """Иванов заблокировал бота, а у Петровой три его результата на проверке. Она подтверждает
    оценку, меняет вторую и возвращает третью на доработку — все решения сохраняются, но после
    каждого бот предупреждает, что Иванов об этом не узнал."""
    h = app
    mgr, emp = await office(h)
    confirm_id = await submitted(h, mgr, emp, "Отчёт по закупкам")
    change_id = await submitted(h, mgr, emp, "Сверка с поставщиками")
    rework_id = await submitted(h, mgr, emp, "Реестр договоров")
    h.api.blocked_chats.add(EMP)

    await h.press(MGR, TaskCB(action="review", task_id=confirm_id))
    log = await h.press_button(MGR, "Подтвердить 110 %")
    assert log.alert == "✅ Оценка подтверждена"
    assert "Подтверждено: 110 %" in log.to(MGR).text and NOT_DELIVERED in log.to(MGR).text

    await h.press(MGR, TaskCB(action="review", task_id=change_id))
    await h.press_button(MGR, "Изменить оценку")
    await h.press_button(MGR, "90 %")
    log = await h.press_button(MGR, "Пропустить")
    assert "Оценка изменена: 90 %" in log.to(MGR).text
    assert NOT_DELIVERED in log.to(MGR).text and "Сотруднику отправлено уведомление" not in log.to(MGR).text

    await h.press(MGR, TaskCB(action="review", task_id=rework_id))
    await h.press_button(MGR, "На доработку")
    await h.send_text(MGR, "Добавьте реестр нарушений")
    log = await h.press_button(MGR, "Завтра")
    assert "Возвращено на доработку" in log.to(MGR).text and NOT_DELIVERED in log.to(MGR).text

    statuses = [(await h.get_task(i)).status for i in (confirm_id, change_id, rework_id)]
    assert statuses == [TaskStatus.DONE, TaskStatus.DONE, TaskStatus.REWORK]


async def test_review_decision_reaches_employee_normally(app):
    """Иванов на связи: после подтверждения оценки Петрова видит обычный итог (без предупреждений),
    а Иванов получает итоговую оценку."""
    h = app
    mgr, emp = await office(h)
    task_id = await submitted(h, mgr, emp, "Отчёт по закупкам")
    await h.press(MGR, TaskCB(action="review", task_id=task_id))
    log = await h.press_button(MGR, "Подтвердить 110 %")
    assert NOT_DELIVERED not in log.to(MGR).text
    assert "Итоговая оценка: 110 %" in log.to(EMP).text


async def test_proposal_decisions_when_employee_blocked_the_bot(app):
    """Иванов внёс три поручения и заблокировал бота. Петрова подтверждает одно, отклоняет другое,
    правит третье — решения сохраняются, а карточки честно говорят, что Иванов уведомлений не получил."""
    h = app
    _, emp = await office(h)
    approve_id = await proposal(h, emp, "Справка для юристов")
    reject_id = await proposal(h, emp, "Лишняя справка")
    edit_id = await proposal(h, emp, "Справка по закупкам")
    h.api.blocked_chats.add(EMP)

    await h.press(MGR, TaskCB(action="approve", task_id=approve_id))
    await h.press_button(MGR, "20 %")
    log = await h.press_button(MGR, "Средний")
    assert log.alert == "✅ Подтверждено"
    text = log.to(MGR).text
    assert f"Поручение #{approve_id} в работе" in text and NOT_DELIVERED in text and DELIVERED not in text

    await h.press(MGR, TaskCB(action="reject", task_id=reject_id))
    log = await h.press_button(MGR, "Пропустить")
    text = log.to(MGR).text
    assert f"Предложение #{reject_id} отклонено" in text and NOT_DELIVERED in text and DELIVERED not in text

    await h.press(MGR, TaskCB(action="pedit", task_id=edit_id))
    await h.press_button(MGR, "Название")
    log = await h.send_text(MGR, "Справка по закупкам за III квартал")
    text = log.to(MGR).text
    assert "Изменено: название." in text and NOT_DELIVERED in text and DELIVERED not in text

    statuses = [(await h.get_task(i)).status for i in (approve_id, reject_id, edit_id)]
    assert statuses == [TaskStatus.ACTIVE, TaskStatus.REJECTED, TaskStatus.PROPOSED]
