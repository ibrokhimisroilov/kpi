"""Middleware: сессия БД на каждый апдейт и загрузка текущего пользователя."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import User


class DbSessionMiddleware(BaseMiddleware):
    """Кладёт `session` в data. Коммит — после успешного хендлера, откат — при исключении.

    Хендлер может (и должен) вызывать `await session.commit()` сам перед долгими
    операциями (запрос к AI) и перед рассылкой уведомлений.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.sessionmaker() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result


class UserMiddleware(BaseMiddleware):
    """Кладёт в data `user: User | None` — запись из БД для автора апдейта."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        session: AsyncSession | None = data.get("session")
        user: User | None = None
        if tg_user is not None and session is not None:
            user = await session.scalar(select(User).where(User.tg_id == tg_user.id))
        data["user"] = user
        return await handler(event, data)
