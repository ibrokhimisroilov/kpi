"""Прерванные диалоги и «чужие» кнопки: в чате не остаётся живых кнопок, а ответы объясняют, что произошло.

* /cancel и «✖️ Отмена» в любом диалоге убирают кнопки его последнего вопроса и говорят, что стало
  с отменённым («Задача не создана», «Предложение по-прежнему ждёт решения» …).
* Сотрудника повысили посреди черновика поручения — черновик закрывается, задачу он не создаст.
* Заблокированный нажимает старую кнопку — ему объясняют, что доступ закрыт.
* Новая сдача поверх начатой предупреждает, что прежние ответы и файлы не сохранены.
* «📋 Открыть» в уведомлении присылает карточку новым сообщением — текст уведомления остаётся в чате.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from bot import notify
from bot.db.models import Priority, Role, Task, TaskSource, TaskStatus, User, UserStatus
from bot.services import tasks as tasks_svc
from bot.ui.callbacks import TaskCB
from bot.ui.texts import BTN_MY_TASKS, BTN_NEW_TASK, BTN_PROPOSE, BTN_SUBMIT, BTN_TASKS
from bot.utils.dates import utcnow

from .fakebot import MANAGER_TG_ID, BotHarness

pytestmark = pytest.mark.asyncio

MGR = MANAGER_TG_ID  # Петрова — начальник
EMP = 2001           # Иванов — сотрудник


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


async def change_user(h: BotHarness, tg_id: int, **fields: object) -> None:
    """Роль/статус поменялись в обход «👥 Сотрудники» (диалог пользователя не сброшен)."""
    async with h.db() as s:
        user = await s.get(User, (await h.get_user(tg_id)).id)
        for name, value in fields.items():
            setattr(user, name, value)
        await s.commit()


def live_buttons(h: BotHarness, chat_id: int) -> list[str]:
    return [text for msg in h.messages(chat_id) for text in msg.button_texts]


# --- /cancel и «✖️ Отмена» ------------------------------------------------------------------------


async def test_cancel_task_creation_removes_buttons_of_the_last_question(app):
    """Петрова дошла в «➕ Поставить задачу» до выбора срока и набрала /cancel. Под вопросом
    о сроке больше нет кнопок «Завтра»/«Пятница» (раньше они оставались и отвечали «устарела»),
    бот пишет «Действие отменено. Задача не создана.», задачи нет. Если старый клиент всё же
    пришлёт «Завтра», черновик не оживает."""
    h = app
    await office(h)
    await h.press_menu(MGR, BTN_NEW_TASK)
    await h.press_button(MGR, "Иванов")
    await h.send_text(MGR, "Анализ договоров")
    await h.send_text(MGR, "проверить 100 договоров и представить отчёт")
    await h.press_button(MGR, "Принять")
    question = h.last_message(MGR).message_id
    tomorrow = h.find_button(MGR, "Завтра", question)

    log = await h.send_command(MGR, "cancel")
    assert "Действие отменено. Задача не создана." in log.text
    assert h.buttons(MGR, question) == []
    assert await h.get_state(MGR) is None

    log = await h.press(MGR, tomorrow, question)
    assert log.answers
    assert await h.get_state(MGR) is None
    assert await h.scalars(select(Task)) == []


async def test_cancel_task_edit_keeps_task_unchanged(app):
    """Петрова начала менять срок задачи и передумала (/cancel): у вопроса о сроке убраны кнопки,
    бот пишет «Задача не изменена.», срок прежний."""
    h = app
    mgr, emp = await office(h)
    task_id = await add_task(h, mgr, emp)
    before = (await h.get_task(task_id)).deadline
    await h.press(MGR, TaskCB(action="open", task_id=task_id))
    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "Срок")
    question = h.last_message(MGR).message_id
    assert "Завтра" in " ".join(h.buttons(MGR, question))

    log = await h.send_command(MGR, "cancel")
    assert "Действие отменено. Задача не изменена." in log.text
    assert h.buttons(MGR, question) == []
    assert (await h.get_task(task_id)).deadline == before


async def test_cancel_in_proposal_approval_says_proposal_still_waits(app):
    """Петрова начала подтверждать поручение Иванова прямо из уведомления (выбор веса) и нажала
    «✖️ Отмена». Бот уточняет, что поручение никуда не делось и ждёт решения в «📥 Предложения»."""
    h = app
    _, emp = await office(h)
    async with h.db() as s:
        task = await tasks_svc.propose_task(
            s, employee=await s.get(User, emp.id), title="Справка для юристов",
            expected_result="Подготовить справку", deadline=utcnow() + timedelta(days=5),
        )
        await s.commit()
    await h.press(MGR, TaskCB(action="approve", task_id=task.id))
    log = await h.press_button(MGR, "Отмена")
    assert "Действие отменено. Предложение по-прежнему ждёт решения — «📥 Предложения»." in log.text
    assert (await h.get_task(task.id)).status == TaskStatus.PROPOSED


async def test_cancel_in_submission_says_result_not_sent(app):
    """Иванов начал сдавать результат и нажал «✖️ Отмена»: результат не отправлен, и бот
    подсказывает, где сдать его потом."""
    h = app
    mgr, emp = await office(h)
    await add_task(h, mgr, emp)
    await h.press_menu(EMP, BTN_SUBMIT)
    await h.press_button(EMP, "Анализ договоров")
    log = await h.press_button(EMP, "Отмена")
    assert "Действие отменено. Результат не отправлен — сдать его можно в любой момент: «✅ Сдать результат»." in log.text


# --- Роль или доступ поменялись посреди диалога ---------------------------------------------------


async def test_employee_promoted_mid_draft_cannot_finish_proposal(app):
    """Иванов вносит устное поручение, и в этот момент его роль меняют на «начальник» в обход
    «👥 Сотрудники» (там диалог сбросился бы сам). Следующий его ответ не принимается как шаг
    черновика: бот пишет, что вносить поручения могут только сотрудники, черновик закрыт,
    задача не создана."""
    h = app
    await office(h)
    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, "Справка для юристов")
    assert await h.get_state(EMP) == "ProposeTaskSG:result"

    await change_user(h, EMP, role=Role.MANAGER)
    log = await h.send_text(EMP, "Подготовить справку по 10 договорам")
    assert "Вносить поручения могут только сотрудники" in log.text
    assert "Формулирую" not in log.text
    assert await h.get_state(EMP) is None
    assert await h.scalars(select(Task)) == []


async def test_menu_button_still_works_after_promotion_mid_draft(app):
    """Тот же случай, но вместо ответа Иванов (уже начальник) жмёт кнопку меню «📋 Задачи»:
    кнопка меню не перехватывается — открывается список задач, черновик сброшен."""
    h = app
    await office(h)
    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, "Справка для юристов")
    await change_user(h, EMP, role=Role.MANAGER)

    log = await h.send_text(EMP, BTN_TASKS)
    assert "📋 Задачи — В работе" in log.text
    assert await h.get_state(EMP) is None


async def test_stale_draft_button_after_promotion_explains(app):
    """Иванова повысили, пока у него был открыт шаг «срок» черновика; он жмёт «Завтра» —
    alert «Вносить поручения могут только сотрудники», кнопки убраны."""
    h = app
    await office(h)
    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, "Справка для юристов")
    await h.send_text(EMP, "Подготовить справку по 10 договорам")
    await h.press_button(EMP, "Принять")
    question = h.last_message(EMP).message_id
    assert "Завтра" in " ".join(h.buttons(EMP, question))

    await change_user(h, EMP, role=Role.MANAGER)
    log = await h.press_button(EMP, "Завтра", question)
    assert "Вносить поручения могут только сотрудники" in log.alert
    assert h.buttons(EMP, question) == []
    assert await h.get_state(EMP) is None


async def test_blocked_user_pressing_old_button_learns_access_is_closed(app):
    """Иванова заблокировали. Кнопка «✅ Принял в работу» в старом уведомлении о задаче отвечает
    отказом и ничего не меняет. А кнопка из диалога, который сбросили при блокировке («⏭ Пропустить»),
    не пишет загадочное «Кнопка устарела» — бот объясняет alert'ом: доступ закрыт."""
    h = app
    mgr, emp = await office(h)
    task_id = await add_task(h, mgr, emp)
    await h.capture(notify.notify_new_task(h.bot, await h.get_task(task_id)))
    await change_user(h, EMP, status=UserStatus.BLOCKED)

    log = await h.press_button(EMP, "Принял в работу")
    assert log.alert == "⛔ Недостаточно прав для этого действия."
    assert (await h.get_task(task_id)).accepted_at is None

    log = await h.press(EMP, "k:skip:skip")  # кнопка «Пропустить» из давно закрытого диалога
    assert log.alert == "⛔ Доступ закрыт. Обратитесь к начальнику."
    assert log.answers[0].show_alert


