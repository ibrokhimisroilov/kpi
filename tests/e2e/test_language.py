"""Язык интерфейса в чате (SPEC.md §14): выбор при первом запуске, смена кнопкой «🌐 Til / Язык» и /lang,
узбекские кнопки меню, уведомления на языке получателя. Сквозной сценарий «поставить → сдать → подтвердить»
проходит целиком на узбекском: в чатах не остаётся ни одной русской буквы, кроме слов самих людей.

Метки слов пользователя здесь включены (как в работе): без них перевод не отличит текст бота от названия
задачи. Русские подписи кнопок в сценарии находятся по обратному словарю каталога.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import select

from bot import i18n
from bot.db.models import Task, TaskStatus, User
from bot.ui.callbacks import TaskCB
from bot.i18n import catalog_uz
from bot.ui.texts import (
    BTN_EXPORT,
    BTN_HELP,
    BTN_LANG,
    BTN_MY_KPI,
    BTN_MY_TASKS,
    BTN_NEW_TASK,
    BTN_PROPOSALS,
    BTN_REVIEW,
    BTN_STAFF,
    BTN_SUBMIT,
    BTN_TASKS,
    BTN_TEAM,
    EMPLOYEE_MENU_LAYOUT,
    MANAGER_MENU_LAYOUT,
)

from .fakebot import MANAGER_TG_ID, BotHarness

pytestmark = pytest.mark.asyncio

MGR = MANAGER_TG_ID
EMP = 2001
NEW = 3001

LANG_BUTTONS = ["Русский", "Oʻzbekcha"]
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
# Слова людей в сценарии: ФИО, должность, название задачи, результат, факт, комментарий.
USER_WORDS = (
    "Петрова Анна Сергеевна",
    "Иванов Иван Иванович",
    "Иванов И. И.",
    "Петрова А. С.",
    "Иванов",
    "Юрист",
    "Анализ договоров",
    "Проверить 100 договоров и представить отчёт",
    "договоров",
    "Проверено 95 договоров, отчёт приложен",
    "Хорошая работа",
    BTN_LANG,  # надпись кнопки языка и вопрос «Выберите язык» — двуязычные на любом языке
    "Выберите язык",
    "Русский",  # название языка на кнопке выбора
    "(язык)",  # справка: «/lang — til (язык)»
)
REVERSE = {uz: ru for ru, uz in catalog_uz.LINES.items()}


@pytest.fixture(autouse=True)
def _marks_on(set_env: Callable[..., None]) -> Iterator[None]:
    """Метки включены; после теста в каталоге не должно остаться ни одной непереведённой строки бота."""
    set_env(I18N_MARKS="true", QUIET_HOURS_START="0", QUIET_HOURS_END="0")
    i18n.clear_misses()
    yield
    missing = i18n.misses()
    i18n.clear_misses()
    assert not missing, f"нет перевода: {sorted(missing)}"


def uz(text: str) -> str:
    return i18n.tr(text, i18n.UZ)


def cyrillic_left(text: str | None) -> str:
    """Русские буквы, оставшиеся в тексте после вычёркивания слов людей (пусто — всё переведено)."""
    rest = text or ""
    for words in sorted(USER_WORDS, key=len, reverse=True):
        rest = rest.replace(words, "")
    return "".join(CYRILLIC.findall(rest))


def assert_uzbek(h: BotHarness, chat_id: int) -> None:
    """Все сообщения бота в чате и их кнопки — на узбекском (русские только слова людей)."""
    for msg in h.messages(chat_id):
        if not msg.from_bot:
            continue
        assert not cyrillic_left(msg.content), f"#{msg.message_id}: {msg.content!r}"
        for label in msg.button_texts:
            assert not cyrillic_left(label), f"кнопка #{msg.message_id}: {label!r}"
    for label in h.reply_keyboard(chat_id) or []:
        assert not cyrillic_left(label), f"меню: {label!r}"


async def press_ru(h: BotHarness, user_id: int, russian: str) -> None:
    """Нажать кнопку, которая по-русски называлась бы ``russian`` (подстрока), в каком бы виде она ни пришла."""
    wanted = russian.lower()
    for msg in h.api.recent_messages(user_id):
        for button in msg.buttons:
            names = (button.text, REVERSE.get(button.text, ""))
            if any(wanted in name.lower() for name in names) and button.callback_data:
                await h.press(user_id, button.callback_data, msg.message_id)
                return
    shown = [msg.button_texts for msg in h.api.recent_messages(user_id) if msg.buttons][:4]
    raise LookupError(f"Нет кнопки «{russian}»; кнопки в чате: {shown}")


async def set_lang(h: BotHarness, tg_id: int, lang: str | None) -> None:
    async with h.db() as s:
        user = (await s.execute(select(User).where(User.tg_id == tg_id))).scalar_one()
        user.lang = lang
        await s.commit()


async def lang_in_db(h: BotHarness, tg_id: int) -> str | None:
    async with h.db() as s:
        return (await s.execute(select(User.lang).where(User.tg_id == tg_id))).scalar_one()


def telegram_language(h: BotHarness, tg_id: int, code: str) -> None:
    h.api.profiles[tg_id] = h.profile(tg_id).model_copy(update={"language_code": code})


# --- Первый запуск ------------------------------------------------------------------------------------


async def test_newcomer_picks_uzbek_and_registers_in_uzbek(app: BotHarness) -> None:
    """Новичок с русским Telegram: первым сообщением бот предлагает язык, анкета идёт своим чередом.
    Нажал «Oʻzbekcha» — тот же вопрос анкеты стал узбекским, дальше всё по-узбекски; заявка начальнику — по-русски."""
    h = app
    await h.send_command(MGR, "start", first_name="Анна")
    await h.send_command(NEW, "start", first_name="Иван")
    picker, question = h.messages(NEW)[-2:]
    assert picker.button_texts == LANG_BUTTONS
    assert "Tilni tanlang" in picker.content and "Выберите язык" in picker.content
    assert "Шаг 1 из 2" in question.content
    assert await h.get_state(NEW) == "RegistrationSG:full_name"

    await h.press_button(NEW, "Oʻzbekcha")
    assert await lang_in_db(h, NEW) == "uz"
    assert await h.get_state(NEW) == "RegistrationSG:full_name"  # анкета не сбилась
    bot_messages = [m for m in h.messages(NEW) if m.from_bot]
    assert len(bot_messages) == 2  # ничего лишнего: выбор языка и тот же вопрос, оба отредактированы
    assert bot_messages[0].content == "✅ Til: Oʻzbekcha."
    assert bot_messages[0].button_texts == []
    assert not cyrillic_left(bot_messages[1].content) and "1" in bot_messages[1].content
    assert bot_messages[1].button_texts == [uz("✖️ Отмена")]

    await h.send_text(NEW, "Иванов")  # ошибка в ФИО объясняется по-узбекски
    assert not cyrillic_left(h.last_text(NEW))
    await h.send_text(NEW, "Иванов Иван Иванович")
    assert await h.get_state(NEW) == "RegistrationSG:position"
    await h.send_text(NEW, "Юрист")
    assert_uzbek(h, NEW)

    request = h.last_text(MGR) or ""  # начальник с русским языком читает заявку по-русски
    assert "Иванов Иван Иванович" in request and "Юрист" in request
    assert CYRILLIC.search(request.replace("Иванов Иван Иванович", "").replace("Юрист", ""))
    await h.press_button(MGR, "Подтвердить")
    assert h.reply_keyboard(NEW) == [uz(text) for row in EMPLOYEE_MENU_LAYOUT for text in row]
    assert_uzbek(h, NEW)


async def test_telegram_in_uzbek_means_uzbek_from_the_first_message(app: BotHarness) -> None:
    """Telegram у человека на узбекском — бот с первого сообщения говорит по-узбекски, язык запоминается."""
    h = app
    telegram_language(h, NEW, "uz")
    await h.send_command(NEW, "start", first_name="Иван")
    picker, question = h.messages(NEW)[-2:]
    assert picker.button_texts == LANG_BUTTONS
    assert not cyrillic_left(question.content)
    await h.send_text(NEW, "Иванов Иван Иванович")
    assert await lang_in_db(h, NEW) == "uz"
    assert_uzbek(h, NEW)

    await h.press_button(NEW, "Русский")  # и обратно: выбор человека важнее языка Telegram
    assert await lang_in_db(h, NEW) == "ru"
    await h.send_text(NEW, "Юрист")
    assert "Анкета сохранена" in (h.last_text(NEW) or "")


# --- Смена языка в меню -------------------------------------------------------------------------------


async def test_language_button_switches_menu_and_back(app: BotHarness) -> None:
    h = app
    await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    await h.send_command(MGR, "start")
    assert h.reply_keyboard(MGR) == [text for row in MANAGER_MENU_LAYOUT for text in row]

    await h.press_menu(MGR, BTN_LANG)
    assert h.buttons(MGR) == LANG_BUTTONS
    await h.press_button(MGR, "Oʻzbekcha")
    assert await lang_in_db(h, MGR) == "uz"
    menu = h.reply_keyboard(MGR) or []
    assert menu == [uz(text) for row in MANAGER_MENU_LAYOUT for text in row]
    assert BTN_LANG in menu and not any(cyrillic_left(label) for label in menu)

    # Узбекские кнопки меню открывают те же экраны.
    for button in (BTN_TEAM, BTN_REVIEW, BTN_TASKS, BTN_HELP):
        await h.press_menu(MGR, uz(button))
        assert not cyrillic_left(h.last_text(MGR)), button
    # Старая русская клавиатура (осталась на экране у тех, кто не обновил меню) тоже работает.
    await h.send_text(MGR, BTN_TEAM)
    assert not cyrillic_left(h.last_text(MGR))

    await h.send_command(MGR, "lang")
    await h.press_button(MGR, "Русский")
    assert await lang_in_db(h, MGR) == "ru"
    assert h.reply_keyboard(MGR) == [text for row in MANAGER_MENU_LAYOUT for text in row]
    assert "Главное меню" in (h.last_text(MGR) or "")


async def test_language_choice_does_not_break_a_dialog(app: BotHarness) -> None:
    """Кнопка языка посреди диалога: диалог остаётся на том же шаге и продолжается на новом языке."""
    h = app
    await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    await h.send_command(MGR, "start")
    await h.press_menu(MGR, BTN_NEW_TASK)
    await h.press_button(MGR, "Иванов")
    state = await h.get_state(MGR)
    assert state is not None

    await h.press_menu(MGR, BTN_LANG)
    await h.press_button(MGR, "Oʻzbekcha")
    assert await h.get_state(MGR) == state
    await h.send_text(MGR, "Анализ договоров")
    assert await h.get_state(MGR) != state  # название принято, мастер пошёл дальше
    assert not cyrillic_left(h.last_text(MGR))


# --- Сквозной сценарий на узбекском -------------------------------------------------------------------


async def test_whole_cycle_in_uzbek(app: BotHarness) -> None:
    """Начальник и сотрудник с узбекским: задача → принята → сдана → оценена → KPI. Русских слов бота нет."""
    h = app
    await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    await set_lang(h, MGR, "uz")
    await set_lang(h, EMP, "uz")
    await h.send_command(MGR, "start")
    await h.send_command(EMP, "start")
    assert h.reply_keyboard(EMP) == [uz(text) for row in EMPLOYEE_MENU_LAYOUT for text in row]

    await h.press_menu(MGR, uz(BTN_NEW_TASK))
    await h.press_button(MGR, "Иванов")
    await h.send_text(MGR, "Анализ договоров")
    await h.send_text(MGR, "Проверить 100 договоров и представить отчёт")
    await press_ru(h, MGR, "Принять")
    if await h.get_state(MGR) == "CreateTaskSG:plan":
        await press_ru(h, MGR, "Пропустить")
    await h.press_button(MGR, "Ertaga")  # «Завтра, 11.10»
    await press_ru(h, MGR, "Средний")
    await h.press_button(MGR, "20 %")
    await press_ru(h, MGR, "Создать")
    assert_uzbek(h, MGR)

    task_id = (await h.scalars(select(Task)))[0].id
    await press_ru(h, EMP, "Принял")
    await h.press_menu(EMP, uz(BTN_MY_TASKS))
    await h.press_button(EMP, "Анализ договоров")  # карточка задачи
    assert_uzbek(h, EMP)

    # Сдача результата: факт, итог, число, без файлов, отправка.
    await h.press_menu(EMP, uz(BTN_SUBMIT))
    await h.press_button(EMP, "Анализ договоров")
    await h.send_text(EMP, "Проверено 95 договоров, отчёт приложен")
    await press_ru(h, EMP, "Пропустить")
    if await h.get_state(EMP) == "SubmitSG:value":
        await h.send_text(EMP, "95")
    await press_ru(h, EMP, "Без файлов")
    await press_ru(h, EMP, "Отправить")
    assert_uzbek(h, EMP)
    assert (await h.scalars(select(Task)))[0].status == TaskStatus.SUBMITTED

    # Начальник: очередь проверки, своя оценка с комментарием.
    await h.press_menu(MGR, uz(BTN_REVIEW))
    assert_uzbek(h, MGR)
    await press_ru(h, MGR, "Изменить оценку")
    await h.send_text(MGR, "90")
    await h.send_text(MGR, "Хорошая работа")
    assert (await h.scalars(select(Task)))[0].status == TaskStatus.DONE
    assert_uzbek(h, MGR)
    assert_uzbek(h, EMP)
    assert "90" in (h.last_text(EMP) or "")

    # KPI, список задач, журнал, помощь, сотрудники, экспорт — тоже по-узбекски.
    await h.press_menu(EMP, uz(BTN_MY_KPI))
    await h.press_menu(EMP, uz(BTN_HELP))
    for button in (BTN_TEAM, BTN_TASKS, BTN_HELP, BTN_STAFF, BTN_PROPOSALS, BTN_EXPORT):
        await h.press_menu(MGR, uz(button))
    await h.press(MGR, TaskCB(action="open", task_id=task_id))
    await h.press(MGR, TaskCB(action="history", task_id=task_id))
    assert_uzbek(h, MGR)
    assert_uzbek(h, EMP)


async def test_newcomer_keeps_russian_without_extra_messages(app: BotHarness) -> None:
    """Новичок нажал «Русский» — язык записан, вопрос анкеты не дублируется, анкета идёт дальше."""
    h = app
    await h.send_command(NEW, "start", first_name="Иван")
    await h.press_button(NEW, "Русский")
    assert await lang_in_db(h, NEW) == "ru"
    bot_messages = [m for m in h.messages(NEW) if m.from_bot]
    assert [m.content.split("\n")[0] for m in bot_messages][0] == "✅ Язык: Русский."
    assert len(bot_messages) == 2 and "Шаг 1 из 2" in bot_messages[1].content
    assert await h.get_state(NEW) == "RegistrationSG:full_name"
    await h.send_text(NEW, "Иванов Иван Иванович")
    assert await h.get_state(NEW) == "RegistrationSG:position"
