"""Задача одним голосовым сообщением (SPEC.md §13).

Голосовое вне диалога (и на первом шаге «Поставить задачу» / «Добавить поручение») — это задача целиком:

* **начальник** — бот распознаёт речь и разбирает её на поля (``bot.ai.dictate.dictate_task``): исполнитель из
  списка сотрудников, название, измеримый результат, план, срок. Дальше — обычный мастер постановки
  (``task_create.start_dictated``): спрашивает только то, чего не было в сообщении, вес и приоритет —
  кнопками, затем сводка;
* **сотрудник** — у него голосовое может быть и поручением, и результатом по задаче: бот спрашивает двумя
  кнопками. «➕ Новое поручение» — черновик поручения (``task_propose.start_dictated``); «✅ Результат по
  задаче» — распознанный текст становится ответом «Что фактически сделано?» выбранной задачи
  (``task_submit.start_with_fact``). Задач в работе нет — сразу поручение; на первом шаге «Добавить
  поручение» вопрос тоже не задаётся.

Ответ голосом на вопрос посреди диалога обрабатывает ``bot.voice.VoiceMiddleware``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import voice
from bot.ai import dictate, progress
from bot.ai.dictate import Dictation, VoiceError
from bot.db.models import OPEN_STATUSES, Task, User
from bot.filters import IsEmployee, IsManager, TextInput
from bot.handlers import common, task_create, task_propose, task_submit
from bot.handlers.task_create import CreateTaskSG
from bot.handlers.task_propose import ProposeTaskSG
from bot.services import tasks as tasks_svc
from bot.services import users as users_svc
from bot.ui import keyboards, render
from bot.ui.callbacks import PickCB
from bot.utils.text import esc, own, truncate

log = logging.getLogger(__name__)

router = Router(name="voice")

WAIT_TASK = "🎤 Слушаю и разбираю задачу…"
TASK_LIMIT = 30  # задач в списке «по какой задаче результат»
STALE = "Эта кнопка уже неактуальна — отправьте голосовое ещё раз."
NO_OPEN_TASK = "Этой задачи уже нет среди задач в работе — выберите другую или нажмите «✖️ Отмена»."


class VoiceSG(StatesGroup):
    """Голосовое сотрудника вне диалога: что это и по какой задаче."""

    choose = State()  # поручение или результат?
    task = State()    # результат — по какой задаче?


def _dump(dictation: Dictation) -> dict[str, Any]:
    data = asdict(dictation)
    data["deadline"] = common.dt_to_state(dictation.deadline)
    return data


def _load(data: Any) -> Dictation | None:
    if not isinstance(data, dict) or not data.get("transcript"):
        return None
    try:
        return Dictation(**{**data, "deadline": common.dt_from_state(data.get("deadline"))})
    except (TypeError, ValueError):
        return None


async def _listen(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    employees: list[tuple[int, str]],
    author: str,
) -> Dictation | None:
    """«🎤 Слушаю…» -> распознать и разобрать сообщение. None — не вышло (пользователю уже сказано почему)."""
    wait = await voice.wait_message(message, WAIT_TASK)
    await session.commit()  # запрос к AI — секунды: соединение с базой на это время не держим
    try:
        async with progress.typing(bot, message.chat.id):
            audio, mime_type = await voice.fetch_voice(message, bot)
            dictation = await dictate.dictate_task(
                audio=audio, mime_type=mime_type, employees=employees, author=author
            )
    except VoiceError as exc:
        await voice.show_recognized(wait, exc.message)
        return None
    await voice.show_recognized(wait, voice.recognized_text(dictation.transcript))
    return dictation


# --- Начальник: задача целиком ------------------------------------------------------------------


@router.message(F.voice, IsManager(), StateFilter(None, CreateTaskSG.assignee))
async def manager_voice(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    employees = await users_svc.list_employees(session)
    if not employees:
        await state.clear()
        await message.answer(task_create.NO_EMPLOYEES_TEXT)
        return
    roster = [(employee.id, employee.full_name) for employee in employees]
    dictation = await _listen(message, bot, session, employees=roster, author="manager")
    if dictation is None:
        return
    assignee = next((employee for employee in employees if employee.id == dictation.assignee_id), None)
    await task_create.start_dictated(message, state, session, dictation, assignee)


# --- Сотрудник: поручение или результат ----------------------------------------------------------


@router.message(F.voice, IsEmployee(), StateFilter(None, ProposeTaskSG.title, VoiceSG.choose, VoiceSG.task))
async def employee_voice(message: Message, state: FSMContext, session: AsyncSession, user: User, bot: Bot) -> None:
    proposing = await state.get_state() == ProposeTaskSG.title.state
    dictation = await _listen(message, bot, session, employees=[], author="employee")
    if dictation is None:
        return
    if proposing:
        await task_propose.start_dictated(message, state, dictation)
        return
    open_tasks = await _open_tasks(session, user)
    if not open_tasks:
        await task_propose.start_dictated(message, state, dictation)
        return
    await state.clear()
    await state.set_state(VoiceSG.choose)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Новое поручение", callback_data=PickCB(field="voice", value="propose").pack())],
            [InlineKeyboardButton(text="✅ Результат по задаче", callback_data=PickCB(field="voice", value="result").pack())],
            *keyboards.cancel_kb().inline_keyboard,
        ]
    )
    sent = await message.answer(
        "Что это?\n"
        "➕ <b>Новое поручение</b> — задача, которую вам дали устно: начальник её подтвердит.\n"
        "✅ <b>Результат по задаче</b> — рассказ о том, что сделано по задаче в работе.",
        reply_markup=kb,
    )
    await state.update_data(dictation=_dump(dictation), msg_id=sent.message_id)


async def _open_tasks(session: AsyncSession, user: User) -> list[Task]:
    return await tasks_svc.list_tasks(session, assignee_id=user.id, statuses=OPEN_STATUSES, limit=TASK_LIMIT)


async def _checked(callback: CallbackQuery, state: FSMContext, user: User | None) -> Dictation | None:
    """Кнопка из последнего вопроса, пользователь — активный сотрудник, распознанный текст на месте."""
    data = await state.get_data()
    dictation = _load(data.get("dictation"))
    message_id = callback.message.message_id if callback.message is not None else None
    if user is None or not user.is_active or dictation is None or message_id != data.get("msg_id"):
        await callback.answer(STALE, show_alert=True)
        await common.remove_markup(callback)
        return None
    return dictation


@router.callback_query(VoiceSG.choose, PickCB.filter(F.field == "voice"))
async def choose_kind(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
) -> None:
    dictation = await _checked(callback, state, user)
    if dictation is None or user is None:
        return
    if callback_data.value == "propose":
        await callback.answer()
        await task_propose.start_dictated(callback, state, dictation)
        return
    open_tasks = await _open_tasks(session, user)
    if not open_tasks:
        await callback.answer("Задач в работе уже нет — оформляю как новое поручение.", show_alert=True)
        await task_propose.start_dictated(callback, state, dictation)
        return
    await callback.answer()
    if len(open_tasks) == 1:
        await _submit(callback, state, bot, open_tasks[0], dictation)
        return
    rows = [
        [InlineKeyboardButton(
            text=truncate(f"#{task.id} {own(task.title)}", 48),
            callback_data=PickCB(field="vtask", value=str(task.id)).pack(),
        )]
        for task in open_tasks
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[*rows, *keyboards.cancel_kb().inline_keyboard])
    lines = "\n".join(render.task_line(task) for task in open_tasks)
    await state.set_state(VoiceSG.task)
    sent = await common.edit_or_answer(callback, truncate(f"По какой задаче этот результат?\n\n{lines}"), kb)
    if sent is not None:
        await state.update_data(msg_id=sent.message_id)


@router.callback_query(VoiceSG.task, PickCB.filter(F.field == "vtask"))
async def choose_task(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
) -> None:
    dictation = await _checked(callback, state, user)
    if dictation is None or user is None:
        return
    task = await tasks_svc.get_task(session, int(callback_data.value)) if callback_data.value.isdigit() else None
    if task is None or task.assignee_id != user.id or not task.is_open:
        await callback.answer(NO_OPEN_TASK, show_alert=True)
        return
    await callback.answer()
    await _submit(callback, state, bot, task, dictation)


async def _submit(callback: CallbackQuery, state: FSMContext, bot: Bot, task: Task, dictation: Dictation) -> None:
    await common.edit_or_answer(callback, f"✅ Результат по задаче <b>#{task.id}</b> «{esc(task.title)}»")
    await task_submit.start_with_fact(callback, state, bot, task, dictation.transcript)


@router.message(StateFilter(VoiceSG), TextInput())
async def choose_hint(message: Message) -> None:
    await message.answer("👆 Выберите кнопкой выше, что это, — или нажмите «✖️ Отмена».")
