"""Сценарии регистрации и управления сотрудниками (bot/handlers/start.py, bot/handlers/users_admin.py).

Пользовательские истории: начальники из ADMIN_IDS, анкета сотрудника (ФИО → должность),
заявка начальнику, подтверждение/отклонение (в том числе вторым начальником), блокировка,
смена роли, защита последнего начальника, /help, /menu, /cancel, сброс диалога кнопкой меню,
листание списка сотрудников. Проверяется то, что видят люди (тексты, кнопки, alert, меню), и БД.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from bot.config import get_settings
from bot.db.models import Role, Task, User, UserStatus
from bot.ui.callbacks import PickCB, TaskCB, UserCB
from bot.ui.texts import (
    BTN_HELP,
    BTN_MY_TASKS,
    BTN_NEW_TASK,
    BTN_PROPOSE,
    BTN_STAFF,
    EMPLOYEE_MENU_LAYOUT,
    MANAGER_MENU_LAYOUT,
)

from .fakebot import MANAGER_TG_ID, BotHarness

pytestmark = pytest.mark.asyncio

MGR2 = 1002  # второй начальник из ADMIN_IDS (фикстура two_admins)
EMP = 2001
EMP2 = 2002
ADMIN_TG = 7001  # админ по отметке в базе (не из ADMIN_IDS)

MANAGER_MENU = [text for row in MANAGER_MENU_LAYOUT for text in row]
EMPLOYEE_MENU = [text for row in EMPLOYEE_MENU_LAYOUT for text in row]

NO_RIGHTS = "Недостаточно прав"
PENDING = "Заявка на рассмотрении"
BLOCKED = "Доступ закрыт"


# --- Хелперы ---------------------------------------------------------------------------------


LANG_BUTTONS = ["Русский", "Oʻzbekcha"]


def form_messages(h: BotHarness, chat_id: int) -> list:
    """Сообщения чата без выбора языка: его кнопки живут сами по себе и к анкете не относятся."""
    return [m for m in h.messages(chat_id) if m.button_texts != LANG_BUTTONS]


def _set_admins(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ADMIN_IDS", value)
    get_settings.cache_clear()


@pytest.fixture
def two_admins(app: BotHarness, monkeypatch: pytest.MonkeyPatch) -> BotHarness:
    """В ADMIN_IDS два начальника: 1001 и 1002."""
    _set_admins(monkeypatch, f"{MANAGER_TG_ID},{MGR2}")
    return app


@pytest.fixture
def no_admins(app: BotHarness, monkeypatch: pytest.MonkeyPatch) -> BotHarness:
    """ADMIN_IDS пуст: начальники только те, кого назначили в боте."""
    _set_admins(monkeypatch, "")
    return app


async def register(h: BotHarness, tg_id: int, full_name: str, position: str | None = None) -> User:
    """Сотрудник проходит анкету: /start → ФИО → должность (или «Пропустить»)."""
    await h.send_command(tg_id, "start")
    await h.send_text(tg_id, full_name)
    if position is None:
        await h.press_button(tg_id, "Пропустить")
    else:
        await h.send_text(tg_id, position)
    return await h.get_user(tg_id)


async def open_card(h: BotHarness, viewer: int, target_name: str) -> None:
    """Начальник открывает «👥 Сотрудники» и нажимает на человека."""
    if h.reply_keyboard(viewer) is None:
        await h.send_command(viewer, "start")
    await h.press_menu(viewer, BTN_STAFF)
    await h.press_button(viewer, target_name)


async def count(h: BotHarness, model: type) -> int:
    return await h.scalar(select(func.count()).select_from(model))


# --- Начальники из ADMIN_IDS ------------------------------------------------------------------


async def test_two_admins_get_manager_menu_without_questionnaire(two_admins):
    """Два начальника из ADMIN_IDS нажимают /start и сразу получают меню начальника —
    без анкеты и без подтверждения. Имя берётся из Telegram, повторный /start не плодит записи.
    Пока сотрудников нет, бот подсказывает, как их подключить."""
    h = two_admins
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна", last_name="Петрова")
    assert "Вы вошли как начальник" in h.last_text(MANAGER_TG_ID)
    assert "Сотрудников пока нет" in h.last_text(MANAGER_TG_ID)
    assert "t.me/test_bot" in h.last_text(MANAGER_TG_ID)
    assert h.reply_keyboard(MANAGER_TG_ID) == MANAGER_MENU

    await h.send_command(MGR2, "start", first_name="Олег")
    assert "Вы вошли как начальник" in h.last_text(MGR2)
    assert h.reply_keyboard(MGR2) == MANAGER_MENU

    await h.send_command(MANAGER_TG_ID, "start")
    assert await count(h, User) == 2
    anna, oleg = await h.get_user(MANAGER_TG_ID), await h.get_user(MGR2)
    assert (anna.full_name, anna.role, anna.status) == ("Анна Петрова", Role.MANAGER, UserStatus.ACTIVE)
    assert (oleg.full_name, oleg.role, oleg.status) == ("Олег", Role.MANAGER, UserStatus.ACTIVE)

    # «👥 Сотрудники»: только два начальника и подсказка, как подключить сотрудников.
    await h.press_menu(MGR2, BTN_STAFF)
    text = h.last_text(MGR2)
    assert "Сотрудников: 0 · Начальников: 2" in text
    assert "Пока в системе нет сотрудников" in text
    assert h.buttons(MGR2) == ["👔 Анна Петрова", "👔 Олег", "🔄 Обновить"]


async def test_admin_card_has_no_demote_or_block_buttons(two_admins):
    """Начальник открывает карточку другого начальника из ADMIN_IDS: кнопок «Сделать
    сотрудником» и «Заблокировать» нет (это всё равно запрещено) — вместо них пояснение.
    Подделанный callback получает понятный отказ, роль не меняется."""
    h = two_admins
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.send_command(MGR2, "start", first_name="Олег")

    await open_card(h, MANAGER_TG_ID, "Олег")
    assert "админ бота" in h.last_text(MANAGER_TG_ID)
    assert not h.has_button(MANAGER_TG_ID, "Сделать сотрудником")
    assert not h.has_button(MANAGER_TG_ID, "Заблокировать")
    assert h.has_button(MANAGER_TG_ID, "К списку сотрудников")

    oleg = await h.get_user(MGR2)
    log = await h.press(MANAGER_TG_ID, UserCB(action="role_emp", user_id=oleg.id))
    assert "админ бота" in log.alert
    log = await h.press(MANAGER_TG_ID, UserCB(action="block", user_id=oleg.id))
    assert "админ бота" in log.alert
    oleg = await h.get_user(MGR2)
    assert (oleg.role, oleg.status) == (Role.MANAGER, UserStatus.ACTIVE)
    assert not log.to(MGR2).texts  # Олегу ничего не пришло


# --- Анкета сотрудника -------------------------------------------------------------------------


async def test_full_name_validation_then_position_and_request_to_both_managers(two_admins):
    """Сотрудник нажимает /start и заполняет анкету. Бот терпеливо объясняет ошибки в ФИО
    (одно слово, цифры, слишком коротко/длинно, стикер вместо текста) и в должности.
    После анкеты оба начальника получают заявку с кнопками «Подтвердить»/«Отклонить»,
    сотрудник — «Заявка отправлена», меню у него пока нет."""
    h = two_admins
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.send_command(MGR2, "start", first_name="Олег")

    await h.send_command(EMP, "start", first_name="Иван", username="ivanov")
    assert "Шаг 1 из 2" in h.last_text(EMP)
    assert h.buttons(EMP) == ["✖️ Отмена"]
    assert h.reply_keyboard(EMP) is None
    assert await h.get_state(EMP) == "RegistrationSG:full_name"

    cases = [
        ("Иванов", "как минимум фамилию и имя"),
        ("И", "Слишком коротко"),
        ("Иванов 1van", "только буквы"),
        ("Иванов <b>Иван</b>", "только буквы"),
        ("Иванов " + "И" * 200, "не более 200 символов"),
    ]
    for wrong, hint in cases:
        log = await h.send_text(EMP, wrong)
        assert hint in log.text, wrong
        assert await h.get_state(EMP) == "RegistrationSG:full_name"
    await h.send_sticker(EMP)
    assert "ФИО обычным текстом" in h.last_text(EMP)
    # Кнопки остаются только у последнего вопроса — старые «Отмена» убраны.
    assert [m.message_id for m in form_messages(h, EMP) if m.buttons] == [h.last_message(EMP).message_id]

    await h.send_text(EMP, "  Иванов   Иван  Иванович ")
    assert "Приятно познакомиться, Иванов Иван Иванович" in h.last_text(EMP)
    assert "Шаг 2 из 2" in h.last_text(EMP)
    assert h.buttons(EMP) == ["⏭ Пропустить", "✖️ Отмена"]

    await h.send_text(EMP, "А")
    assert "Слишком коротко" in h.last_text(EMP)
    await h.send_sticker(EMP)
    assert "должность обычным текстом" in h.last_text(EMP)
    assert await h.get_state(EMP) == "RegistrationSG:position"

    log = await h.send_text(EMP, "Ведущий   специалист")
    assert "Заявка отправлена начальнику" in log.to(EMP).text
    assert "Ведущий специалист" in log.to(EMP).text
    assert h.reply_keyboard(EMP) is None
    assert await h.get_state(EMP) is None
    assert not any(m.buttons for m in form_messages(h, EMP))  # кнопки анкеты больше не нажать

    for manager in (MANAGER_TG_ID, MGR2):
        request = log.to(manager).text
        assert "Новая заявка на доступ" in request
        assert "Иванов Иван Иванович" in request
        assert "Должность: Ведущий специалист" in request
        assert "@ivanov" in request
        assert h.buttons(manager) == ["✅ Подтвердить", "❌ Отклонить"]

    user = await h.get_user(EMP)
    assert (user.full_name, user.position) == ("Иванов Иван Иванович", "Ведущий специалист")
    assert (user.role, user.status) == (Role.EMPLOYEE, UserStatus.PENDING)


async def test_special_characters_in_name_and_position_are_shown_as_is(app):
    """ФИО с апострофом и должность с «<», «&» доходят до начальника без искажений
    (HTML экранирован, Telegram не ругается) — и в заявке, и в карточке, и в списке."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await register(h, EMP, "Gʻulomov O'Connor Ali", "<b>Начальник</b> отдела R&D")

    request = h.last_text(MANAGER_TG_ID)
    assert "ФИО: Gʻulomov O'Connor Ali" in request
    assert "Должность: <b>Начальник</b> отдела R&D" in request

    await open_card(h, MANAGER_TG_ID, "O'Connor")
    assert "Должность: <b>Начальник</b> отдела R&D" in h.last_text(MANAGER_TG_ID)


