"""/start, регистрация сотрудника, главное меню, помощь, отмена и «ловушка» необработанных событий.

Модуль экспортирует два роутера:
* ``router`` — подключается ПЕРВЫМ (команды /start, /cancel, /help, /menu, глобальная отмена
  ``PickCB(field="cancel")`` и FSM регистрации);
* ``fallback_router`` — подключается ПОСЛЕДНИМ: отвечает на всё, что не обработали другие модули.
"""

from __future__ import annotations

import logging
import unicodedata

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notify
from bot.config import get_settings
from bot.db.models import Role, User, UserStatus
from bot.filters import TextInput
from bot.handlers import common
from bot.services import users
from bot.ui import keyboards, render
from bot.ui.callbacks import PickCB
from bot.ui.texts import (
    BTN_EXPORT,
    BTN_HELP,
    BTN_MY_KPI,
    BTN_MY_TASKS,
    BTN_NEW_TASK,
    BTN_PROPOSALS,
    BTN_PROPOSE,
    BTN_REVIEW,
    BTN_STAFF,
    BTN_SUBMIT,
    BTN_TASKS,
    BTN_TEAM,
)
from bot.utils.text import esc

log = logging.getLogger(__name__)

router = Router(name="start")
fallback_router = Router(name="fallback")


class RegistrationSG(StatesGroup):
    full_name = State()
    position = State()


# --- Тексты ------------------------------------------------------------------------------------

NAME_MIN_LEN = 2
NAME_MAX_LEN = 200
POSITION_MIN_LEN = 2
POSITION_MAX_LEN = 200
# Кроме букв в ФИО допускаются пробел, дефис, точка и апостроф (узбекские/казахские имена: Gʻulomov, Toʻxtayev).
_NAME_EXTRA_CHARS = frozenset(" -.'’ʼʻ`‘")

TXT_START_HINT = "Нажмите /start, чтобы начать."
# После «Действие отменено.» — что стало с тем, что отменили (по группе состояний FSM диалога).
_CANCEL_HINTS = {
    "CreateTaskSG": "Задача не создана.",
    "ProposeTaskSG": "Поручение не отправлено.",
    "DecideProposalSG": f"Предложение по-прежнему ждёт решения — «{BTN_PROPOSALS}».",
    "SubmitSG": f"Результат не отправлен — сдать его можно в любой момент: «{BTN_SUBMIT}».",
    "ReviewSG": f"Результат по-прежнему ждёт проверки — «{BTN_REVIEW}».",
    "EditTaskSG": "Задача не изменена.",
    "CancelTaskSG": "Задача не отменена.",
}
# Где модули хранят id сообщения с текущим вопросом диалога (его кнопки убираем при /cancel):
# анкета (start), task_create / task_submit / task_review, task_view, task_propose.
_PROMPT_KEYS = ("q_msg_id", "prompt_id", "tv_prompt_id", "msg_id")
TXT_PENDING = (
    "⏳ Заявка на рассмотрении у начальника.\n"
    "Как только вас подтвердят, придёт уведомление."
)
TXT_BLOCKED = "⛔ Доступ закрыт. Обратитесь к начальнику."
TXT_CANCELLED = "Действие отменено."
TXT_NOT_UNDERSTOOD = "Не понял. Воспользуйтесь меню 👇"
TXT_ANSWER_ABOVE = "Пожалуйста, ответьте на вопрос выше или нажмите /cancel."
TXT_STALE_BUTTON = "Кнопка устарела или недоступна."
# Сообщение с кнопкой «📱 Открыть приложение» после приветствия (Mini App, только режим webhook).
TXT_APP_MANAGER = "📱 Команда, задачи, проверка результатов и KPI — в приложении."
TXT_APP_EMPLOYEE = "📱 Ваши задачи, сдача результата и KPI — в приложении."

