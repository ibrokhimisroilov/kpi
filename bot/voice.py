"""Голосовые сообщения в чате: ответ на вопрос диалога голосом (SPEC.md §13).

``VoiceMiddleware`` стоит перед хендлерами сообщений. Голосовое сообщение активного пользователя посреди
диалога о задаче (постановка, поручение, сдача, проверка, правка, отмена) распознаётся
(``bot.ai.dictate.transcribe``: узбекский и русский) и уходит дальше как обычный текст — диалогам ничего
знать о голосе не нужно. Пользователь видит «🎤 распознанный текст» и следующий шаг; ошибку исправляет,
как при вводе с клавиатуры (кнопка «✏️ Изменить» в сводке).

Мимо распознавания проходят (их получают обычные хендлеры):

* голосовое вне диалога и на первом шаге «Поставить задачу» / «Добавить поручение» — это задача целиком,
  её разбирает ``bot.handlers.voice``;
* регистрация (ФИО и должность вводятся текстом — имя не должно исказиться) и идущая отправка результата;
* сообщения неактивных пользователей.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

from bot.ai import dictate, progress
from bot.ai.dictate import VoiceError
from bot.config import get_settings
from bot.utils.text import esc, truncate

__all__ = ["ONE_SHOT_STATES", "VoiceMiddleware", "fetch_voice", "show_recognized", "wait_message"]

log = logging.getLogger(__name__)

# Группы состояний, где на вопрос можно ответить голосом (имя класса StatesGroup до двоеточия).
VOICE_GROUPS = frozenset(
    {"CreateTaskSG", "ProposeTaskSG", "DecideProposalSG", "SubmitSG", "ReviewSG", "EditTaskSG", "CancelTaskSG"}
)
# Первый шаг диалога: голосовое здесь — задача целиком (bot.handlers.voice), а не ответ на один вопрос.
ONE_SHOT_STATES = frozenset({"CreateTaskSG:assignee", "ProposeTaskSG:title"})
_SKIP_STATES = frozenset({"SubmitSG:sending"})

WAIT_TEXT = "🎤 Распознаю речь…"
DOWNLOAD_TIMEOUT_SEC = 60
_ECHO_LIMIT = 3500


def accepts_voice(state_name: str | None) -> bool:
    """На этом шаге диалога голосовое распознаётся и подставляется как текст ответа."""
    if not state_name or state_name in ONE_SHOT_STATES or state_name in _SKIP_STATES:
        return False
    return state_name.split(":", 1)[0] in VOICE_GROUPS


async def fetch_voice(message: Message, bot: Bot) -> tuple[bytes, str]:
    """Скачать голосовое сообщение -> (байты, MIME). VoiceError — голос выключен или запись слишком длинная."""
    voice = message.voice
    settings = get_settings()
    if voice is None or not settings.voice_enabled:
        raise VoiceError("off")
    too_long = VoiceError("too_long", limit=dictate.too_long_text())
    if (voice.duration or 0) > settings.voice_max_sec or (voice.file_size or 0) > dictate.MAX_AUDIO_BYTES:
        raise too_long
    try:
        buffer = await bot.download(voice.file_id, timeout=DOWNLOAD_TIMEOUT_SEC)
    except (TelegramAPIError, OSError) as exc:
        log.info("Не удалось скачать голосовое сообщение: %s", type(exc).__name__)
        raise VoiceError("unavailable") from exc
    data = buffer.read() if buffer is not None else b""
    if len(data) > dictate.MAX_AUDIO_BYTES:
        raise too_long
    return data, voice.mime_type or "audio/ogg"


async def wait_message(message: Message, text: str = WAIT_TEXT) -> Message:
    """«🎤 Распознаю речь…» — сразу, до скачивания записи и запроса к AI."""
    return await message.answer(text)


async def show_recognized(wait: Message, text: str) -> None:
    """Заменить «🎤 Распознаю речь…» распознанным текстом (или сообщением об ошибке)."""
    try:
        await wait.edit_text(text)
    except TelegramAPIError:
        try:
            await wait.answer(text)
        except TelegramAPIError as exc:  # чат недоступен — диалог продолжится и без показа текста
            log.debug("Не удалось показать распознанный текст: %s", type(exc).__name__)


def recognized_text(text: str) -> str:
    return f"🎤 <i>{esc(truncate(text, _ECHO_LIMIT))}</i>"


class VoiceMiddleware(BaseMiddleware):
    """Голосовой ответ на вопрос диалога -> текст (см. описание модуля)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.voice is None:
            return await handler(event, data)
        user = data.get("user")
        state: FSMContext | None = data.get("state")
        bot: Bot | None = data.get("bot")
        if user is None or not getattr(user, "is_active", False) or state is None or bot is None:
            return await handler(event, data)
        if not accepts_voice(await state.get_state()):
            return await handler(event, data)

        wait = await wait_message(event)
        session = data.get("session")
        if session is not None:
            await session.commit()  # запрос к AI — секунды: соединение с базой на это время не держим
        try:
            async with progress.typing(bot, event.chat.id):
                audio, mime_type = await fetch_voice(event, bot)
                text = await dictate.transcribe(audio, mime_type)
        except VoiceError as exc:
            await show_recognized(wait, exc.message)
            return None
        await show_recognized(wait, recognized_text(text))
        # Дальше — как будто пользователь написал этот текст: диалог получает обычное текстовое сообщение.
        return await handler(event.model_copy(update={"text": text, "voice": None}), data)