async def test_registration_without_any_manager_explains_who_must_start_bot(no_admins, monkeypatch):
    """В боте ещё нет ни одного начальника (ADMIN_IDS пуст). Сотрудник заполняет анкету —
    бот честно говорит, что заявку некому подтвердить и что нужно начальнику."""
    h = no_admins
    log = await h.send_command(EMP, "start")
    assert "Шаг 1 из 2" in log.text
    await h.send_text(EMP, "Иванов Иван")
    await h.press_button(EMP, "Пропустить")
    text = h.last_text(EMP)
    assert "Анкета сохранена" in text
    assert "нет ни одного начальника" in text
    assert "ADMIN_IDS" in text
    assert (await h.get_user(EMP)).status == UserStatus.PENDING

    # Позже начальник (его ID добавили в ADMIN_IDS) запускает бота — видит ждущую заявку.
    _set_admins(monkeypatch, str(MANAGER_TG_ID))
    log = await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    assert "Новых заявок на доступ: 1" in log.text
    await h.press_menu(MANAGER_TG_ID, BTN_STAFF)
    await h.press_button(MANAGER_TG_ID, "Иванов")
    log = await h.press_button(MANAGER_TG_ID, "Подтвердить")
    assert "Доступ к боту открыт" in log.to(EMP).text


async def test_request_reaches_manager_even_if_another_manager_blocked_the_bot(two_admins):
    """Один из начальников заблокировал бота в Telegram. Заявка всё равно доходит до
    второго начальника, а сотрудник видит «Заявка отправлена»."""
    h = two_admins
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.send_command(MGR2, "start", first_name="Олег")
    h.api.blocked_chats.add(MANAGER_TG_ID)

    await register(h, EMP, "Иванов Иван")
    assert "Заявка отправлена начальнику" in h.last_text(EMP)
    assert "Новая заявка на доступ" in h.last_text(MGR2)
    log = await h.press_button(MGR2, "Подтвердить")
    assert "Доступ к боту открыт" in log.to(EMP).text


async def test_approved_user_who_blocked_the_bot_is_still_approved(app):
    """Сотрудник подал заявку и заблокировал бота. Начальник подтверждает — действие
    выполняется (без ошибок), доступ открыт; меню сотрудник увидит, когда вернётся (/start)."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await register(h, EMP, "Иванов Иван")
    h.api.blocked_chats.add(EMP)

    log = await h.press_button(MANAGER_TG_ID, "Подтвердить")
    assert log.alert == "✅ Заявка подтверждена"
    assert (await h.get_user(EMP)).status == UserStatus.ACTIVE

    h.api.blocked_chats.discard(EMP)
    await h.send_command(EMP, "start")
    assert h.reply_keyboard(EMP) == EMPLOYEE_MENU


async def test_manager_is_not_told_notification_was_sent_when_it_was_not(app):
    """Сотрудник подал заявку и заблокировал бота — уведомление о решении до него не дойдёт.
    Начальник не читает в карточке «пользователю отправлено уведомление»: карточка честно
    говорит, что уведомление не доставлено и сообщить нужно лично. Так же — при отклонении.
    Когда сотрудник на связи, карточка по-прежнему подтверждает отправку уведомления."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await register(h, EMP, "Иванов Иван")
    h.api.blocked_chats.add(EMP)

    log = await h.press_button(MANAGER_TG_ID, "Подтвердить")
    assert log.alert == "✅ Заявка подтверждена"
    card = h.last_text(MANAGER_TG_ID)
    assert "отправлено уведомление" not in card
    assert "Уведомление не доставлено" in card and "Сообщите ему лично" in card
    assert (await h.get_user(EMP)).status == UserStatus.ACTIVE

    await register(h, EMP2, "Петров Пётр")
    h.api.blocked_chats.add(EMP2)
    petrov = await h.get_user(EMP2)
    await h.press(MANAGER_TG_ID, UserCB(action="reject", user_id=petrov.id))
    card = h.last_text(MANAGER_TG_ID)
    assert "Заявка отклонена" in card and "Уведомление не доставлено" in card

    await register(h, 2003, "Сидоров Сидор")
    sidorov = await h.get_user(2003)
    log = await h.press(MANAGER_TG_ID, UserCB(action="approve", user_id=sidorov.id))
    assert "пользователю отправлено уведомление" in h.last_text(MANAGER_TG_ID)
    assert "Доступ к боту открыт" in log.to(2003).text