# --- Сдача результата поверх начатой --------------------------------------------------------------


async def test_new_submission_warns_that_started_one_is_dropped(app):
    """Иванов начал сдавать «Анализ договоров» (уже ответил, что сделано), а потом нажал
    «📤 Сдать результат» по другой задаче. Новая сдача начинается, но бот предупреждает, что
    начатая по задаче #1 отменена и её ответы не сохранены. Повторный старт той же задачи
    без ответов — без предупреждения."""
    h = app
    mgr, emp = await office(h)
    first = await add_task(h, mgr, emp)
    second = await add_task(h, mgr, emp, "Отчёт по закупкам")
    await h.press(EMP, TaskCB(action="submit", task_id=first))
    await h.send_text(EMP, "Проверено 60 договоров из 100")

    log = await h.press(EMP, TaskCB(action="submit", task_id=second))
    assert f"Начатая сдача по задаче #{first} отменена" in log.text
    assert "Шаг 1 из 4. Что фактически сделано?" in log.text
    assert (await h.get_data(EMP))["task_id"] == second

    log = await h.press(EMP, TaskCB(action="submit", task_id=second))
    assert "отменена" not in log.text and "начата заново" not in log.text


# --- «📋 Открыть» в уведомлении -------------------------------------------------------------------


async def test_open_from_notification_keeps_notification_text(app):
    """Иванову пришло «🆕 Вам поставлена новая задача». Он жмёт «📋 Открыть» — карточка задачи
    приходит новым сообщением, а уведомление остаётся в чате как было (раньше оно превращалось
    в карточку). Открытие из списка «📋 Мои задачи» по-прежнему просто листает экран."""
    h = app
    mgr, emp = await office(h)
    task_id = await add_task(h, mgr, emp)
    await h.capture(notify.notify_new_task(h.bot, await h.get_task(task_id)))
    notice = h.find_message(EMP, "Вам поставлена новая задача")

    await h.press_button(EMP, "Открыть", notice.message_id)
    assert h.api.messages[(EMP, notice.message_id)].content.startswith("🆕 Вам поставлена новая задача")
    card = h.last_message(EMP)
    assert card.message_id != notice.message_id
    assert card.content.startswith(f"📌 Задача #{task_id}")
    assert "⏳ Вы ещё не подтвердили получение" in card.content  # карточка обращается к исполнителю

    await h.press_menu(EMP, BTN_MY_TASKS)
    listing = h.last_message(EMP).message_id
    await h.press(EMP, TaskCB(action="open", task_id=task_id), listing)
    assert h.last_message(EMP).message_id == listing
    assert h.api.messages[(EMP, listing)].content.startswith(f"📌 Задача #{task_id}")
