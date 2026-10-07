"""Middleware апдейта: сессия БД, пользователь, повтор после оборванного соединения, обмены с базой.

* ``DbSessionMiddleware`` — одна сессия на апдейт: commit после хендлера, rollback при исключении.
* ``UserMiddleware`` — пользователь одним запросом по ``tg_id``; соединение, которое оборвалось,
  пока лежало в пуле (DBAPIError с ``connection_invalidated``), — один повтор на новом соединении;
  другие ошибки базы не повторяются.
* Пользователь, загруженный middleware, сервисы берут из сессии, не перечитывая (``get_by_tg``).

На PostgreSQL то же поведение с настоящим разрывом соединения — tests/test_postgres_compat.py
(раздел «Обмены с базой»).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.db.models import Role, User, UserStatus
from bot.middlewares import DbSessionMiddleware, UserMiddleware, load_user
from bot.services import users

pytestmark = pytest.mark.asyncio


class Statements:
    """SQL-выражения, ушедшие в базу через движок (для подсчёта запросов)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.items: list[str] = []

    def _on_execute(self, _conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        self.items.append(" ".join(statement.split()))

    def __enter__(self) -> Statements:
        event.listen(self.engine.sync_engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self.engine.sync_engine, "before_cursor_execute", self._on_execute)


async def _seed_user(sessionmaker: async_sessionmaker[AsyncSession], tg_id: int = 2001) -> int:
    async with sessionmaker() as session:
        user = User(tg_id=tg_id, full_name="Иванов Иван Иванович", role=Role.EMPLOYEE, status=UserStatus.ACTIVE)
        session.add(user)
        await session.commit()
        return user.id


async def _run_user_middleware(session: Any, tg_id: int | None) -> dict[str, Any]:
    data: dict[str, Any] = {"session": session}
    if tg_id is not None:
        data["event_from_user"] = SimpleNamespace(id=tg_id)
    seen: dict[str, Any] = {}

    async def handler(_event: object, handler_data: dict[str, Any]) -> str:
        seen.update(handler_data)
        return "handled"

    assert await UserMiddleware()(handler, object(), data) == "handled"
    return seen


# --- UserMiddleware --------------------------------------------------------------------------------


async def test_user_middleware_loads_author_of_update(sessionmaker, engine: AsyncEngine) -> None:
    user_id = await _seed_user(sessionmaker)
    async with sessionmaker() as session:
        with Statements(engine) as sql:
            seen = await _run_user_middleware(session, 2001)
        assert seen["user"].id == user_id
        assert len(sql.items) == 1 and sql.items[0].startswith("SELECT users.")  # один запрос, без связей

        assert (await _run_user_middleware(session, 9999))["user"] is None  # не зарегистрирован
    assert (await _run_user_middleware(None, 2001))["user"] is None  # апдейт без сессии
    async with sessionmaker() as session:
        assert (await _run_user_middleware(session, None))["user"] is None  # апдейт без автора


async def test_services_reuse_user_loaded_by_middleware(sessionmaker, engine: AsyncEngine) -> None:
    """/start: register_or_get ищет того же пользователя по tg_id — второй запрос не нужен."""
    await _seed_user(sessionmaker, tg_id=2001)
    async with sessionmaker() as session:
        user = await load_user(session, 2001)
        with Statements(engine) as sql:
            assert await users.get_by_tg(session, 2001) is user
            found, created = await users.register_or_get(session, 2001, None, "Иван")
        assert found is user and not created
        assert sql.items == []
        assert await users.get_by_tg(session, 2002) is None  # другого в сессии нет — обычный запрос


class FlakySession:
    """Сессия, у которой первый запрос падает с заданной ошибкой базы."""

    def __init__(self, error: DBAPIError, user: object) -> None:
        self.error = error
        self.user = user
        self.calls = 0
        self.rollbacks = 0

    async def scalar(self, _stmt: object) -> object:
        self.calls += 1
        if self.calls == 1:
            raise self.error
        return self.user

    async def rollback(self) -> None:
        self.rollbacks += 1


def _db_error(*, invalidated: bool) -> DBAPIError:
    return DBAPIError("SELECT users ...", {}, ConnectionResetError("connection was closed"), connection_invalidated=invalidated)


async def test_user_middleware_retries_once_after_dropped_connection() -> None:
    """Соединение оборвалось, пока лежало в пуле: запрос повторяется на новом, апдейт не падает."""
    user = SimpleNamespace(id=7)
    session = FlakySession(_db_error(invalidated=True), user)
    seen = await _run_user_middleware(session, 2001)
    assert seen["user"] is user
    assert (session.calls, session.rollbacks) == (2, 1)


async def test_user_middleware_does_not_retry_other_db_errors() -> None:
    session = FlakySession(_db_error(invalidated=False), SimpleNamespace(id=7))
    with pytest.raises(DBAPIError):
        await _run_user_middleware(session, 2001)
    assert (session.calls, session.rollbacks) == (1, 0)


# --- DbSessionMiddleware ---------------------------------------------------------------------------


class FakeSession:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def __aenter__(self) -> FakeSession:
        self.log.append("open")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.log.append("close")

    async def commit(self) -> None:
        self.log.append("commit")

    async def rollback(self) -> None:
        self.log.append("rollback")


async def test_db_session_middleware_commits_after_handler_and_rolls_back_on_error() -> None:
    log: list[str] = []
    middleware = DbSessionMiddleware(lambda: FakeSession(log))  # type: ignore[arg-type]

    async def ok(_event: object, data: dict[str, Any]) -> str:
        log.append("handler")
        assert isinstance(data["session"], FakeSession)
        return "done"

    assert await middleware(ok, object(), {}) == "done"
    assert log == ["open", "handler", "commit", "close"]

    log.clear()

    async def failing(_event: object, _data: dict[str, Any]) -> None:
        log.append("handler")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await middleware(failing, object(), {})
    assert log == ["open", "handler", "rollback", "close"]