async def test_card_of_missing_user_and_unknown_action(app):
    """Кнопка с несуществующим пользователем (например, из очень старого сообщения):
    понятный alert «Пользователь не найден», ничего не ломается."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    log = await h.press(MANAGER_TG_ID, UserCB(action="manage", user_id=999))
    assert "не найден" in log.alert
    log = await h.press(MANAGER_TG_ID, UserCB(action="approve", user_id=999))
    assert "не найден" in log.alert
    log = await h.press(MANAGER_TG_ID, UserCB(action="staff", user_id=0, page=99))
    assert "Сотрудники" in log.text  # несуществующая страница -> последняя существующая


async def test_cancel_and_restart_in_the_middle_of_questionnaire(app):
    """Сотрудник передумал посреди анкеты: «Отмена» — анкета сброшена, ФИО не сохранено,
    бот подсказывает /start. Старая кнопка «Пропустить» из прерванной анкеты не завершает
    новую. /start посреди анкеты начинает её заново с первого шага, /cancel — тоже сбрасывает."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.send_command(EMP, "start")
    await h.send_text(EMP, "Иванов Иван")
    old_question = h.last_message(EMP).message_id
    skip_data = h.find_button(EMP, "Пропустить")

    log = await h.press_button(EMP, "Отмена")
    assert "Действие отменено" in log.text
    assert "Нажмите /start" in log.text
    assert await h.get_state(EMP) is None
    assert h.buttons(EMP, old_question) == []
    assert (await h.get_user(EMP)).full_name == ""

    # Новая анкета, но нажата старая «Пропустить» -> не засчитывается.
    await h.send_command(EMP, "start")
    await h.send_text(EMP, "Петров Пётр")
    log = await h.press(EMP, skip_data, old_question)
    assert "устарела" in log.alert
    assert await h.get_state(EMP) == "RegistrationSG:position"
    assert (await h.get_user(EMP)).full_name == ""
    assert "Новая заявка" not in "\n".join(h.sent_to(MANAGER_TG_ID))

    # /start посреди анкеты — заново с шага 1, кнопки старого вопроса убраны.
    log = await h.send_command(EMP, "start")
    assert "Шаг 1 из 2" in log.text
    assert await h.get_state(EMP) == "RegistrationSG:full_name"
    assert [m.message_id for m in form_messages(h, EMP) if m.buttons] == [h.last_message(EMP).message_id]

    # /cancel — тоже сброс.
    log = await h.send_command(EMP, "cancel")
    assert "Действие отменено" in log.text
    assert await h.get_state(EMP) is None
    assert not any(m.buttons for m in form_messages(h, EMP))

    # Текст после отмены — не ФИО, а подсказка.
    log = await h.send_text(EMP, "Сидоров Сидор")
    assert "Нажмите /start" in log.text
    assert (await h.get_user(EMP)).full_name == ""


async def test_double_press_skip_sends_one_request(app):
    """Сотрудник дважды быстро нажал «⏭ Пропустить»: заявка отправлена один раз, начальнику
    приходит одно уведомление, а второе нажатие объясняет статус — «Заявка на рассмотрении»."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.send_command(EMP, "start")
    await h.send_text(EMP, "Иванов Иван")
    question = h.last_message(EMP).message_id
    skip = h.find_button(EMP, "Пропустить")

    await asyncio.gather(h.press(EMP, skip, question), h.press(EMP, skip, question))
    requests = [text for text in h.sent_to(MANAGER_TG_ID) if "Новая заявка" in text]
    assert len(requests) == 1
    assert len([t for t in h.sent_to(EMP) if "Заявка отправлена" in t]) == 1
    assert any(PENDING in alert for alert in h.alerts_for(EMP))


async def test_admin_with_special_characters_in_telegram_name(two_admins):
    """Имя начальника в Telegram с «<», «&» и кавычками: приветствие, список и карточка
    показывают его как есть (разметка не ломается)."""
    h = two_admins
    log = await h.send_command(MANAGER_TG_ID, "start", first_name='Анна <b>"&"</b>')
    assert 'Здравствуйте, Анна <b>"&"</b>!' in log.text
    await h.send_command(MGR2, "start", first_name="Олег")
    await open_card(h, MGR2, "Анна")
    assert 'Анна <b>"&"</b>' in h.last_text(MGR2)


async def test_help_in_the_middle_of_questionnaire(app):
    """/help и «❓ Помощь» посреди анкеты показывают справку для новичка и подсказку /start."""
    h = app
    await h.send_command(EMP, "start")
    log = await h.send_command(EMP, "help")
    assert "Как работает бот" in log.text
    assert "/start" in log.text
    assert await h.get_state(EMP) is None
    assert h.reply_keyboard(EMP) is None


# --- Решение по заявке ------------------------------------------------------------------------


async def test_approve_from_notification_and_second_manager_gets_already_processed(two_admins):
    """Первый начальник подтверждает заявку прямо из уведомления: уведомление превращается
    в карточку сотрудника, сотрудник получает «Доступ открыт» и меню сотрудника.
    Второй начальник нажимает «Подтвердить» в своём уведомлении — alert «Заявка уже
    обработана», его уведомление показывает актуальный статус, сотруднику не приходит дубль.
    «Отклонить» после подтверждения тоже ничего не ломает."""
    h = two_admins
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.send_command(MGR2, "start", first_name="Олег")
    await register(h, EMP, "Иванов Иван Иванович")
    emp = await h.get_user(EMP)

    log = await h.press_button(MANAGER_TG_ID, "Подтвердить")
    assert log.alert == "✅ Заявка подтверждена"
    card = h.last_text(MANAGER_TG_ID)
    assert "Заявка подтверждена" in card
    assert "Статус: ✅ Активен" in card
    assert h.buttons(MANAGER_TG_ID) == [
        "📊 Карточка", "👔 Сделать начальником", "⛔ Заблокировать", "◀ К списку сотрудников",
    ]
    assert "Доступ к боту открыт" in log.to(EMP).text
    assert "Ваша роль: сотрудник" in log.to(EMP).text
    assert h.reply_keyboard(EMP) == EMPLOYEE_MENU
    assert (await h.get_user(EMP)).status == UserStatus.ACTIVE

    log = await h.press_button(MGR2, "Подтвердить")
    assert log.alert == "Заявка уже обработана"
    assert log.answers[0].show_alert
    assert not log.to(EMP).texts
    assert "Текущий статус: ✅ Активен" in h.last_text(MGR2)

    log = await h.press(MGR2, UserCB(action="reject", user_id=emp.id))
    assert log.alert == "Заявка уже обработана"
    assert not log.to(EMP).texts
    assert (await h.get_user(EMP)).status == UserStatus.ACTIVE

    # Сотрудник пользуется меню.
    log = await h.press_menu(EMP, BTN_HELP)
    assert BTN_PROPOSE in log.text


async def test_approve_from_staff_list(app):
    """Начальник открывает «👥 Сотрудники»: новая заявка сверху, с пометкой. Открывает
    карточку, подтверждает, возвращается к списку — человек уже среди сотрудников."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await register(h, EMP, "Петров Пётр", "Бухгалтер")
    await h.send_command(EMP2, "start")  # начал анкету, но ФИО не ввёл

    log = await h.send_command(MANAGER_TG_ID, "start")
    assert "Новых заявок на доступ: 1" in log.text

    await h.press_menu(MANAGER_TG_ID, BTN_STAFF)
    text = h.last_text(MANAGER_TG_ID)
    assert "Заявок: 1 · Сотрудников: 0 · Начальников: 1" in text
    assert "Есть новые заявки" in text
    assert text.index("Заявки на доступ") < text.index("Начальники")
    assert "1 чел. начали регистрацию, но пока не указали ФИО" in text
    assert h.buttons(MANAGER_TG_ID) == ["⏳ Петров Пётр", "👔 Анна", "🔄 Обновить"]

    await h.press_button(MANAGER_TG_ID, "Петров")
    assert "Подтвердите заявку" in h.last_text(MANAGER_TG_ID)
    assert h.buttons(MANAGER_TG_ID) == ["✅ Подтвердить", "❌ Отклонить", "◀ К списку сотрудников"]

    log = await h.press_button(MANAGER_TG_ID, "Подтвердить")
    assert "Доступ к боту открыт" in log.to(EMP).text
    assert h.reply_keyboard(EMP) == EMPLOYEE_MENU

    await h.press_button(MANAGER_TG_ID, "К списку сотрудников")
    text = h.last_text(MANAGER_TG_ID)
    assert "Заявок: 0 · Сотрудников: 1" in text
    assert h.buttons(MANAGER_TG_ID)[0] == "👤 Петров Пётр"

    # Уведомление о заявке (выше в чате) больше не подтверждает повторно.
    log = await h.press_button(MANAGER_TG_ID, "Подтвердить")
    assert log.alert == "Заявка уже обработана"


