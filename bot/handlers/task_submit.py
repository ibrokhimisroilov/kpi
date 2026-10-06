"""Сдача фактического результата сотрудником (SPEC 7.6; ТЗ, шаги 3–5).

Сценарий: «✅ Сдать результат» / /submit -> выбор задачи (TaskCB submit; та же кнопка приходит в карточке,
напоминаниях и уведомлении о доработке) -> «Что фактически сделано?» -> «Какой получен результат?» ->
«Фактическое значение?» (только если у задачи есть план-число) -> «Какие документы или материалы
подтверждают выполнение?» (файлы, фото, видео) -> сводка -> «📤 Отправить».

После отправки: submit_result -> commit -> предварительная оценка (AI или правила) -> commit ->
уведомление руководителю. Оценку AI сотруднику не показываем — решение принимает руководитель.
Если AI упал или думает слишком долго, оценка считается по правилам, а сдача всё равно уходит руководителю.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notify
from bot.ai import evaluate as ai_evaluate
from bot.ai import evidence as ai_evidence
from bot.ai.provider import ai_available
from bot.db.models import (
    OPEN_STATUSES,
    AttachmentKind,
    ReviewDecision,
    Submission,
    Task,
    TaskStatus,
    User,
)
from bot.filters import IsEmployee, TextInput
from bot.handlers import common
from bot.services import tasks as tasks_svc
from bot.services.errors import DomainError
from bot.ui import keyboards, render
from bot.ui.callbacks import PickCB, TaskCB
from bot.ui.texts import BTN_SUBMIT
from bot.utils.dates import to_local
from bot.utils.text import esc, fmt_num, parse_number, plural, truncate

log = logging.getLogger(__name__)

router = Router(name="task_submit")

MAX_TEXT = 3000            # «что сделано» / «какой результат», символов
MIN_FACT = 3               # совсем пустые ответы («да», «+») не принимаем
MAX_FILES = 20             # файлов в одной сдаче
MAX_NOTES = 1500           # текстовое описание материалов (ссылки, «отправил по почте») на шаге файлов
LIST_LIMIT = 50            # задач в списке «Сдать результат»
_AI_BUDGET_MARGIN_SEC = 5  # запас до общего срока оценки: AI заканчивает раньше, чем его прервут
ALBUM_DELAY_SEC = 1.0      # альбом приходит пачкой сообщений — отвечаем один раз, после последнего
MSG_LIMIT = 4000           # запас до лимита Telegram 4096

SKIP_RESULT = "skip_res"   # отдельные поля «Пропустить», чтобы старая кнопка не пропустила другой шаг
SKIP_VALUE = "skip_val"
LIST_TITLE = BTN_SUBMIT    # заголовок списка задач: по нему узнаём, что выбор сделан из списка
RULES_PREFIX = "Расчёт по правилам (AI недоступен): "
STALE_BUTTON = "Эта кнопка уже неактуальна — ответьте на последний вопрос."
NO_TASKS_TEXT = "Нет задач для сдачи.\nЗдесь появятся задачи в работе и на доработке."


class SubmitSG(StatesGroup):
    fact = State()      # 1. что фактически сделано
    result = State()    # 2. какой получен результат
    value = State()     # 3. фактическое значение (если у задачи есть план-число)
    files = State()     # 4. документы и материалы
    confirm = State()   # сводка, «📤 Отправить»
    sending = State()   # идёт отправка — защита от двойного нажатия


_TEXT_STATES = (SubmitSG.fact, SubmitSG.result, SubmitSG.value)
_FILE_STATES = (*_TEXT_STATES, SubmitSG.files, SubmitSG.confirm)
_FILE_STATE_NAMES = frozenset(s.state for s in _FILE_STATES)


# --- 0. Меню: список задач для сдачи --------------------------------------------------------------


@router.message(F.text == BTN_SUBMIT, IsEmployee())
@router.message(Command("submit"), IsEmployee())
async def submit_menu(message: Message, session: AsyncSession, user: User, state: FSMContext, bot: Bot) -> None:
    await _reset(state, bot, message.chat.id)
    tasks = await _open_tasks(session, user)
    if not tasks:
        await message.answer(NO_TASKS_TEXT, reply_markup=keyboards.main_menu(user))
        return
    await message.answer(_list_text(tasks), reply_markup=_tasks_kb(tasks))


async def _open_tasks(session: AsyncSession, user: User) -> list[Task]:
    return await tasks_svc.list_tasks(session, assignee_id=user.id, statuses=OPEN_STATUSES, limit=LIST_LIMIT)


def _list_text(tasks: list[Task]) -> str:
    header = f"<b>{LIST_TITLE}</b>\nВыберите задачу, по которой сдаёте результат:\n\n"
    body = "\n".join(render.task_line(task) for task in tasks)
    return header + truncate(body, MSG_LIMIT - len(header))


async def _refresh_list(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Нажата кнопка из устаревшего списка «Сдать результат» — показать актуальный список."""
    msg = callback.message
    if not isinstance(msg, Message) or not (msg.text or "").startswith(LIST_TITLE):
        return  # напоминание, карточку, уведомление не трогаем
    tasks = await _open_tasks(session, user)
    if tasks:
        await common.edit_or_answer(callback, _list_text(tasks), _tasks_kb(tasks))
    else:
        await common.edit_or_answer(callback, NO_TASKS_TEXT)