TXT_ASK_NAME = (
    "👋 Добро пожаловать! Это бот для постановки задач и оценки эффективности.\n\n"
    "Чтобы получить доступ, ответьте на 2 вопроса — начальник получит заявку и подтвердит её.\n\n"
    "<b>Шаг 1 из 2.</b> Введите ваши <b>фамилию, имя и отчество</b>.\n"
    "Например: <i>Иванов Иван Иванович</i>"
)
TXT_NAME_EXAMPLE = "Например: <i>Иванов Иван Иванович</i>"
TXT_ASK_POSITION = (
    "<b>Шаг 2 из 2.</b> Укажите вашу <b>должность</b>.\n"
    "Например: <i>ведущий специалист</i>\n\n"
    "Если не хотите указывать — нажмите «⏭ Пропустить»."
)
TXT_NAME_NOT_TEXT = "Пожалуйста, отправьте ФИО обычным текстом.\n" + TXT_NAME_EXAMPLE + "\n\nОтменить — /cancel."
TXT_POSITION_NOT_TEXT = (
    "Пожалуйста, отправьте должность обычным текстом или нажмите «⏭ Пропустить».\n\nОтменить — /cancel."
)


# --- Общие хелперы ----------------------------------------------------------------------------


def _needs_registration(user: User | None) -> bool:
    """Пользователь ещё не заполнил анкету (новый или прервал регистрацию)."""
    return user is None or (user.status == UserStatus.PENDING and not user.full_name)


def _inactive_text(user: User | None) -> str:
    """Ответ пользователю без доступа (нет в системе / заявка / заблокирован)."""
    if _needs_registration(user):
        return TXT_START_HINT
    assert user is not None
    if user.status == UserStatus.BLOCKED:
        return TXT_BLOCKED
    return TXT_PENDING


def _greeting(user: User) -> str:
    """Приветствие активного пользователя по роли."""
    name = esc(user.full_name) if user.full_name else "коллега"
    if user.role == Role.MANAGER:
        return (
            f"👋 Здравствуйте, {name}!\n"
            "Вы вошли как <b>начальник</b>.\n\n"
            f"• {BTN_NEW_TASK} — задача, ожидаемый результат, срок, приоритет и вес\n"
            f"• {BTN_REVIEW} — результаты сотрудников и оценка AI\n"
            f"• {BTN_PROPOSALS} — поручения, внесённые сотрудниками\n"
            f"• {BTN_TASKS} — все задачи и просрочки\n"
            f"• {BTN_TEAM} — коэффициент эффективности команды\n"
            f"• {BTN_STAFF} — заявки на доступ и роли\n"
            f"• {BTN_EXPORT} — отчёт в Excel\n\n"
            f"Подробнее — «{BTN_HELP}»."
        )
    return (
        f"👋 Здравствуйте, {name}!\n"
        "Здесь ваши задачи, сроки и оценка эффективности.\n\n"
        f"• {BTN_MY_TASKS} — задачи и сроки\n"
        f"• {BTN_PROPOSE} — внести устное поручение начальника\n"
        f"• {BTN_SUBMIT} — отчитаться о выполнении\n"
        f"• {BTN_MY_KPI} — ваш коэффициент эффективности\n\n"
        f"Подробнее — «{BTN_HELP}»."
    )


async def _bot_link(bot: Bot) -> str | None:
    try:
        me = await bot.me()
    except TelegramAPIError:
        return None
    return f"https://t.me/{me.username}" if me.username else None


async def _manager_extras(session: AsyncSession, bot: Bot) -> str:
    """Подсказки начальнику: новые заявки, как подключить сотрудников."""
    lines: list[str] = []
    pending = await users.list_pending(session)
    if pending:
        lines.append(f"📥 Новых заявок на доступ: <b>{len(pending)}</b> — откройте «{BTN_STAFF}».")
    if not await users.list_employees(session):
        link = await _bot_link(bot)
        where = f" ({esc(link)})" if link else ""
        lines.append(
            "💡 Сотрудников пока нет. Отправьте им ссылку на этого бота"
            f"{where}: они нажмут /start и заполнят анкету, а заявка придёт вам."
        )
    return "\n\n" + "\n\n".join(lines) if lines else ""