async def test_reject_request_then_unblock_later(app):
    """Начальник отклоняет заявку: сотрудник узнаёт об этом, меню у него нет, на /start,
    /menu и любой текст бот отвечает «Доступ закрыт». Позже начальник передумал и
    разблокировал — сотрудник получает доступ и меню."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await register(h, EMP, "Иванов Иван")

    log = await h.press_button(MANAGER_TG_ID, "Отклонить")
    assert log.alert == "❌ Заявка отклонена"
    assert "Заявка отклонена" in h.last_text(MANAGER_TG_ID)
    assert h.buttons(MANAGER_TG_ID) == ["📊 Карточка", "🔓 Разблокировать", "◀ К списку сотрудников"]
    assert "Заявка на доступ отклонена" in log.to(EMP).text
    assert h.reply_keyboard(EMP) is None
    assert (await h.get_user(EMP)).status == UserStatus.BLOCKED

    for send in (
        h.send_command(EMP, "start"),
        h.send_command(EMP, "menu"),
        h.send_text(EMP, "Почему отклонили?"),
    ):
        log = await send
        assert BLOCKED in log.text
        assert h.reply_keyboard(EMP) is None
    await h.send_sticker(EMP)
    assert BLOCKED in h.last_text(EMP)

    log = await h.press_button(MANAGER_TG_ID, "Разблокировать")
    assert log.alert == "✅ Пользователь разблокирован"
    assert "Доступ к боту открыт" in log.to(EMP).text
    assert h.reply_keyboard(EMP) == EMPLOYEE_MENU
    assert (await h.get_user(EMP)).status == UserStatus.ACTIVE


async def test_pending_user_cannot_do_anything_until_approved(app):
    """Заявка подана, но не рассмотрена. Что бы сотрудник ни писал (текст, стикер, кнопку
    меню, /menu, /staff, /start), бот отвечает «Заявка на рассмотрении» и меню не показывает.
    Подделанная кнопка «Подтвердить» для самого себя — «Недостаточно прав»."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    emp = await register(h, EMP, "Иванов Иван")

    for send in (
        h.send_text(EMP, "Здравствуйте, когда меня подтвердят?"),
        h.send_text(EMP, BTN_MY_TASKS),
        h.send_command(EMP, "menu"),
        h.send_command(EMP, "staff"),
        h.send_command(EMP, "start"),
    ):
        log = await send
        assert PENDING in log.text
        assert h.reply_keyboard(EMP) is None
    await h.send_sticker(EMP)
    assert PENDING in h.last_text(EMP)

    log = await h.send_command(EMP, "help")
    assert "Как работает бот" in log.text
    assert PENDING in log.text  # справка не предлагает «отправить заявку» ещё раз

    log = await h.press(EMP, UserCB(action="approve", user_id=emp.id))
    assert NO_RIGHTS in log.alert
    log = await h.press(EMP, UserCB(action="manage", user_id=emp.id))
    assert NO_RIGHTS in log.alert
    log = await h.press(EMP, UserCB(action="staff", user_id=0))
    assert NO_RIGHTS in log.alert
    assert (await h.get_user(EMP)).status == UserStatus.PENDING


async def test_unknown_user_without_start(app):
    """Человек ни разу не нажимал /start: на текст и на чужую кнопку бот подсказывает /start
    или отказывает — ничего не ломается и в БД ничего не создаётся."""
    h = app
    log = await h.send_text(3999, "Привет")
    assert "Нажмите /start" in log.text
    log = await h.press(3999, UserCB(action="approve", user_id=1))
    assert NO_RIGHTS in log.alert
    log = await h.press(3999, PickCB(field="skip", value="skip"))
    assert log.alert
    assert await count(h, User) == 0


# --- Блокировка, роли ----------------------------------------------------------------------------


