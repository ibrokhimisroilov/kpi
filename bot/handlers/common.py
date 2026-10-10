"""Общие хелперы для хендлеров (контрактный модуль, правится согласованно)."""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from bot.db.models import User

log = logging.getLogger(__name__)

AnyMarkup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None

NO_RIGHTS = "⛔ Недостаточно прав для этого действия."
NOT_FOUND = "Задача не найдена."
ALREADY_DONE = "Уже обработано."
# Уведомление сотруднику не дошло (notify_* вернул False/None): он заблокировал бота или у него
# больше нет доступа. Начальнику нельзя писать «исполнитель получил уведомление».
NOT_DELIVERED = (
    "⚠️ Уведомление не доставлено: у сотрудника нет доступа к боту или он заблокировал бота — "
    "сообщите ему лично."
)


async def edit_or_answer(
    event: Message | CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Для callback — редактирует сообщение с кнопкой; если нельзя — отправляет новое.

    Для Message — отправляет ответ. Ошибку «message is not modified» игнорирует.
    Не вызывает callback.answer() — это делает хендлер.
    """
    if isinstance(event, CallbackQuery):
        msg = event.message
        if isinstance(msg, Message) and msg.text is not None:
            try:
                edited = await msg.edit_text(text, reply_markup=reply_markup)
                return edited if isinstance(edited, Message) else msg
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return msg
                log.debug("edit_text failed, sending new message: %s", exc)
        if isinstance(msg, Message):
            return await msg.answer(text, reply_markup=reply_markup)
        if event.from_user and event.bot:
            return await event.bot.send_message(event.from_user.id, text, reply_markup=reply_markup)
        return None
    return await event.answer(text, reply_markup=reply_markup)


async def send_new(event: Message | CallbackQuery, text: str, reply_markup: AnyMarkup = None) -> Message | None:
    """Всегда отправляет новое сообщение в чат события (нужно для reply-клавиатуры главного меню)."""
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            return await event.message.answer(text, reply_markup=reply_markup)
        if event.from_user and event.bot:
            return await event.bot.send_message(event.from_user.id, text, reply_markup=reply_markup)
        return None
    return await event.answer(text, reply_markup=reply_markup)


async def remove_markup(callback: CallbackQuery) -> None:
    """Убирает inline-кнопки у сообщения (чтобы по ним нельзя было нажать повторно)."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass


async def deny(callback: CallbackQuery, text: str = NO_RIGHTS) -> None:
    await callback.answer(text, show_alert=True)


def is_manager(user: User | None) -> bool:
    return user is not None and user.is_manager


def dt_to_state(value: datetime | None) -> str | None:
    """datetime -> строка для хранения в FSM (MemoryStorage/Redis-совместимо)."""
    return value.isoformat() if value else None


def dt_from_state(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def clear_state(state: FSMContext | None) -> None:
    if state is not None:
        await state.clear()