async def _send_welcome(event: Message | CallbackQuery, session: AsyncSession, bot: Bot, user: User) -> None:
    """Приветствие активного пользователя + главное меню; в режиме webhook — ещё сообщение с кнопкой
    приложения (Mini App). У сообщения одна клавиатура: меню — reply, кнопка приложения — inline,
    поэтому сообщений два. В polling (webapp_url пуст) — как раньше, одно сообщение."""
    text = _greeting(user)
    if user.role == Role.MANAGER:
        text += await _manager_extras(session, bot)
    await common.send_new(event, text, keyboards.main_menu(user))
    url = get_settings().webapp_url
    if url:
        hint = TXT_APP_MANAGER if user.role == Role.MANAGER else TXT_APP_EMPLOYEE
        await common.send_new(event, hint, keyboards.open_app_kb(url))


async def _send_status(event: Message | CallbackQuery, session: AsyncSession, bot: Bot, user: User | None) -> None:
    """Ответ по текущему статусу: активному — меню, остальным — подсказка (и убрать старое меню)."""
    if user is not None and user.is_active:
        await _send_welcome(event, session, bot, user)
    else:
        await common.send_new(event, _inactive_text(user), keyboards.main_menu(user))


async def _ensure_user(session: AsyncSession, event: Message | CallbackQuery, user: User | None) -> User | None:
    """Пользователь из middleware; если его нет (например, БД очищена) — создать заново."""
    if user is not None or event.from_user is None:
        return user
    tg = event.from_user
    user, _ = await users.register_or_get(session, tg.id, tg.username, tg.full_name)
    return user


async def _drop_question_kb(bot: Bot, state: FSMContext, chat_id: int) -> None:
    """Убрать inline-кнопки у предыдущего вопроса анкеты, чтобы их нельзя было нажать повторно."""
    data = await state.get_data()
    msg_id = data.get("q_msg_id")
    if not msg_id:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
    except TelegramAPIError:
        pass


async def _strip_dialog_prompts(bot: Bot, state: FSMContext, chat_id: int, keep: int | None = None) -> None:
    """Убрать inline-кнопки у последнего вопроса прерываемого диалога любого модуля (кроме keep).

    Иначе после /cancel под вопросом оставались бы «Завтра», «20 %» и т. п., которые отвечают
    только «Кнопка устарела».
    """
    if await state.get_state() is None:
        return
    data = await state.get_data()
    ids = {data.get(key) for key in _PROMPT_KEYS}
    for msg_id in ids:
        if not isinstance(msg_id, int) or msg_id == keep:
            continue
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
        except TelegramAPIError:
            pass


def _cancel_hint(current: str | None) -> str | None:
    """Пояснение к «Действие отменено.» для прерванного диалога: что не сохранено / что ждёт."""
    if not current:
        return None
    return _CANCEL_HINTS.get(current.split(":", 1)[0])


async def _clear_dialog(message: Message, state: FSMContext, bot: Bot) -> None:
    """Сбросить текущий диалог, убрав кнопки его последнего вопроса (анкета или диалог другого модуля)."""
    await _strip_dialog_prompts(bot, state, message.chat.id)
    await state.clear()


def _capitalize_name(name: str) -> str:
    """«иванов иван» / «ИВАНОВ ИВАН» -> «Иванов Иван» (и «петров-водкин» -> «Петров-Водкин»).

    Смешанный регистр («ван Дейк», «Gʻulomov») оставляем как ввели: человек написал так сознательно.
    """
    if name != name.lower() and name != name.upper():
        return name

    def cap(part: str) -> str:
        return part[:1].upper() + part[1:].lower()

    return " ".join("-".join(cap(part) for part in word.split("-")) for word in name.split(" "))