async def test_block_and_unblock_employee(app):
    """Начальник блокирует сотрудника из карточки: тому приходит «Доступ закрыт», меню
    исчезает; старые кнопки меню и /start больше не работают. После разблокировки —
    снова меню сотрудника и рабочие кнопки."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.seed_user(EMP, "Иванов Иван Иванович")
    await h.send_command(EMP, "start")
    assert h.reply_keyboard(EMP) == EMPLOYEE_MENU

    await open_card(h, MANAGER_TG_ID, "Иванов")
    log = await h.press_button(MANAGER_TG_ID, "Заблокировать")
    assert log.alert == "🚫 Пользователь заблокирован"
    assert "Пользователь заблокирован" in h.last_text(MANAGER_TG_ID)
    assert "Статус: 🚫 Заблокирован" in h.last_text(MANAGER_TG_ID)
    # Карточка эффективности ушедшего сотрудника остаётся доступной: история оценок не пропадает.
    assert h.buttons(MANAGER_TG_ID) == ["📊 Карточка", "🔓 Разблокировать", "◀ К списку сотрудников"]
    assert "Доступ к боту закрыт начальником" in log.to(EMP).text
    assert h.reply_keyboard(EMP) is None
    assert (await h.get_user(EMP)).status == UserStatus.BLOCKED

    log = await h.send_text(EMP, BTN_MY_TASKS)  # старая кнопка меню на устройстве
    assert BLOCKED in log.text
    log = await h.send_command(EMP, "start")
    assert BLOCKED in log.text
    assert h.reply_keyboard(EMP) is None

    # Повторное нажатие «Заблокировать» (старое сообщение) — понятный отказ.
    log = await h.press(MANAGER_TG_ID, UserCB(action="block", user_id=(await h.get_user(EMP)).id))
    assert "уже заблокирован" in log.alert

    await h.press_button(MANAGER_TG_ID, "К списку сотрудников")
    assert "Заблокировано: 1" in h.last_text(MANAGER_TG_ID)
    await h.press_button(MANAGER_TG_ID, "🚫 Иванов")
    log = await h.press_button(MANAGER_TG_ID, "Разблокировать")
    assert "Доступ к боту открыт" in log.to(EMP).text
    assert h.reply_keyboard(EMP) == EMPLOYEE_MENU
    log = await h.press_menu(EMP, BTN_MY_TASKS)
    assert BLOCKED not in log.text
    assert NO_RIGHTS not in log.text


async def test_employee_blocked_in_the_middle_of_a_dialog_is_stopped(app):
    """Сотрудник начал вносить поручение, и в этот момент начальник его заблокировал.
    Следующее сообщение и кнопка диалога не продолжают диалог, а отвечают «Доступ закрыт»;
    поручение не создаётся."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    emp = await h.seed_user(EMP, "Иванов Иван Иванович")
    await h.send_command(EMP, "start")
    await h.press_menu(EMP, BTN_PROPOSE)
    assert await h.get_state(EMP) is not None

    question = h.last_message(EMP).message_id

    await h.press(MANAGER_TG_ID, UserCB(action="block", user_id=emp.id))
    assert await h.get_state(EMP) is None  # диалог сброшен сразу при блокировке
    log = await h.send_text(EMP, "Анализ договоров поставщиков")
    assert BLOCKED in log.text
    log = await h.press_button(EMP, "Отмена", question)
    assert BLOCKED in log.text
    assert await count(h, Task) == 0


async def test_inactive_user_with_unfinished_dialog_is_stopped_by_start_router(app):
    """Гонка: сотрудника заблокировали, а у него всё ещё открыт диалог (например, апдейт
    пришёл в момент блокировки). Любой ввод и кнопка диалога получают «Доступ закрыт»,
    диалог закрывается, меню убирается; поручение не создаётся."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.seed_user(EMP, "Иванов Иван Иванович")
    await h.send_command(EMP, "start")
    await h.press_menu(EMP, BTN_PROPOSE)
    async with h.db() as session:
        user = await session.scalar(select(User).where(User.tg_id == EMP))
        user.status = UserStatus.BLOCKED
        await session.commit()
    state = await h.get_state(EMP)
    assert state is not None

    log = await h.send_text(EMP, "Анализ договоров поставщиков")
    assert BLOCKED in log.text
    assert h.reply_keyboard(EMP) is None
    assert await h.get_state(EMP) is None
    assert await count(h, Task) == 0

    # То же для кнопки: сотрудник снова «в диалоге» и жмёт кнопку шага.
    context = h.dp.fsm.get_context(h.bot, chat_id=EMP, user_id=EMP)
    await context.set_state(state)
    log = await h.press(EMP, PickCB(field="ai", value="accept"))
    assert BLOCKED in log.alert
    assert await h.get_state(EMP) is None


async def test_promoted_employee_loses_unfinished_employee_dialog_with_explanation(app):
    """Сотрудник вносит поручение, и в этот момент его назначают начальником. Начатый диалог
    сотрудника сбрасывается, в уведомлении о новой роли сказано, что действие отменено, —
    следующий текст не попадает в старый диалог."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    emp = await h.seed_user(EMP, "Иванов Иван Иванович")
    await h.send_command(EMP, "start")
    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, "Анализ договоров поставщиков")

    log = await h.press(MANAGER_TG_ID, UserCB(action="role_mgr", user_id=emp.id))
    assert "назначена роль начальника" in log.to(EMP).text
    assert "Незавершённое действие в боте отменено" in log.to(EMP).text
    assert await h.get_state(EMP) is None
    assert h.reply_keyboard(EMP) == MANAGER_MENU
    log = await h.send_text(EMP, "проверить 100 договоров")
    assert "Не понял" in log.text
    assert await count(h, Task) == 0

    # Без открытого диалога уведомление о смене роли — без лишней фразы.
    log = await h.press(MANAGER_TG_ID, UserCB(action="role_emp", user_id=emp.id))
    assert "Ваша роль изменена" in log.to(EMP).text
    assert "отменено" not in log.to(EMP).text


async def test_demoted_manager_loses_task_creation_dialog(no_admins):
    """Второй начальник ставит задачу, и в этот момент его переводят в сотрудники.
    Его диалог постановки задачи сброшен: следующий текст не становится названием задачи,
    старые кнопки диалога не работают, задача не создаётся."""
    h = no_admins
    await h.seed_user(MANAGER_TG_ID, "Петрова Анна", role="manager")
    boris = await h.seed_user(3001, "Борисов Борис", role="manager")
    await h.seed_user(EMP, "Иванов Иван Иванович")
    await h.send_command(3001, "start")
    await h.press_menu(3001, BTN_NEW_TASK)
    await h.press_button(3001, "Иванов")
    assert await h.get_state(3001) == "CreateTaskSG:title"
    question = h.last_message(3001).message_id

    log = await h.press(MANAGER_TG_ID, UserCB(action="role_emp", user_id=boris.id))
    assert "Незавершённое действие в боте отменено" in log.to(3001).text
    assert h.reply_keyboard(3001) == EMPLOYEE_MENU
    assert await h.get_state(3001) is None

    log = await h.send_text(3001, "Анализ договоров")
    assert "Не понял" in log.text
    log = await h.press_button(3001, "Отмена", question)
    assert "Действие отменено" in log.text
    assert await count(h, Task) == 0