def _tasks_kb(tasks: list[Task]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for task in tasks:
        builder.button(text=_task_button(task), callback_data=TaskCB(action="submit", task_id=task.id))
    builder.adjust(1)
    return builder.as_markup()


def _task_button(task: Task) -> str:
    if tasks_svc.is_overdue(task):
        icon = "⏰"
    elif task.status == TaskStatus.REWORK:
        icon = "↩️"
    else:
        icon = "📌"
    title = " ".join(task.title.split())
    if len(title) > 40:
        title = title[:39].rstrip() + "…"
    return f"{icon} {title} · до {to_local(task.deadline):%d.%m}"


# --- 1. Начало сдачи (из списка, карточки, напоминания, уведомления о доработке) ------------------


@router.callback_query(TaskCB.filter(F.action == "submit"))
async def start_submit(
    callback: CallbackQuery,
    callback_data: TaskCB,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
    bot: Bot,
) -> None:
    if user is None or not user.is_active:
        await common.deny(callback)
        return
    task = await tasks_svc.get_task(session, callback_data.task_id)
    if task is None:
        await callback.answer(common.NOT_FOUND, show_alert=True)
        return
    if task.assignee_id != user.id:
        await common.deny(callback, "⛔ Сдать результат может только исполнитель задачи.")
        return
    if not task.is_open:
        # Кнопка из старого списка, напоминания или уведомления: результат уже сдан, задачу оценили
        # или отменили — alert объясняет, что именно; устаревший список «Сдать результат» обновляем.
        await callback.answer(_not_open_text(task), show_alert=True)
        await _refresh_list(callback, session, user)
        return
    await callback.answer()

    msg = callback.message
    dropped = await _abandoned_note(state, task.id)
    await _reset(state, bot, msg.chat.id if msg is not None else callback.from_user.id)
    data: dict[str, Any] = {
        "task_id": task.id,
        "plan_value": task.plan_value,
        "plan_unit": task.plan_unit,
        "files": [],
        "notes": [],
        "seq": 0,
    }
    await state.set_state(SubmitSG.fact)
    await state.set_data(data)

    question, kb = _step_prompt(SubmitSG.fact.state, data)
    intro = _intro_text(task)
    text = _fit(f"{dropped}\n\n{intro}" if dropped else intro, question)
    # Список «Сдать результат» превращаем в диалог; напоминание, карточку и замечания руководителя
    # не трогаем — диалог начинается новым сообщением.
    if isinstance(msg, Message) and (msg.text or "").startswith(LIST_TITLE):
        sent = await common.edit_or_answer(callback, text, kb)
    else:
        sent = await common.send_new(callback, text, kb)
    if sent is not None:
        await state.update_data(prompt_id=sent.message_id)


async def _abandoned_note(state: FSMContext, task_id: int) -> str | None:
    """Начатая сдача (ответы или файлы уже есть) сейчас сбросится новой — предупредить об этом."""
    current = await state.get_state()
    if current is None or current not in SubmitSG or current == SubmitSG.sending.state:
        return None
    data = await state.get_data()
    if not (data.get("fact") or data.get("files") or data.get("notes")):
        return None
    old_id = data.get("task_id")
    if old_id == task_id:
        return "ℹ️ Сдача начата заново: прежние ответы и файлы по этой задаче не сохранены."
    return f"ℹ️ Начатая сдача по задаче #{esc(old_id)} отменена: её ответы и файлы не сохранены."


def _intro_text(task: Task) -> str:
    lines = [
        "📤 <b>Сдача результата</b>",
        "",
        f"📌 <b>Задача #{task.id}:</b> {esc(task.title)}",
        f"🎯 <b>Ожидаемый результат:</b> {render.plan_text(task)}",
        f"⏳ <b>Срок:</b> {render.deadline_label(task)}",
    ]
    if task.status == TaskStatus.REWORK:
        lines.append("↩️ <b>Задача возвращена на доработку.</b>")
        comment = _rework_comment(task)
        if comment:
            # Сначала экранируем, потом режем: truncate рассчитана на HTML и выбросила бы хвост после «<».
            lines.append(f"💬 Комментарий руководителя: <i>{truncate(esc(comment), 1000)}</i>")
    if task.submissions:
        lines.append(f"🔁 Попытка сдачи №{len(task.submissions) + 1}")
    return "\n".join(lines)


_NOT_OPEN_TEXTS: dict[TaskStatus, str] = {
    TaskStatus.SUBMITTED: "📝 Результат уже отправлен и ждёт проверки руководителя.",
    TaskStatus.DONE: "✅ Задача уже выполнена и оценена — сдавать результат не нужно.",
    TaskStatus.CANCELLED: "🚫 Задача отменена руководителем — сдавать результат не нужно.",
    TaskStatus.PROPOSED: "📥 Поручение ещё не подтверждено руководителем — сдать результат можно после подтверждения.",
    TaskStatus.REJECTED: "❌ Поручение отклонено руководителем — сдавать результат не нужно.",
}


def _not_open_text(task: Task) -> str:
    """Почему по задаче сейчас нельзя сдать результат (коротко — помещается в alert)."""
    return _NOT_OPEN_TEXTS.get(task.status, "Задача не в работе — сдать результат нельзя.")


def _rework_comment(task: Task) -> str | None:
    """Комментарий руководителя к последней сдаче, возвращённой на доработку."""
    sub = task.last_submission
    if sub is not None and sub.decision == ReviewDecision.REWORK and sub.review_comment:
        return sub.review_comment
    return None


# --- 2. Вопросы -----------------------------------------------------------------------------------


def _total_steps(data: dict[str, Any]) -> int:
    return 4 if data.get("plan_value") is not None else 3


def _plan_amount(data: dict[str, Any]) -> str:
    return f"{fmt_num(data.get('plan_value'))} {esc(data.get('plan_unit') or '')}".strip()


def _step_prompt(state_name: str | None, data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Текст вопроса и клавиатура для шага диалога."""
    total = _total_steps(data)
    if state_name == SubmitSG.fact.state:
        return (
            f"<b>Шаг 1 из {total}. Что фактически сделано?</b>\n"
            "Опишите своими словами, что выполнено. "
            "Например: <i>«Проверено 110 договоров, в 12 выявлены нарушения»</i>.",
            keyboards.cancel_kb(),
        )
    if state_name == SubmitSG.result.state:
        return (
            f"<b>Шаг 2 из {total}. Какой получен результат?</b>\n"
            "Что получилось в итоге: документ, решение, эффект. "
            "Например: <i>«Подготовлен отчёт и рекомендации по нарушениям»</i>.\n"
            "Если добавить нечего — нажмите «⏭ Пропустить».",
            keyboards.skip_cancel_kb(SKIP_RESULT),
        )
    if state_name == SubmitSG.value.state:
        return (
            f"<b>Шаг 3 из {total}. Фактическое значение?</b>\n"
            f"План: <b>{_plan_amount(data)}</b>. Напишите число, которое получилось по факту, "
            f"например: <i>{fmt_num(data.get('plan_value'))}</i>.\n"
            "Если посчитать нельзя — нажмите «⏭ Пропустить».",
            keyboards.skip_cancel_kb(SKIP_VALUE),
        )
    count = len(data.get("files") or [])
    lines = [
        f"<b>Шаг {total} из {total}. Какие документы или материалы подтверждают выполнение?</b>",
        f"Пришлите файлы, фото или видео — можно несколько, до {MAX_FILES}. "
        "Можно и написать текстом, где лежат материалы (например, ссылку на папку).",
    ]
    if count:
        lines.append(f"📎 Уже приложено: {plural(count, 'файл', 'файла', 'файлов')}.")
        lines.append("Когда закончите — нажмите «✅ Готово».")
    else:
        lines.append("Если подтверждений нет — нажмите «📭 Без файлов».")
    return "\n".join(lines), keyboards.files_kb(count)


def _fit(head: str, tail: str) -> str:
    """Склеить шапку и вопрос, обрезав шапку так, чтобы вопрос точно поместился в сообщение."""
    return truncate(head, MSG_LIMIT - len(tail) - 2) + "\n\n" + tail


async def _go(event: Message | CallbackQuery, state: FSMContext, bot: Bot, step: State) -> None:
    """Перейти к следующему шагу и задать его вопрос."""
    await state.set_state(step)
    data = await state.get_data()
    text, kb = _step_prompt(step.state, data)
    await _send_prompt(event, state, bot, text, kb)


async def _send_prompt(
    event: Message | CallbackQuery,
    state: FSMContext,
    bot: Bot,
    text: str,
    kb: InlineKeyboardMarkup | None,
    *,
    album: str | None = None,
) -> None:
    """Показать вопрос: по кнопке — редактируем её сообщение, на ввод — отвечаем новым.

    У предыдущего вопроса убираем кнопки, чтобы по ним нельзя было нажать невпопад: кнопки диалога
    живут только в сообщении prompt_id (см. _stale). album — id альбома, на файл которого отвечаем:
    ответ на следующий файл того же альбома правит это же сообщение, а не шлёт новое.
    """
    data = await state.get_data()
    prev_id = data.get("prompt_id")
    if isinstance(event, CallbackQuery):
        msg = event.message
        clicked_id = msg.message_id if msg is not None else None
        if prev_id and prev_id != clicked_id:
            chat_id = msg.chat.id if msg is not None else event.from_user.id
            await _drop_kb(bot, chat_id, prev_id)
        sent = await common.edit_or_answer(event, text, kb)
    else:
        if album is not None and prev_id and data.get("prompt_album") == album:
            if await _edit_prompt(bot, event.chat.id, prev_id, text, kb):
                return
        if prev_id:
            await _drop_kb(bot, event.chat.id, prev_id)
        sent = await event.answer(text, reply_markup=kb)
    if sent is not None:
        await state.update_data(prompt_id=sent.message_id, prompt_album=album)


async def _edit_prompt(
    bot: Bot, chat_id: int, message_id: int, text: str, kb: InlineKeyboardMarkup | None
) -> bool:
    """Переписать текущий вопрос. False — не получилось (тогда шлём новое сообщение)."""
    try:
        await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return True
        log.debug("Не удалось обновить сообщение %s: %s", message_id, exc)
        return False
    except TelegramAPIError as exc:
        log.debug("Не удалось обновить сообщение %s: %s", message_id, exc)
        return False
    return True


async def _drop_kb(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramAPIError as exc:  # сообщение удалено / уже без кнопок — не важно
        log.debug("Не удалось убрать кнопки у сообщения %s: %s", message_id, exc)


async def _reset(state: FSMContext, bot: Bot, chat_id: int) -> None:
    """Сбросить FSM; если шла сдача результата — убрать кнопки у её последнего вопроса."""
    in_dialog = await state.get_state() in SubmitSG
    prompt_id = await state.get_value("prompt_id")
    await state.clear()
    if in_dialog and prompt_id:
        await _drop_kb(bot, chat_id, prompt_id)


async def _stale(callback: CallbackQuery, state: FSMContext) -> bool:
    """Кнопка не из текущего вопроса диалога (например, из прежней сдачи другой задачи) -> alert и True.

    Значения PickCB одинаковы во всех сдачах, поэтому без этой проверки старая кнопка
    «📤 Отправить» отправила бы данные текущего диалога.
    """
    msg = callback.message
    if msg is not None and msg.message_id == await state.get_value("prompt_id"):
        return False
    await callback.answer(STALE_BUTTON, show_alert=True)
    await common.remove_markup(callback)
    return True


def _text_error(text: str, *, min_len: int = 1) -> str | None:
    if len(text) > MAX_TEXT:
        return f"Слишком длинно: {len(text)} символов. Сократите до {MAX_TEXT} и отправьте ещё раз."
    if len(text) < min_len:
        return "Опишите чуть подробнее, пожалуйста."
    return None


# --- Шаг 1: что сделано ---------------------------------------------------------------------------


@router.message(SubmitSG.fact, TextInput())
async def on_fact(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if (error := _text_error(text, min_len=MIN_FACT)) is not None:
        await message.answer(error, reply_markup=keyboards.cancel_kb())
        return
    await state.update_data(fact=text)
    await _go(message, state, bot, SubmitSG.result)


# --- Шаг 2: какой результат -----------------------------------------------------------------------


@router.message(SubmitSG.result, TextInput())
async def on_result(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if (error := _text_error(text)) is not None:
        await _send_prompt(message, state, bot, error, keyboards.skip_cancel_kb(SKIP_RESULT))
        return
    await state.update_data(result=text)
    await _after_result(message, state, bot)


@router.callback_query(SubmitSG.result, PickCB.filter(F.field == SKIP_RESULT))
async def on_result_skip(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if await _stale(callback, state):
        return
    await callback.answer()
    await state.update_data(result=None)
    await _after_result(callback, state, bot)


async def _after_result(event: Message | CallbackQuery, state: FSMContext, bot: Bot) -> None:
    has_plan = await state.get_value("plan_value") is not None
    await _go(event, state, bot, SubmitSG.value if has_plan else SubmitSG.files)


# --- Шаг 3: фактическое значение (только при плане-числе) -----------------------------------------


@router.message(SubmitSG.value, TextInput())
async def on_value(message: Message, state: FSMContext, bot: Bot) -> None:
    value = parse_number(message.text or "")
    if value is None or not math.isfinite(value) or value < 0:
        await _send_prompt(
            message,
            state,
            bot,
            "Не понял число 🤔 Напишите, например: <i>110</i> — или нажмите «⏭ Пропустить».",
            keyboards.skip_cancel_kb(SKIP_VALUE),
        )
        return
    await state.update_data(fact_value=value)
    await _go(message, state, bot, SubmitSG.files)


@router.callback_query(SubmitSG.value, PickCB.filter(F.field == SKIP_VALUE))
async def on_value_skip(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if await _stale(callback, state):
        return
    await callback.answer()
    await state.update_data(fact_value=None)
    await _go(callback, state, bot, SubmitSG.files)


# --- Шаг 4: файлы ---------------------------------------------------------------------------------
# Файлы принимаются на любом шаге: многие сначала присылают документ, а потом отвечают на вопросы.


@router.message(StateFilter(*_FILE_STATES), F.document | F.photo | F.video)
async def on_file(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    item = _file_from_message(message)
    if item is None:
        return
    stored_at = await state.get_state()
    status, seq = await _store_file(state, item, message.caption)
    album = message.media_group_id
    if album is not None:
        # Каждый файл альбома приходит отдельным сообщением. Если апдейты обрабатываются параллельно,
        # отвечает только последнее (seq). Если строго по очереди (events isolation), следующий файл
        # ждёт этот, и ответ получает каждый — но правкой одного сообщения (album в _send_prompt);
        # пауза тогда не даёт упереться в лимит Telegram на частые правки.
        await asyncio.sleep(ALBUM_DELAY_SEC)
        if await state.get_value("seq") != seq:
            return
    current = await state.get_state()
    if current != stored_at or current not in _FILE_STATE_NAMES:
        # Пока ждали альбом, диалог ушёл дальше (файлы уже учтены в следующем шаге) или отменён.
        return

    data = await state.get_data()
    await state.update_data(overflow=False)
    count = len(data.get("files") or [])
    notes: list[str] = []
    if data.get("overflow"):
        notes.append(f"⚠️ Можно приложить не более {MAX_FILES} файлов — лишние не добавлены.")
    elif status == "dup" and album is None:
        notes.append("Этот файл уже был добавлен.")
    note = "\n".join(notes)

    if current == SubmitSG.confirm.state:
        head = f"📎 Файлов в сдаче: {count}." + (f"\n{note}" if note else "")
        await _show_summary(message, state, session, bot, note=head, album=album)
        return
    if current == SubmitSG.files.state:
        lines = [f"📎 Добавлено файлов: {count}"]
        if note:
            lines.append(note)
        if count < MAX_FILES:
            lines.append("Можно прислать ещё или нажать «✅ Готово».")
        else:
            lines.append("Нажмите «✅ Готово».")
        await _send_prompt(message, state, bot, "\n".join(lines), keyboards.files_kb(count), album=album)
        return
    question, kb = _step_prompt(current, data)
    head = f"📎 Сохранено файлов: {count} — приложу их к результату." + (f"\n{note}" if note else "")
    await _send_prompt(
        message, state, bot, f"{head}\nА сейчас ответьте на вопрос:\n\n{question}", kb, album=album
    )


def _file_from_message(message: Message) -> dict[str, Any] | None:
    """Вложение сообщения -> JSON-совместимый словарь (поля как у AttachmentIn)."""
    if message.document is not None:
        doc = message.document
        return _file_dict(
            AttachmentKind.DOCUMENT, doc.file_id, doc.file_unique_id, doc.file_name, doc.mime_type, doc.file_size
        )
    if message.photo:
        photo = message.photo[-1]  # самый большой размер
        return _file_dict(
            AttachmentKind.PHOTO, photo.file_id, photo.file_unique_id, None, "image/jpeg", photo.file_size
        )
    if message.video is not None:
        video = message.video
        return _file_dict(
            AttachmentKind.VIDEO, video.file_id, video.file_unique_id, video.file_name, video.mime_type,
            video.file_size,
        )
    return None


def _file_dict(
    kind: AttachmentKind,
    file_id: str,
    file_unique_id: str | None,
    file_name: str | None,
    mime_type: str | None,
    file_size: int | None,
) -> dict[str, Any]:
    return {
        "kind": kind.value,
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "file_name": file_name[:255] if file_name else None,
        "mime_type": mime_type[:128] if mime_type else None,
        "file_size": file_size,
    }


async def _store_file(state: FSMContext, item: dict[str, Any], caption: str | None = None) -> tuple[str, int]:
    """Добавить файл (и подпись к нему) в FSM. -> (added | dup | overflow, порядковый номер события).

    Подпись к файлу («Сводная таблица по 110 договорам») — это описание материалов: сохраняем её
    вместе с текстовыми пояснениями шага файлов, чтобы руководитель и AI её увидели.
    Между чтением и записью данных нет await с переключением задач (MemoryStorage),
    поэтому сообщения альбома, обрабатываемые параллельно, не теряют файлы друг друга.
    """
    data = await state.get_data()
    files = list(data.get("files") or [])
    notes = list(data.get("notes") or [])
    seq = int(data.get("seq") or 0) + 1
    overflow = bool(data.get("overflow"))
    unique = item.get("file_unique_id")
    if unique and any(f.get("file_unique_id") == unique for f in files):
        status = "dup"
    elif len(files) >= MAX_FILES:
        status, overflow = "overflow", True
    else:
        files.append(item)
        status = "added"
        note = " ".join((caption or "").split())
        budget = MAX_NOTES - sum(len(n) for n in notes)
        if note and budget > 0:
            notes.append(f"{_file_label(item, len(files))}: {note}"[:budget])
    await state.update_data(files=files, notes=notes, seq=seq, overflow=overflow)
    return status, seq


@router.message(SubmitSG.files, TextInput())
async def on_materials_text(message: Message, state: FSMContext, bot: Bot) -> None:
    """Текст на шаге файлов — описание материалов (ссылка на папку, «отправил по почте» и т. п.)."""
    text = (message.text or "").strip()
    data = await state.get_data()
    notes = list(data.get("notes") or [])
    count = len(data.get("files") or [])
    if sum(len(n) for n in notes) + len(text) > MAX_NOTES:
        await _send_prompt(
            message,
            state,
            bot,
            f"Слишком длинно: описание материалов — до {MAX_NOTES} символов. "
            "Пришлите файлы или нажмите кнопку ниже.",
            keyboards.files_kb(count),
        )
        return
    notes.append(text)
    await state.update_data(notes=notes)
    action = "«✅ Готово»" if count else "«📭 Без файлов», если файлов не будет"
    await _send_prompt(
        message,
        state,
        bot,
        f"📝 Записал — добавлю к результату как описание подтверждающих материалов.\n"
        f"Можно прислать файлы или нажать {action}.",
        keyboards.files_kb(count),
    )


@router.message(SubmitSG.files, ~F.text)
async def on_files_unsupported(message: Message, state: FSMContext, bot: Bot) -> None:
    count = len(await state.get_value("files") or [])
    await _send_prompt(
        message,
        state,
        bot,
        "Такой тип сообщения не подходит. Пришлите документ, фото или видео "
        "(другие форматы можно отправить как файл: 📎 → Файл).",
        keyboards.files_kb(count),
    )


@router.callback_query(SubmitSG.files, PickCB.filter(F.field == "files"))
async def on_files_done(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    # «✅ Готово» и «📭 Без файлов» ведут к сводке; уже присланные файлы не теряем.
    if await _stale(callback, state):
        return
    await callback.answer()
    await _show_summary(callback, state, session, bot)


# --- Нетекстовый ввод на текстовых шагах ----------------------------------------------------------


@router.message(StateFilter(*_TEXT_STATES), ~F.text)
async def on_text_step_other(message: Message) -> None:
    await message.answer("✍️ Ответьте на вопрос выше текстом, пожалуйста.")


# --- 5. Сводка и отправка -------------------------------------------------------------------------


async def _show_summary(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    note: str | None = None,
    album: str | None = None,
) -> None:
    data = await state.get_data()
    task_id = data.get("task_id")
    task = await tasks_svc.get_task(session, int(task_id)) if task_id else None
    if task is None or not task.is_open:
        # Пока сотрудник отвечал на вопросы, задачу отменили / сдали из другого диалога / удалили.
        reason = "⚠️ Задача не найдена — сдача результата отменена." if task is None else _not_open_text(task)
        # Через _send_prompt — чтобы у последнего вопроса не осталось кнопок.
        await _send_prompt(event, state, bot, reason, None)
        await state.clear()
        return
    await state.set_state(SubmitSG.confirm)
    text = _summary_text(task, data)
    if note:
        text = _fit(note, text)
    await _send_prompt(event, state, bot, text, keyboards.confirm_kb("📤 Отправить", edit=False), album=album)


def _summary_text(task: Task, data: dict[str, Any]) -> str:
    files = data.get("files") or []
    notes = data.get("notes") or []
    result = data.get("result")
    fact_value = data.get("fact_value")
    lines = [
        "📤 <b>Проверьте перед отправкой</b>",
        "",
        f"📌 <b>Задача #{task.id}:</b> {esc(task.title)}",
        f"🎯 <b>Ожидаемый результат:</b> {render.plan_text(task)}",
        "",
        # Сначала экранируем, потом режем: truncate рассчитана на HTML и выбросила бы хвост после «<».
        f"✅ <b>Что сделано:</b> {truncate(esc(data.get('fact') or ''), 1200)}",
        f"📈 <b>Результат:</b> {truncate(esc(result), 800) if result else '—'}",
    ]
    if task.plan_value is not None:
        unit = f" {esc(task.plan_unit)}" if task.plan_unit else ""
        fact = f"<b>{fmt_num(fact_value)}{unit}</b>" if fact_value is not None else "не указан"
        lines.append(f"🔢 <b>План → факт:</b> {fmt_num(task.plan_value)}{unit} → {fact}")
    if files:
        names = [esc(_file_label(f, i)) for i, f in enumerate(files, 1)]
        shown = ", ".join(names[:10])
        more = f" и ещё {len(names) - 10}" if len(names) > 10 else ""
        lines.append(f"📎 <b>Файлы ({len(files)}):</b> {shown}{more}")
    else:
        lines.append("📎 <b>Файлы:</b> не приложены")
    if notes:
        lines.append(f"🔗 <b>Материалы:</b> {truncate(esc('; '.join(notes)), 600)}")
    if tasks_svc.is_overdue(task):
        lines.append("")
        lines.append("⚠️ Срок уже прошёл — результат будет отмечен как сданный с опозданием.")
    footer = "\n\nВсё верно? Нажмите «📤 Отправить». Передумали — «✖️ Отмена»."
    return truncate("\n".join(lines), MSG_LIMIT - len(footer)) + footer


def _file_label(item: dict[str, Any], number: int) -> str:
    if item.get("file_name"):
        return str(item["file_name"])
    if item.get("kind") == AttachmentKind.PHOTO.value:
        return f"Фото {number}"
    if item.get("kind") == AttachmentKind.VIDEO.value:
        return f"Видео {number}"
    return f"Файл {number}"


@router.message(SubmitSG.confirm, TextInput())
@router.message(SubmitSG.confirm, ~F.text)
async def on_confirm_input(message: Message) -> None:
    await message.answer("Проверьте сводку выше и нажмите «📤 Отправить» или «✖️ Отмена».")


@router.callback_query(SubmitSG.confirm, PickCB.filter(F.field == "confirm"))
async def on_confirm(
    callback: CallbackQuery,
    callback_data: PickCB,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
    bot: Bot,
) -> None:
    # Кнопка из старой сводки (в т.ч. по другой задаче) не должна отправить текущий диалог.
    if await _stale(callback, state):
        return
    if callback_data.value != "yes":
        await callback.answer()
        return
    if user is None or not user.is_active:
        await state.clear()
        await common.deny(callback)
        return
    data = await state.get_data()
    await state.set_state(SubmitSG.sending)  # повторное нажатие не создаст вторую сдачу
    task_id = data.get("task_id")
    if not task_id or not data.get("fact"):
        await _clear_if(state, SubmitSG.sending)
        await callback.answer("Данные сдачи потеряны — начните заново.", show_alert=True)
        await common.edit_or_answer(callback, "⚠️ Данные сдачи потеряны. Начните заново: «✅ Сдать результат».")
        return
    files = list(data.get("files") or [])

    try:
        sub = await tasks_svc.submit_result(
            session,
            int(task_id),
            user,
            fact_text=data["fact"],
            result_text=_result_with_notes(data.get("result"), data.get("notes") or []),
            fact_value=data.get("fact_value"),
            attachments=[_attachment_in(item) for item in files],
        )
        await session.commit()  # сдача сохранена до любых обращений к AI
    except DomainError as exc:
        # Например, задачу отменили или изменили статус, пока сотрудник заполнял ответы.
        await session.rollback()
        await _clear_if(state, SubmitSG.sending)
        reason = await _refusal_reason(session, int(task_id), exc.message)
        await callback.answer(reason, show_alert=True)
        await common.edit_or_answer(callback, f"⚠️ <b>Результат не отправлен.</b>\n{esc(reason)}")
        return
    except Exception:
        # Сбой БД и т. п.: возвращаем сводку в рабочее состояние, чтобы можно было нажать ещё раз.
        if await state.get_state() == SubmitSG.sending.state:
            await state.set_state(SubmitSG.confirm)
        raise
    await _clear_if(state, SubmitSG.sending)
    await callback.answer("📤 Отправлено")

    task = sub.task
    title = task.title
    sub_id = sub.id
    task_line = f"📌 Задача #{task_id}: {esc(title)}"
    await common.edit_or_answer(
        callback,
        f"⏳ <b>Анализирую результат…</b>\n{task_line}\n"
        "Это может занять до пары минут — сообщение обновится само.",
    )

    current: tuple[Task, Submission] | None = (task, sub)
    try:
        current = await _evaluate(bot, session, task, sub)
    except Exception:  # noqa: BLE001 - сдача уже сохранена, руководитель должен её получить
        log.exception("Не удалось сохранить предварительную оценку сдачи #%s", sub_id)
        try:
            current = await _reload(session, int(task_id), sub_id)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось перечитать сдачу #%s", sub_id)
            current = None
    if current is not None:
        # Пока AI думал (до пары минут), руководитель мог уже решить по сдаче из «📝 На проверке»
        # или отменить задачу — сессия этого не видит, поэтому перечитываем свежее состояние.
        current = await _fresh(session, int(task_id), sub_id, current)
    if current is not None and _awaits_review(*current):
        try:
            await notify.notify_submission(bot, session, *current)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось уведомить руководителя о сдаче #%s", sub_id)
    elif current is not None:
        log.info("Сдача #%s уже не ждёт проверки (%s) — уведомление руководителю не нужно", sub_id, current[0].status)

    status = current[0].status if current is not None else TaskStatus.SUBMITTED
    files_line = f"\n📎 Файлов: {len(files)}" if files else ""
    if status == TaskStatus.CANCELLED:
        head = "⚠️ <b>Результат сохранён, но руководитель тем временем отменил задачу</b> — проверять его не будут."
    elif status in (TaskStatus.DONE, TaskStatus.REWORK):
        head = "✅ <b>Результат отправлен.</b> Руководитель уже принял решение — оно пришло отдельным сообщением."
    else:
        head = "✅ <b>Результат отправлен руководителю на проверку.</b> Решение придёт сюда."
    await common.edit_or_answer(callback, f"{head}\n\n{task_line}{files_line}")


def _result_with_notes(result: str | None, notes: list[str]) -> str | None:
    """«Какой результат» + текстовое описание подтверждающих материалов (если было)."""
    parts = [result] if result else []
    if notes:
        parts.append("Подтверждающие материалы: " + "; ".join(notes))
    return "\n\n".join(parts) or None


def _attachment_in(item: dict[str, Any]) -> tasks_svc.AttachmentIn:
    return tasks_svc.AttachmentIn(
        kind=AttachmentKind(item["kind"]),
        file_id=item["file_id"],
        file_unique_id=item.get("file_unique_id"),
        file_name=item.get("file_name"),
        mime_type=item.get("mime_type"),
        file_size=item.get("file_size"),
    )


async def _refusal_reason(session: AsyncSession, task_id: int, fallback: str) -> str:
    """Понятная причина отказа в сдаче: по свежему статусу задачи («отменена», «уже оценена»…)."""
    try:
        session.expunge_all()  # после rollback объекты протухли — читаем задачу заново
        task = await tasks_svc.get_task(session, task_id)
    except Exception:  # noqa: BLE001 - причина — лишь пояснение, отказ уже состоялся
        log.exception("Не удалось перечитать задачу #%s после отказа в сдаче", task_id)
        task = None
    reason = _not_open_text(task) if task is not None and not task.is_open else fallback
    return reason[:200]  # лимит Telegram на текст alert


async def _clear_if(state: FSMContext, expected: State) -> None:
    """Сбросить состояние, только если пользователь не начал за это время другой диалог."""
    if await state.get_state() == expected.state:
        await state.clear()


# --- Предварительная оценка -----------------------------------------------------------------------


def _ai_budget_sec() -> float:
    """Сколько ждать AI целиком (скачивание файлов + перебор моделей), потом — правила.

    Если бот остановят посреди оценки (обновление на хостинге, сбой), сдачу без оценки позже найдут
    задания по расписанию (bot.scheduler.jobs.recover_stalled_evaluations): оценят по правилам и
    передадут руководителю.
    """
    return ai_evaluate.evaluation_budget_sec()


async def _ai_evaluation(bot: Bot, task: Task, sub: Submission) -> ai_evaluate.Evaluation | None:
    """Оценка AI (или правил — решает evaluate_submission). None — упало или не уложилось во время."""

    loop = asyncio.get_running_loop()
    budget = _ai_budget_sec()
    ends_at = loop.time() + budget

    async def run() -> ai_evaluate.Evaluation:
        # Без AI файлы скачивать незачем: evaluate_submission всё равно посчитает по правилам.
        evidence = await ai_evidence.collect_evidence(bot, list(sub.attachments)) if ai_available() else []
        # Перебор моделей — только в оставшееся после скачивания файлов время (с запасом), чтобы AI успел
        # ответить или отказаться сам, а не был прерван по общему сроку ниже.
        left = ends_at - loop.time() - _AI_BUDGET_MARGIN_SEC
        return await ai_evaluate.evaluate_submission(task, sub, evidence, time_budget=left)

    try:
        return await asyncio.wait_for(run(), timeout=budget)
    except TimeoutError:
        log.warning("Оценка сдачи #%s не уложилась в %.0f с — считаю по правилам", sub.id, budget)
    except Exception:  # noqa: BLE001 - оценка должна быть всегда
        log.exception("Ошибка оценки сдачи #%s — считаю по правилам", sub.id)
    return None


async def _evaluate(bot: Bot, session: AsyncSession, task: Task, sub: Submission) -> tuple[Task, Submission]:
    """Оценить сдачу и сохранить оценку (с commit). При любой проблеме с AI — rules_score."""
    task_id, sub_id = task.id, sub.id
    plan_value, fact_value, late_days = task.plan_value, sub.fact_value, float(sub.late_days or 0.0)

    evaluation = await _ai_evaluation(bot, task, sub)
    if evaluation is not None:
        try:
            await tasks_svc.record_evaluation(
                session,
                sub_id,
                score=evaluation.score,
                rationale=evaluation.rationale,
                source=evaluation.source,
                model=evaluation.model,
            )
            await session.commit()
            return task, sub
        except Exception:  # noqa: BLE001 - например, некорректный ответ модели; пробуем правила
            log.exception("Не удалось сохранить оценку AI для сдачи #%s — считаю по правилам", sub_id)
            task, sub = await _reload(session, task_id, sub_id)

    score, explanation = ai_evaluate.rules_score(plan_value, fact_value, late_days)
    await tasks_svc.record_evaluation(
        session, sub_id, score=score, rationale=RULES_PREFIX + explanation, source="rules", model=None
    )
    await session.commit()
    return task, sub


async def _fresh(
    session: AsyncSession, task_id: int, sub_id: int, fallback: tuple[Task, Submission]
) -> tuple[Task, Submission]:
    """Свежие задача и сдача из БД (изменения других пользователей видны); при сбое — fallback."""
    try:
        return await _reload(session, task_id, sub_id)
    except Exception:  # noqa: BLE001 - уведомить руководителя важнее, чем идеально свежие данные
        log.exception("Не удалось перечитать сдачу #%s перед уведомлением", sub_id)
        return fallback


def _awaits_review(task: Task, sub: Submission) -> bool:
    """Сдача ещё ждёт решения: задача на проверке, сдача последняя и без решения."""
    last = task.last_submission
    return task.status == TaskStatus.SUBMITTED and sub.decision is None and last is not None and last.id == sub.id


async def _reload(session: AsyncSession, task_id: int, sub_id: int) -> tuple[Task, Submission]:
    """Откатить неудачную транзакцию и заново загрузить задачу и сдачу (старые объекты протухли)."""
    await session.rollback()
    session.expunge_all()
    task = await tasks_svc.get_task(session, task_id)
    sub = next((s for s in task.submissions if s.id == sub_id), None) if task is not None else None
    if task is None or sub is None:
        raise RuntimeError(f"Сдача #{sub_id} задачи #{task_id} не найдена после отката")
    return task, sub


# --- Устаревшие кнопки внутри диалога (регистрируются последними) ---------------------------------


@router.callback_query(SubmitSG.sending, PickCB.filter(F.field != "cancel"))
async def on_busy(callback: CallbackQuery) -> None:
    await callback.answer("⏳ Результат уже отправляется…")


@router.callback_query(StateFilter(SubmitSG), PickCB.filter(F.field != "cancel"))
async def on_stale_button(callback: CallbackQuery) -> None:
    await callback.answer(STALE_BUTTON)