def _validate_full_name(raw: str) -> tuple[str | None, str | None]:
    """-> (нормализованное ФИО, None) или (None, текст ошибки)."""
    name = " ".join(unicodedata.normalize("NFC", raw).split())
    if len(name) < NAME_MIN_LEN:
        return None, "Слишком коротко."
    if len(name) > NAME_MAX_LEN:
        return None, f"Слишком длинно — не более {NAME_MAX_LEN} символов."
    if any(not (ch.isalpha() or ch in _NAME_EXTRA_CHARS) for ch in name):
        return None, "ФИО может содержать только буквы, пробелы, дефис и точку."
    words = [word for word in name.split(" ") if any(ch.isalpha() for ch in word)]
    if len(words) < 2:
        return None, "Укажите как минимум фамилию и имя через пробел."
    return _capitalize_name(name), None


def _validate_position(raw: str) -> tuple[str | None, str | None]:
    position = " ".join(raw.split())
    if len(position) < POSITION_MIN_LEN:
        return None, "Слишком коротко."
    if len(position) > POSITION_MAX_LEN:
        return None, f"Слишком длинно — не более {POSITION_MAX_LEN} символов."
    return position, None


# --- /start и регистрация ---------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    await _clear_dialog(message, state, bot)
    tg = message.from_user
    if tg is None:
        return
    # UserMiddleware загрузил пользователя ДО регистрации — берём то, что вернул сервис.
    user, _created = await users.register_or_get(session, tg.id, tg.username, tg.full_name)

    if user.is_active:
        await _send_welcome(message, session, bot, user)
        return
    if user.status == UserStatus.BLOCKED:
        await message.answer(TXT_BLOCKED, reply_markup=keyboards.main_menu(user))
        return
    if user.full_name:
        await message.answer(TXT_PENDING, reply_markup=keyboards.main_menu(user))
        return

    await state.set_state(RegistrationSG.full_name)
    question = await message.answer(TXT_ASK_NAME, reply_markup=keyboards.cancel_kb())
    await state.update_data(q_msg_id=question.message_id)


@router.message(RegistrationSG.full_name, TextInput())
async def reg_full_name(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot, user: User | None
) -> None:
    if not _needs_registration(user):
        # Статус изменился, пока шла анкета (например, Telegram ID добавлен в ADMIN_IDS).
        await state.clear()
        await _send_status(message, session, bot, user)
        return

    name, error = _validate_full_name(message.text or "")
    if error:
        await _drop_question_kb(bot, state, message.chat.id)
        question = await message.answer(
            f"⚠️ {error}\n\nВведите фамилию, имя и отчество. {TXT_NAME_EXAMPLE}",
            reply_markup=keyboards.cancel_kb(),
        )
        await state.update_data(q_msg_id=question.message_id)
        return

    await _drop_question_kb(bot, state, message.chat.id)
    await state.update_data(full_name=name)
    await state.set_state(RegistrationSG.position)
    question = await message.answer(
        f"Приятно познакомиться, {esc(name)}!\n\n{TXT_ASK_POSITION}",
        reply_markup=keyboards.skip_cancel_kb("skip"),
    )
    await state.update_data(q_msg_id=question.message_id)


@router.message(RegistrationSG.position, TextInput())
async def reg_position(
    message: Message, session: AsyncSession, state: FSMContext, bot: Bot, user: User | None
) -> None:
    position, error = _validate_position(message.text or "")
    if error:
        await _drop_question_kb(bot, state, message.chat.id)
        question = await message.answer(
            f"⚠️ {error}\n\nУкажите должность или нажмите «⏭ Пропустить».",
            reply_markup=keyboards.skip_cancel_kb("skip"),
        )
        await state.update_data(q_msg_id=question.message_id)
        return
    await _drop_question_kb(bot, state, message.chat.id)
    await _finish_registration(message, session, state, bot, user, position)