async def test_promote_to_manager_and_demote_back(app):
    """Начальник назначает сотрудника начальником: тот получает уведомление и меню
    начальника и может открыть «👥 Сотрудники». Затем роль возвращают — снова меню
    сотрудника, а старые кнопки управления сотрудниками ему уже недоступны."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.seed_user(EMP, "Иванов Иван Иванович")

    await open_card(h, MANAGER_TG_ID, "Иванов")
    log = await h.press_button(MANAGER_TG_ID, "Сделать начальником")
    assert log.alert == "👔 Назначен начальником"
    assert "Роль: 👔 Начальник" in h.last_text(MANAGER_TG_ID)
    assert h.buttons(MANAGER_TG_ID) == ["👤 Сделать сотрудником", "⛔ Заблокировать", "◀ К списку сотрудников"]
    assert "назначена роль начальника" in log.to(EMP).text
    assert h.reply_keyboard(EMP) == MANAGER_MENU
    assert (await h.get_user(EMP)).role == Role.MANAGER

    await h.press_menu(EMP, BTN_STAFF)
    assert "Начальников: 2" in h.last_text(EMP)
    staff_list = h.last_message(EMP).message_id

    log = await h.press_button(MANAGER_TG_ID, "Сделать сотрудником")
    assert log.alert == "👤 Роль изменена на «Сотрудник»"
    assert "Ваша роль изменена" in log.to(EMP).text
    assert h.reply_keyboard(EMP) == EMPLOYEE_MENU
    assert (await h.get_user(EMP)).role == Role.EMPLOYEE

    # Список сотрудников, открытый, пока он был начальником, больше не работает.
    log = await h.press_button(EMP, "Анна", staff_list)
    assert NO_RIGHTS in log.alert
    log = await h.press_button(EMP, "Обновить", staff_list)
    assert NO_RIGHTS in log.alert
    log = await h.send_command(EMP, "staff")
    assert "Не понял" in log.text


async def test_last_manager_cannot_demote_or_block_himself(no_admins):
    """Начальников двое (оба назначены в боте, ADMIN_IDS пуст). Первый переводит второго в
    сотрудники — теперь он единственный. В своей карточке он видит «Это вы.» без кнопок
    понижения и блокировки, а подделанные callback'и получают понятный отказ. Бывший
    начальник старыми кнопками ничего сделать не может. Начальник в системе остаётся."""
    h = no_admins
    anna = await h.seed_user(MANAGER_TG_ID, "Петрова Анна", role="manager")
    boris = await h.seed_user(3001, "Борисов Борис", role="manager")

    await open_card(h, MANAGER_TG_ID, "Борисов")
    await h.press_button(MANAGER_TG_ID, "Сделать сотрудником")
    assert (await h.get_user(3001)).role == Role.EMPLOYEE

    await h.press_button(MANAGER_TG_ID, "К списку сотрудников")
    await h.press_button(MANAGER_TG_ID, "Петрова")
    assert "Это вы." in h.last_text(MANAGER_TG_ID)
    assert h.buttons(MANAGER_TG_ID) == ["◀ К списку сотрудников"]

    log = await h.press(MANAGER_TG_ID, UserCB(action="role_emp", user_id=anna.id))
    assert "Нельзя понизить самого себя" in log.alert
    log = await h.press(MANAGER_TG_ID, UserCB(action="block", user_id=anna.id))
    assert "Нельзя заблокировать самого себя" in log.alert
    log = await h.press(3001, UserCB(action="role_emp", user_id=anna.id))
    assert NO_RIGHTS in log.alert
    log = await h.press(3001, UserCB(action="role_mgr", user_id=boris.id))
    assert NO_RIGHTS in log.alert

    anna = await h.get_user(MANAGER_TG_ID)
    assert (anna.role, anna.status) == (Role.MANAGER, UserStatus.ACTIVE)
    assert (await h.get_user(3001)).role == Role.EMPLOYEE


async def test_two_managers_demote_each_other_at_the_same_moment(no_admins):
    """Два начальника одновременно нажимают «Сделать сотрудником» друг на друге. Бот
    выполняет действия по очереди: один становится сотрудником, второй получает отказ —
    в системе остаётся ровно один начальник."""
    h = no_admins
    anna = await h.seed_user(MANAGER_TG_ID, "Петрова Анна", role="manager")
    boris = await h.seed_user(3001, "Борисов Борис", role="manager")
    await open_card(h, MANAGER_TG_ID, "Борисов")
    await open_card(h, 3001, "Петрова")

    await asyncio.gather(
        h.press_button(MANAGER_TG_ID, "Сделать сотрудником"),
        h.press_button(3001, "Сделать сотрудником"),
    )
    managers = await h.scalars(
        select(User).where(User.role == Role.MANAGER, User.status == UserStatus.ACTIVE)
    )
    assert len(managers) == 1
    winner = managers[0]
    loser_tg = 3001 if winner.id == anna.id else MANAGER_TG_ID
    assert {winner.id} <= {anna.id, boris.id}
    assert h.alerts_for(winner.tg_id)[-1] == "👤 Роль изменена на «Сотрудник»"
    refusal = h.alerts_for(loser_tg)[-1]
    assert "последний активный начальник" in refusal or NO_RIGHTS in refusal
    assert h.reply_keyboard(loser_tg) == EMPLOYEE_MENU


async def test_manager_demoted_while_his_click_waits_cannot_approve(no_admins):
    """Борис нажимает «Подтвердить» заявку ровно в тот момент, когда его самого переводят в
    сотрудники (его нажатие ждёт своей очереди). Когда очередь доходит, у него уже нет прав:
    «Недостаточно прав», заявка остаётся нерассмотренной, сотрудник ничего не получает."""
    from bot.handlers import users_admin

    h = no_admins
    await h.seed_user(MANAGER_TG_ID, "Петрова Анна", role="manager")
    await h.seed_user(3001, "Борисов Борис", role="manager")
    emp = await register(h, EMP, "Иванов Иван")

    lock = users_admin._action_lock()
    async with lock:
        click = asyncio.create_task(h.press(3001, UserCB(action="approve", user_id=emp.id)))
        for _ in range(50):  # дать нажатию дойти до очереди
            await asyncio.sleep(0)
        assert not click.done()
        async with h.db() as session:
            boris = await session.scalar(select(User).where(User.tg_id == 3001))
            boris.role = Role.EMPLOYEE
            await session.commit()
    log = await click

    assert NO_RIGHTS in log.alert
    assert not log.to(EMP).texts
    assert (await h.get_user(EMP)).status == UserStatus.PENDING


async def test_concurrent_approve_and_reject_of_one_request(two_admins):
    """Два начальника одновременно: один нажимает «Подтвердить», другой «Отклонить».
    Срабатывает только первое решение, второй получает «Заявка уже обработана»,
    сотрудник получает ровно одно уведомление."""
    h = two_admins
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.send_command(MGR2, "start", first_name="Олег")
    await register(h, EMP, "Иванов Иван")

    await asyncio.gather(
        h.press_button(MANAGER_TG_ID, "Подтвердить"),
        h.press_button(MGR2, "Отклонить"),
    )
    alerts = h.alerts_for(MANAGER_TG_ID) + h.alerts_for(MGR2)
    assert alerts.count("Заявка уже обработана") == 1
    assert len(alerts) == 2
    decisions = [text for text in h.sent_to(EMP) if "Доступ к боту открыт" in text or "отклонена" in text]
    assert len(decisions) == 1
    status = (await h.get_user(EMP)).status
    assert status in (UserStatus.ACTIVE, UserStatus.BLOCKED)


# --- /help, /menu, /cancel, кнопка меню посреди диалога ---------------------------------------


async def test_help_menu_cancel_for_active_users(app):
    """Начальник и сотрудник: /help и «❓ Помощь» — справка по своей роли, /menu — главное
    меню, /cancel без диалога — просто «Действие отменено» с меню."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.seed_user(EMP, "Иванов Иван Иванович")

    log = await h.send_command(MANAGER_TG_ID, "help")
    assert "Как работает бот" in log.text
    assert BTN_NEW_TASK in log.text and BTN_STAFF in log.text
    log = await h.press_menu(MANAGER_TG_ID, BTN_HELP)
    assert BTN_STAFF in log.text

    log = await h.send_command(EMP, "help")
    assert BTN_PROPOSE in log.text
    assert BTN_STAFF not in log.text
    assert h.reply_keyboard(EMP) == EMPLOYEE_MENU

    log = await h.send_command(EMP, "menu")
    assert "Главное меню" in log.text
    assert h.reply_keyboard(EMP) == EMPLOYEE_MENU
    log = await h.send_command(MANAGER_TG_ID, "menu")
    assert h.reply_keyboard(MANAGER_TG_ID) == MANAGER_MENU

    log = await h.send_command(EMP, "cancel")
    assert "Действие отменено" in log.text
    assert h.reply_keyboard(EMP) == EMPLOYEE_MENU

    log = await h.send_text(EMP, "абракадабра")
    assert "Не понял" in log.text


