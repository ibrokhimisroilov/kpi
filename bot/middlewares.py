"""Middleware: сессия БД на каждый апдейт и загрузка текущего пользователя.

Обмены с базой. Бот в облаке ходит в базу в другом регионе (130–190 мс на обмен), поэтому:

* одна сессия (одно соединение из пула) на весь апдейт: её получают и UserMiddleware, и хендлер;
* апдейт, который только читал, не стоит транзакции: движок PostgreSQL откладывает BEGIN до первой
  записи (``bot.db.base``), и ``commit`` такой сессии в базу ничего не отправляет;
* пользователь читается одним запросом по уникальному индексу ``tg_id`` (у ``User`` нет связей),
  и сервисы потом берут его из сессии, не перечитывая (``services.users.get_by_tg``);
* соединение, которое оборвалось, пока лежало в пуле (сервер перезапустился, сеть разорвала
  простаивающее соединение), не превращается в «⚠️ Произошла ошибка»: первый запрос апдейта
  (чтение пользователя — до него ничего не записано и никому ничего не отправлено) повторяется
  один раз на новом соединении.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import User

log = logging.getLogger(__name__)


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
            user = await load_user(session, tg_user.id)
        data["user"] = user
        return await handler(event, data)


async def load_user(session: AsyncSession, tg_id: int) -> User | None:
    """Пользователь по Telegram ID — первый запрос апдейта к базе.

    Соединение оборвалось, пока лежало в пуле (SQLAlchemy распознал разрыв и пометил соединения
    пула негодными), — один повтор на новом соединении: в этой сессии ещё ничего не сделано.
    """
    stmt = select(User).where(User.tg_id == tg_id)
    try:
        return await session.scalar(stmt)
    except DBAPIError as exc:
        if not exc.connection_invalidated:
            raise
        log.info("Соединение с базой оборвалось (%s) — повторяю запрос на новом", type(exc.orig).__name__)
        await session.rollback()
        return await session.scalar(stmt)
