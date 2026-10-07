"""«Печатает…» в чате, пока бот ждёт AI (подсказка формулировки, оценка сдачи).

Telegram показывает действие ``sendChatAction`` около 5 секунд или до следующего сообщения бота, поэтому
действие повторяется каждые ``TYPING_INTERVAL_SEC`` секунды, пока открыт блок ``async with typing(...)``.
Отправлять его стоит ПОСЛЕ сообщения «⏳ …»: новое сообщение бота сбрасывает «печатает…» у собеседника.

Индикатор — только украшение: ошибка Telegram (нет сети, чат недоступен, лимит запросов) его молча
выключает и никогда не мешает основной работе; выход из блока не ждёт незаконченный запрос к Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Bot
from aiogram.enums import ChatAction

__all__ = ["TYPING_INTERVAL_SEC", "typing"]

log = logging.getLogger(__name__)

TYPING_INTERVAL_SEC = 4.0  # чуть меньше 5 с, которые Telegram показывает действие

# Сильные ссылки на задачи индикатора (asyncio хранит только слабые): отменённая задача не исчезнет,
# пока не завершится.
_tasks: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def typing(
    bot: Bot | None, chat_id: int | None, *, interval: float = TYPING_INTERVAL_SEC
) -> AsyncIterator[None]:
    """Показывать «печатает…» в чате chat_id, пока выполняется блок (bot или chat_id None — ничего)."""
    if bot is None or chat_id is None:
        yield
        return
    task = asyncio.create_task(_keep_typing(bot, chat_id, interval), name=f"typing-{chat_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    try:
        yield
    finally:
        # Не ждём: запрос к Telegram мог зависнуть, а ответ пользователю уже готов.
        task.cancel()


async def _keep_typing(bot: Bot, chat_id: int, interval: float) -> None:
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - индикатор не важнее ответа: при ошибке просто перестаём
            log.debug("«Печатает…» в чат %s не отправлено: %s", chat_id, type(exc).__name__)
            return
        await asyncio.sleep(interval)