async def test_cancel_in_the_middle_of_task_creation(app):
    """Начальник начал ставить задачу и передумал: /cancel и кнопка «✖️ Отмена» сбрасывают
    диалог, следующий текст уже не считается названием задачи."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.seed_user(EMP, "Иванов Иван Иванович")

    await h.press_menu(MANAGER_TG_ID, BTN_NEW_TASK)
    await h.press_button(MANAGER_TG_ID, "Иванов")
    assert await h.get_state(MANAGER_TG_ID) == "CreateTaskSG:title"
    log = await h.send_command(MANAGER_TG_ID, "cancel")
    assert "Действие отменено" in log.text
    assert await h.get_state(MANAGER_TG_ID) is None
    assert h.reply_keyboard(MANAGER_TG_ID) == MANAGER_MENU
    log = await h.send_text(MANAGER_TG_ID, "Анализ договоров")
    assert "Не понял" in log.text

    await h.press_menu(MANAGER_TG_ID, BTN_NEW_TASK)
    question = h.last_message(MANAGER_TG_ID).message_id
    log = await h.press(MANAGER_TG_ID, PickCB(field="cancel"))
    assert log.answers
    assert "Действие отменено" in log.text
    assert h.buttons(MANAGER_TG_ID, question) == []
    assert await h.get_state(MANAGER_TG_ID) is None

    # /menu посреди диалога — тоже выход в главное меню.
    await h.press_menu(MANAGER_TG_ID, BTN_NEW_TASK)
    await h.press_button(MANAGER_TG_ID, "Иванов")
    log = await h.send_command(MANAGER_TG_ID, "menu")
    assert "Главное меню" in log.text
    assert await h.get_state(MANAGER_TG_ID) is None
    assert await count(h, Task) == 0


async def test_menu_button_in_the_middle_of_dialog_resets_it(app):
    """Начальник посреди постановки задачи нажимает «👥 Сотрудники» — открывается список,
    диалог сброшен. Затем «❓ Помощь» посреди нового диалога — справка, диалог снова сброшен.
    Сотрудник посреди внесения поручения нажимает «❓ Помощь» — то же самое."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.seed_user(EMP, "Иванов Иван Иванович")

    await h.press_menu(MANAGER_TG_ID, BTN_NEW_TASK)
    await h.press_button(MANAGER_TG_ID, "Иванов")
    assert await h.get_state(MANAGER_TG_ID) == "CreateTaskSG:title"
    log = await h.press_menu(MANAGER_TG_ID, BTN_STAFF)
    assert "Сотрудников: 1" in log.text
    assert await h.get_state(MANAGER_TG_ID) is None
    log = await h.send_text(MANAGER_TG_ID, "Анализ договоров")
    assert "Не понял" in log.text

    await h.press_menu(MANAGER_TG_ID, BTN_NEW_TASK)
    await h.press_button(MANAGER_TG_ID, "Иванов")
    log = await h.press_menu(MANAGER_TG_ID, BTN_HELP)
    assert "Как работает бот" in log.text
    assert await h.get_state(MANAGER_TG_ID) is None

    await h.send_command(EMP, "start")
    await h.press_menu(EMP, BTN_PROPOSE)
    assert await h.get_state(EMP) is not None
    log = await h.press_menu(EMP, BTN_HELP)
    assert "Как работает бот" in log.text
    assert await h.get_state(EMP) is None
    log = await h.send_text(EMP, "Подготовить отчёт")
    assert "Не понял" in log.text
    assert await count(h, Task) == 0


async def test_start_in_the_middle_of_dialog_shows_welcome(app):
    """Начальник посреди постановки задачи нажимает /start: приветствие с меню,
    диалог сброшен, у вопроса «кому поставить задачу» кнопки убраны, а если старый клиент
    ещё показывает кнопку сотрудника — она больше не продолжает диалог."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await h.seed_user(EMP, "Иванов Иван Иванович")
    await h.press_menu(MANAGER_TG_ID, BTN_NEW_TASK)
    choose = h.last_message(MANAGER_TG_ID).message_id
    ivanov = h.find_button(MANAGER_TG_ID, "Иванов", choose)

    log = await h.send_command(MANAGER_TG_ID, "start")
    assert "Вы вошли как начальник" in log.text
    assert await h.get_state(MANAGER_TG_ID) is None
    assert h.buttons(MANAGER_TG_ID, choose) == []
    log = await h.press(MANAGER_TG_ID, ivanov, choose)
    assert log.answers
    assert await h.get_state(MANAGER_TG_ID) is None


# --- Список сотрудников: листание -------------------------------------------------------------


async def test_staff_list_pagination_with_25_employees(app):
    """В системе 25 сотрудников и начальник. Список «👥 Сотрудники» листается по 20:
    на второй странице нумерация продолжается, кнопка «◀ К списку» из карточки и действие
    в карточке возвращают на ту же страницу."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    for i in range(1, 26):
        await h.seed_user(5000 + i, f"Сотрудник{i:02d} Тест", position="Специалист")

    await h.press_menu(MANAGER_TG_ID, BTN_STAFF)
    text = h.last_text(MANAGER_TG_ID)
    assert "Сотрудников: 25 · Начальников: 1" in text
    assert "Страница 1 из 2" in text
    buttons = h.buttons(MANAGER_TG_ID)
    assert len(buttons) == 22  # 20 человек + «🔄 1/2» и «▶»
    assert buttons[0] == "👤 Сотрудник01 Тест"
    assert buttons[19] == "👤 Сотрудник20 Тест"
    assert buttons[20:] == ["🔄 1/2", "▶"]
    assert "20. 🟢 Сотрудник20 Тест" in text
    assert "Сотрудник21" not in text

    await h.press_button(MANAGER_TG_ID, "▶")
    text = h.last_text(MANAGER_TG_ID)
    assert "Страница 2 из 2" in text
    assert "21. 🟢 Сотрудник21 Тест" in text
    assert "26. 🟢 Анна" in text
    assert h.buttons(MANAGER_TG_ID) == [
        "👤 Сотрудник21 Тест", "👤 Сотрудник22 Тест", "👤 Сотрудник23 Тест", "👤 Сотрудник24 Тест",
        "👤 Сотрудник25 Тест", "👔 Анна", "◀", "🔄 2/2",
    ]

    await h.press_button(MANAGER_TG_ID, "Сотрудник23")
    assert "Сотрудник23 Тест" in h.last_text(MANAGER_TG_ID)
    await h.press_button(MANAGER_TG_ID, "К списку сотрудников")
    assert "Страница 2 из 2" in h.last_text(MANAGER_TG_ID)

    await h.press_button(MANAGER_TG_ID, "Сотрудник24")
    await h.press_button(MANAGER_TG_ID, "Заблокировать")
    await h.press_button(MANAGER_TG_ID, "К списку сотрудников")
    text = h.last_text(MANAGER_TG_ID)
    assert "Заблокировано: 1" in text
    assert "Страница 2 из 2" in text
    assert "🚫 Сотрудник24 Тест" in h.buttons(MANAGER_TG_ID)

    await h.press_button(MANAGER_TG_ID, "◀")
    assert "Страница 1 из 2" in h.last_text(MANAGER_TG_ID)

    # «Обновить» на той же странице ничего не ломает (сообщение не изменилось).
    log = await h.press_button(MANAGER_TG_ID, "🔄 1/2")
    assert log.answers