@router.callback_query(RegistrationSG.position, PickCB.filter(F.field == "skip"))
async def reg_position_skip(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot, user: User | None
) -> None:
    # Кнопка должна быть с текущего вопроса анкеты: «Пропустить» из прерванной раньше анкеты
    # (/start, /cancel, /help посреди регистрации) не должна завершать новую.
    q_msg_id = (await state.get_data()).get("q_msg_id")
    if callback.message is None or callback.message.message_id != q_msg_id:
        await callback.answer(TXT_STALE_BUTTON)
        await common.remove_markup(callback)
        return
    await callback.answer()
    await common.remove_markup(callback)
    await _finish_registration(callback, session, state, bot, user, None)


async def _finish_registration(
    event: Message | CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    bot: Bot,
    user: User | None,
    position: str | None,
) -> None:
    """Сохранить анкету, уведомить начальников. На callback отвечает вызывающий хендлер."""
    data = await state.get_data()
    full_name: str | None = data.get("full_name")
    user = await _ensure_user(session, event, user)

    if full_name is None or user is None or not _needs_registration(user):
        # ФИО потерялось (перезапуск бота) или статус уже другой — начать заново / показать статус.
        await state.clear()
        await _send_status(event, session, bot, user)
        return

    await users.complete_registration(session, user, full_name, position)
    await state.clear()
    await session.commit()

    try:
        await notify.notify_registration(bot, session, user)
    except Exception:  # уведомление не должно ломать регистрацию — анкета уже сохранена
        log.exception("Не удалось отправить начальникам заявку пользователя %s", user.id)

    summary = f"👤 {esc(user.full_name)}\n💼 {esc(user.position) if user.position else 'должность не указана'}"
    if await users.list_managers(session):
        text = (
            "✅ Заявка отправлена начальнику.\n\n"
            f"{summary}\n\n"
            "Как только вас подтвердят, придёт уведомление."
        )
    else:
        text = (
            "✅ Анкета сохранена.\n\n"
            f"{summary}\n\n"
            "⚠️ В боте пока нет ни одного начальника, поэтому заявку некому подтвердить. "
            "Сообщите об этом начальнику: ему нужно запустить бота (его Telegram ID должен быть "
            "указан в настройках ADMIN_IDS). Как только вас подтвердят, придёт уведомление."
        )
    await common.send_new(event, text, keyboards.main_menu(user))


# --- Отмена, помощь, меню ---------------------------------------------------------------------


async def _after_cancel(event: Message | CallbackQuery, user: User | None, hint: str | None = None) -> None:
    if user is not None and user.is_active:
        text = f"{TXT_CANCELLED} {hint}" if hint else TXT_CANCELLED
        await common.send_new(event, text, keyboards.main_menu(user))
        return
    text = TXT_CANCELLED + "\n\n" + _inactive_text(user)
    await common.send_new(event, text, keyboards.main_menu(user))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, bot: Bot, user: User | None) -> None:
    hint = _cancel_hint(await state.get_state())
    await _clear_dialog(message, state, bot)
    await _after_cancel(message, user, hint)


@router.callback_query(PickCB.filter(F.field == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot, user: User | None) -> None:
    hint = _cancel_hint(await state.get_state())
    clicked = callback.message.message_id if callback.message is not None else None
    chat_id = callback.message.chat.id if callback.message is not None else callback.from_user.id
    await _strip_dialog_prompts(bot, state, chat_id, keep=clicked)  # нажатое очистит remove_markup
    await state.clear()
    await callback.answer()
    await common.remove_markup(callback)
    await _after_cancel(callback, user, hint)


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message, state: FSMContext, bot: Bot, user: User | None) -> None:
    await _clear_dialog(message, state, bot)
    # Неактивному справка сама говорит, что делать: новичку — /start, подавшему заявку — что она
    # на рассмотрении, заблокированному — что доступ закрыт.
    await message.answer(render.help_text(user), reply_markup=keyboards.main_menu(user))


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, bot: Bot, user: User | None) -> None:
    await _clear_dialog(message, state, bot)
    if user is not None and user.is_active:
        await message.answer("🏠 Главное меню. Выберите действие на клавиатуре ниже 👇",
                             reply_markup=keyboards.main_menu(user))
        return
    await message.answer(_inactive_text(user), reply_markup=keyboards.main_menu(user))


