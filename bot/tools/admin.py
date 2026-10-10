"""Админы бота: ``python -m bot.tools.admin grant|revoke <Telegram ID>`` и ``python -m bot.tools.admin list``.

Админ — начальник со всеми правами, которого нельзя понизить или заблокировать. Отметка хранится в базе
(``users.is_admin``), поэтому настройки хостинга (ADMIN_IDS) менять не нужно и бота перезапускать тоже:
команда работает с базой из ``DATABASE_URL`` (файл ``.env`` в папке бота или переменные окружения).

* ``grant`` — человек уже зарегистрирован в боте: сразу становится начальником-админом. Ещё не писал
  боту: отметка ждёт его первого /start.
* ``revoke`` — снять отметку (человек остаётся обычным начальником).
* ``list`` — кто отмечен админом в базе.

Колонка ``users.is_admin`` добавляется в базу, если её ещё нет (как при запуске бота — ``init_db``).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.db.base import describe_url, init_db, make_engine, make_sessionmaker
from bot.services import users
from bot.services.errors import DomainError

__all__ = ["main", "run"]

ACTIONS = ("grant", "revoke", "list")


async def run(engine: AsyncEngine, action: str, tg_id: int | None = None) -> str:
    """Выполнить действие и вернуть сообщение для того, кто запустил команду. Бросает DomainError."""
    if action not in ACTIONS:
        raise DomainError(f"Неизвестное действие: {action}")
    await init_db(engine)
    async with make_sessionmaker(engine)() as session:
        if action == "list":
            admins = await users.list_admins(session)
            if not admins:
                return "Админов, отмеченных в базе, нет."
            return "Админы бота:\n" + "\n".join(f"  {_describe(user)}" for user in admins)
        if tg_id is None:
            raise DomainError("Укажите Telegram ID")
        if action == "grant":
            user = await users.grant_admin(session, tg_id)
            await session.commit()
            if user.is_manager:
                return f"Готово: {_describe(user)} — админ бота (начальник, которого нельзя снять)."
            return (
                f"Готово: Telegram ID {tg_id} отмечен админом. Он ещё не запускал бота — "
                "права появятся, как только он нажмёт «Запустить» (/start)."
            )
        user = await users.revoke_admin(session, tg_id)
        await session.commit()
        return f"Готово: {_describe(user)} больше не админ (остаётся обычным начальником)."


def _describe(user: object) -> str:
    name = getattr(user, "full_name", "") or "без имени"
    return f"{name} (Telegram ID {getattr(user, 'tg_id', '?')})"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m bot.tools.admin",
        description="Назначить или снять админа бота (начальника, которого нельзя понизить или заблокировать).",
    )
    parser.add_argument("action", choices=ACTIONS, help="grant — назначить, revoke — снять, list — показать")
    parser.add_argument("tg_id", nargs="?", type=int, help="Telegram ID (число из @userinfobot)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        from bot.config import get_settings

        database_url = get_settings().database_url
    except Exception as exc:  # noqa: BLE001 — понятное сообщение вместо трассировки
        print(f"Ошибка: не удалось прочитать настройки (.env): {type(exc).__name__}.", file=sys.stderr)
        return 2
    print(f"База: {describe_url(database_url)}", flush=True)

    async def _go() -> str:
        engine = make_engine(database_url)
        try:
            return await run(engine, args.action, args.tg_id)
        finally:
            await engine.dispose()

    try:
        print(asyncio.run(_go()))
    except DomainError as exc:
        print(f"Ошибка: {exc.message}", file=sys.stderr)
        return 1
    except (SQLAlchemyError, OSError) as exc:
        print(f"Ошибка: не удалось подключиться к базе или записать в неё: {type(exc).__name__}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