async def test_staff_list_with_long_names_fits_telegram_limits(app):
    """20 сотрудников с очень длинными ФИО и должностями: список всё равно отправляется
    (не больше 4096 символов, разметка цела), на каждой кнопке — обрезанное имя."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    for i in range(1, 21):
        await h.seed_user(
            6000 + i,
            f"Длинноименный{i:02d} " + "Оченьдлинноеимя " * 11,
            position="Должность " * 19,
            username=f"user_name_{i:02d}_long",
        )
    log = await h.press_menu(MANAGER_TG_ID, BTN_STAFF)
    assert log.texts
    text = h.last_text(MANAGER_TG_ID)
    assert len(text) <= 4096
    # Видны все 20 человек страницы, номер страницы и подсказка — ничего не обрезано.
    assert "20. 👤 Длинноименный20" in text
    assert "Страница 1 из 2" in text
    assert "Нажмите на человека" in text
    person_buttons = [b for b in h.buttons(MANAGER_TG_ID) if b.startswith("👤")]
    assert len(person_buttons) == 20 and all(len(b) <= 52 for b in person_buttons)

    # Карточка человека с длинными данными тоже открывается.
    await h.press_button(MANAGER_TG_ID, "Длинноименный07")
    assert "Длинноименный07" in h.last_text(MANAGER_TG_ID)


async def test_manager_opens_card_of_employee_and_old_task_button_of_blocked_user(app):
    """Заблокированный сотрудник нажимает кнопку из старого уведомления о задаче — бот не
    открывает задачу, а отвечает отказом."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    emp = await h.seed_user(EMP, "Иванов Иван Иванович")
    await h.press(MANAGER_TG_ID, UserCB(action="block", user_id=emp.id))
    log = await h.press(EMP, TaskCB(action="open", task_id=1))
    assert log.alert
    assert not log.to(EMP).texts


# --- Анкета: регистр ФИО, справка для заблокированного -----------------------------------------


@pytest.mark.parametrize(
    ("typed", "saved"),
    [
        ("иванов иван иванович", "Иванов Иван Иванович"),
        ("ПЕТРОВ-ВОДКИН КУЗЬМА", "Петров-Водкин Кузьма"),
        ("сидоров с. с.", "Сидоров С. С."),
        ("ван Дейк Анна", "ван Дейк Анна"),  # смешанный регистр — как ввели
    ],
)
async def test_full_name_typed_in_one_case_is_capitalized(app, typed, saved):
    """Сотрудник набрал ФИО строчными (или капслоком): в заявке, списках и Excel оно будет
    «Иванов Иван Иванович», а не «иванов иван иванович». Смешанный регистр не трогаем."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await register(h, EMP, typed)
    assert (await h.get_user(EMP)).full_name == saved
    assert f"ФИО: {saved}" in h.last_text(MANAGER_TG_ID)


async def test_help_for_blocked_user_does_not_offer_to_apply_again(app):
    """Заблокированный пользователь открывает /help: справка объясняет, как устроен бот,
    и говорит, что доступ закрыт, — а не предлагает «нажать /start и отправить заявку»."""
    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
    await register(h, EMP, "Иванов Иван")
    await h.press_button(MANAGER_TG_ID, "Отклонить")

    log = await h.send_command(EMP, "help")
    assert "Как работает бот" in log.text
    assert BLOCKED in log.text
    assert "отправьте заявку" not in log.text
    assert h.reply_keyboard(EMP) is None


# --- Админ бота называется админом -------------------------------------------------------------------


async def test_marked_admin_is_called_admin_everywhere(app: BotHarness) -> None:
    """Человек с отметкой админа (python -m bot.tools.admin grant …) в приветствии, в списке сотрудников и
    в своей карточке — «админ», а не «начальник». Начальник из ADMIN_IDS остаётся начальником."""
    from bot.services import users as users_svc

    h = app
    await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")  # начальник из ADMIN_IDS
    assert "Вы вошли как начальник" in h.last_text(MANAGER_TG_ID)

    await h.seed_user(ADMIN_TG, "Исроилов Иброхим")
    async with h.db() as s:
        await users_svc.grant_admin(s, ADMIN_TG)
        await s.commit()
    await h.send_command(ADMIN_TG, "start", first_name="Иброхим")
    greeting = h.last_text(ADMIN_TG)
    assert "Вы вошли как админ бота" in greeting and "начальник" not in greeting.split("\n")[1]
    assert h.reply_keyboard(ADMIN_TG) == MANAGER_MENU  # права и меню — как у начальника

    await h.press_menu(ADMIN_TG, BTN_STAFF)
    text = h.last_text(ADMIN_TG)
    assert "Начальников: 1 · Админов: 1 · Заблокировано: 0" in text
    assert "👔 Начальники" in text and "🛡 Админы" in text
    assert text.index("👔 Начальники") < text.index("🛡 Админы")
    admin_line = next(line for line in text.split("\n") if "Исроилов Иброхим" in line)
    assert "🛡 админ" in admin_line and "начальник" not in admin_line
    assert "🛡 Исроилов Иброхим" in h.buttons(ADMIN_TG)
    assert any(label.startswith("👔 ") for label in h.buttons(ADMIN_TG))

    await h.press_button(ADMIN_TG, "Исроилов Иброхим")
    card = h.last_text(ADMIN_TG)
    assert "Роль: 🛡 Админ" in card and "Роль: 👔 Начальник" not in card

    # Начальник смотрит карточку админа: понизить и заблокировать нельзя.
    await h.press_menu(MANAGER_TG_ID, BTN_STAFF)
    await h.press_button(MANAGER_TG_ID, "Исроилов Иброхим")
    card = h.last_text(MANAGER_TG_ID)
    assert "Роль: 🛡 Админ" in card and "админ бота" in card
    assert not any("Сотрудник" in label or "Заблокировать" in label for label in h.buttons(MANAGER_TG_ID))