# --- Доступ закрыт посреди диалога ------------------------------------------------------------
# Сотрудника заблокировали (или вернули в «ожидание»), пока он был в диалоге другого модуля
# (поручение, сдача результата…). FSM-хендлеры модулей ловят ввод по состоянию и роль не
# проверяют, поэтому без этой проверки заблокированный продолжил бы диалог. Роутер start
# подключается первым, команды выше продолжают работать.


async def _inactive_in_dialog(
    event: TelegramObject, state: FSMContext | None = None, user: User | None = None
) -> bool:
    """Зарегистрированный, но неактивный пользователь, у которого открыт какой-то диалог."""
    if state is None or user is None or user.is_active or _needs_registration(user):
        return False
    return await state.get_state() is not None


@router.message(_inactive_in_dialog)
async def inactive_dialog_message(message: Message, state: FSMContext, bot: Bot, user: User | None) -> None:
    await _clear_dialog(message, state, bot)
    await message.answer(_inactive_text(user), reply_markup=keyboards.main_menu(user))


@router.callback_query(_inactive_in_dialog)
async def inactive_dialog_callback(callback: CallbackQuery, state: FSMContext, user: User | None) -> None:
    await state.clear()
    await callback.answer(_inactive_text(user), show_alert=True)
    await common.remove_markup(callback)


# Всё остальное, что прислали во время анкеты (стикер, фото, неизвестная команда), — подсказка.
# Эти хендлеры стоят после команд, поэтому /start, /cancel, /help, /menu работают и здесь.


@router.message(RegistrationSG.full_name)
async def reg_full_name_other(message: Message, state: FSMContext, bot: Bot) -> None:
    await _drop_question_kb(bot, state, message.chat.id)
    question = await message.answer(TXT_NAME_NOT_TEXT, reply_markup=keyboards.cancel_kb())
    await state.update_data(q_msg_id=question.message_id)


@router.message(RegistrationSG.position)
async def reg_position_other(message: Message, state: FSMContext, bot: Bot) -> None:
    await _drop_question_kb(bot, state, message.chat.id)
    question = await message.answer(TXT_POSITION_NOT_TEXT, reply_markup=keyboards.skip_cancel_kb("skip"))
    await state.update_data(q_msg_id=question.message_id)


# --- Ловушка: подключается в main.py ПОСЛЕДНИМ ------------------------------------------------


@fallback_router.message()
async def fallback_message(message: Message, state: FSMContext, user: User | None) -> None:
    if user is None or not user.is_active:
        # Неактивному пользователю диалоги недоступны — сбросить зависшее состояние.
        await state.clear()
        await message.answer(_inactive_text(user), reply_markup=keyboards.main_menu(user))
        return
    if await state.get_state() is None:
        await message.answer(TXT_NOT_UNDERSTOOD, reply_markup=keyboards.main_menu(user))
        return
    # Идёт диалог, но ответ не подошёл ни одному шагу (например, стикер вместо текста).
    await message.answer(TXT_ANSWER_ABOVE)


@fallback_router.callback_query()
async def fallback_callback(callback: CallbackQuery, user: User | None = None) -> None:
    if user is not None and not user.is_active and not _needs_registration(user):
        # Например, «📤 Отправить» в диалоге, который сбросили при блокировке: объяснить почему.
        await callback.answer(_inactive_text(user), show_alert=True)
        return
    await callback.answer(TXT_STALE_BUTTON, show_alert=False)
