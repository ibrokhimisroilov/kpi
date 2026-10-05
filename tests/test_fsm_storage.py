"""DbStorage — состояния диалогов (FSM aiogram) в таблице fsm_state (bot/fsm_storage.py, SPEC §10.1).

* запись/чтение состояния и данных, update_data (слияние), очистка удаляет строку, разные собеседники,
  чаты и «destiny» не смешиваются, данные переживают перезапуск (новый экземпляр хранилища и движка);
* данные только JSON-совместимые: понятная ошибка FsmDataError, кортежи -> списки, как у RedisStorage;
* режимы: прямой (PostgreSQL; на SQLite — для проверки), «запись вдогонку» (SQLite в файле: хендлер
  держит незакоммиченную запись, а хранилище не ждёт busy_timeout), обобщённый UPSERT (прочие СУБД),
  SQLite в памяти — MemoryStorage (не трогает транзакцию хендлера);
* PostgreSQL: у хранилища свой пул (make_storage_engine) — апдейты, занявшие основной пул, не ждут
  друг друга при записи состояния;
* сценарий бота через e2e-harness: анкета сотрудника продолжается после «перезапуска» бота.

PostgreSQL — если задан TEST_DATABASE_URL (пустая тестовая база: тесты пересоздают в ней таблицы бота),
иначе эти варианты пропускаются.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import DefaultKeyBuilder, StorageKey
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot import fsm_storage
from bot.db.base import Base, init_db, make_engine, make_sessionmaker, make_storage_engine
from bot.db.models import FsmState, Role, User, UserStatus
from bot.fsm_storage import DbStorage, FsmDataError

BOT_ID = 42
EMP, EMP2, MGR = 2001, 2002, 1001
PG_URL = os.environ.get("TEST_DATABASE_URL", "")


class Form(StatesGroup):
    name = State()
    position = State()


def key(user: int = EMP, chat: int | None = None, *, destiny: str = "default") -> StorageKey:
    return StorageKey(bot_id=BOT_ID, chat_id=chat if chat is not None else user, user_id=user, destiny=destiny)


def row_key(user: int = EMP, chat: int | None = None, *, destiny: str = "default") -> str:
    return f"fsm:{BOT_ID}:{chat if chat is not None else user}:{user}:{destiny}"


# --- Базы и хранилища -------------------------------------------------------------------------------


@dataclass
class Db:
    url: str
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]

    async def rows(self) -> dict[str, tuple[str | None, dict[str, Any]]]:
        """Строки fsm_state, как они лежат в базе: ключ -> (state, data)."""
        async with self.sessionmaker() as session:
            result = await session.execute(select(FsmState.key, FsmState.state, FsmState.data))
            return {row.key: (row.state, row.data) for row in result}

    async def reopen(self) -> Db:
        """«Перезапуск бота»: старый движок закрыт, новый подключается к той же базе."""
        await self.engine.dispose()
        engine = make_engine(self.url)
        await init_db(engine)
        return Db(self.url, engine, make_sessionmaker(engine))


async def open_db(url: str, *, recreate: bool = False) -> Db:
    engine = make_engine(url)
    if recreate:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await init_db(engine)
    return Db(url, engine, make_sessionmaker(engine))


def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'data' / 'bot.db').as_posix()}"


# Режимы хранилища: прямая запись, запись вдогонку, обобщённый UPSERT (прочие СУБД), PostgreSQL.
MODES = ["direct", "write_behind", "generic", "postgresql"]
DIRECT_MODES = ["direct", "postgresql"]  # несколько экземпляров бота на одной базе


@pytest_asyncio.fixture
async def db(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Db]:
    mode = getattr(request, "param", "direct")
    if mode == "postgresql":
        if not PG_URL:
            pytest.skip("TEST_DATABASE_URL не задан — проверка на PostgreSQL пропущена")
        database = await open_db(PG_URL, recreate=True)
    else:
        database = await open_db(sqlite_url(tmp_path))
    try:
        yield database
    finally:
        await database.engine.dispose()


StorageFactory = Callable[..., DbStorage]


@pytest.fixture
def storage_factory(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> StorageFactory:
    mode = getattr(request, "param", "direct")

    def make(database: Db, **kwargs: Any) -> DbStorage:
        if mode == "generic":
            # СУБД без INSERT ... ON CONFLICT и UPDATE ... RETURNING: SELECT ... FOR UPDATE + INSERT/UPDATE.
            monkeypatch.setattr(fsm_storage, "_UPSERT_INSERTS", {})
            monkeypatch.setattr(database.engine.dialect, "update_returning", False)
        kwargs.setdefault("write_behind", mode == "write_behind")
        storage = DbStorage(database.sessionmaker, **kwargs)
        assert storage.write_behind is (mode == "write_behind")
        return storage

    return make


def modes(*names: str) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    """Прогнать тест для каждого режима хранилища (одинаковый mode у фикстур db и storage_factory)."""
    return pytest.mark.parametrize(("db", "storage_factory"), [(name, name) for name in names], indirect=True)


async def stored(database: Db, storage: DbStorage) -> dict[str, tuple[str | None, dict[str, Any]]]:
    """Что лежит в базе после того, как хранилище дописало всё (запись вдогонку)."""
    await storage.flush()
    return await database.rows()


# --- Основные операции -------------------------------------------------------------------------------


@modes(*MODES)
async def test_round_trip(db: Db, storage_factory: StorageFactory) -> None:
    storage = storage_factory(db)
    data = {"title": "Отчёт за квартал", "weight": 30, "plan": 10.5, "files": [{"id": "f1", "name": "a.pdf"}],
            "editing": True, "suggestion": None}

    await storage.set_state(key(), Form.name)
    await storage.set_data(key(), data)

    assert await storage.get_state(key()) == "Form:name"
    assert await storage.get_data(key()) == data
    assert await storage.get_value(key(), "title") == "Отчёт за квартал"
    assert await stored(db, storage) == {row_key(): ("Form:name", data)}

    await storage.set_state(key(), "Form:position")  # строкой — тоже можно
    assert await storage.get_state(key()) == "Form:position"
    assert await stored(db, storage) == {row_key(): ("Form:position", data)}


@modes(*MODES)
async def test_unknown_key_is_empty_and_creates_nothing(db: Db, storage_factory: StorageFactory) -> None:
    storage = storage_factory(db)
    assert await storage.get_state(key()) is None
    assert await storage.get_data(key()) == {}
    assert await storage.update_data(key(), {}) == {}
    await FSMContext(storage, key()).clear()
    assert await stored(db, storage) == {}


@modes(*MODES)
async def test_update_data_merges(db: Db, storage_factory: StorageFactory) -> None:
    storage = storage_factory(db)
    assert await storage.update_data(key(), {"title": "Отчёт", "weight": 10}) == {"title": "Отчёт", "weight": 10}
    merged = await storage.update_data(key(), {"weight": 30, "files": ["f1"]})

    assert merged == {"title": "Отчёт", "weight": 30, "files": ["f1"]}
    assert await storage.get_data(key()) == merged
    assert await storage.get_state(key()) is None
    assert await stored(db, storage) == {row_key(): (None, merged)}

    # FSMContext.update_data(**kwargs) — как в хендлерах.
    context = FSMContext(storage, key())
    await context.set_state(Form.position)
    assert await context.update_data(prompt_id=7) == {**merged, "prompt_id": 7}
    assert await storage.get_state(key()) == "Form:position"


@modes(*MODES)
async def test_clear_deletes_row(db: Db, storage_factory: StorageFactory) -> None:
    storage = storage_factory(db)
    context = FSMContext(storage, key())
    await context.set_state(Form.name)
    await context.update_data(full_name="Иванов Иван Иванович")

    await context.set_state(None)  # состояние снято, данные ещё есть — строка остаётся
    assert await stored(db, storage) == {row_key(): (None, {"full_name": "Иванов Иван Иванович"})}

    await context.set_data({})  # ни состояния, ни данных — строки нет
    assert await stored(db, storage) == {}

    await context.set_data({"a": 1})
    await context.set_state(Form.name)
    await context.clear()
    assert await context.get_state() is None and await context.get_data() == {}
    assert await stored(db, storage) == {}

    await context.set_state(Form.name)  # состояние без данных — строка есть
    await context.set_data({})
    assert await stored(db, storage) == {row_key(): ("Form:name", {})}


@modes(*MODES)
async def test_isolation_between_users_chats_and_destinies(db: Db, storage_factory: StorageFactory) -> None:
    storage = storage_factory(db)
    await storage.set_state(key(EMP), Form.name)
    await storage.set_data(key(EMP), {"who": "emp"})
    await storage.set_state(key(EMP2), Form.position)
    await storage.set_data(key(EMP2), {"who": "emp2"})
    await storage.set_data(key(EMP, chat=-100500), {"who": "emp in group"})
    await storage.set_data(key(EMP, destiny="scene"), {"who": "emp scene"})

    assert (await storage.get_state(key(EMP)), await storage.get_data(key(EMP))) == ("Form:name", {"who": "emp"})
    assert (await storage.get_state(key(EMP2)), await storage.get_data(key(EMP2))) == (
        "Form:position",
        {"who": "emp2"},
    )
    assert await storage.get_state(key(EMP, chat=-100500)) is None
    assert await storage.get_data(key(EMP, chat=-100500)) == {"who": "emp in group"}
    assert await storage.get_data(key(EMP, destiny="scene")) == {"who": "emp scene"}

    await FSMContext(storage, key(EMP)).clear()
    assert await storage.get_data(key(EMP2)) == {"who": "emp2"}
    assert set(await stored(db, storage)) == {
        row_key(EMP2),
        row_key(EMP, chat=-100500),
        row_key(EMP, destiny="scene"),
    }


@modes(*MODES)
async def test_survives_restart(db: Db, storage_factory: StorageFactory) -> None:
    """Новый экземпляр хранилища на новом движке (перезапуск бота, деплой) видит незавершённый диалог."""
    storage = storage_factory(db)
    await storage.set_state(key(), Form.position)
    await storage.update_data(key(), {"full_name": "Иванов Иван Иванович", "q_msg_id": 5})
    await storage.set_state(key(EMP2), Form.name)
    await storage.close()  # остановка бота: Dispatcher закрывает хранилище

    db = await db.reopen()
    restarted = storage_factory(db)
    assert await restarted.get_state(key()) == "Form:position"
    assert await restarted.get_data(key()) == {"full_name": "Иванов Иван Иванович", "q_msg_id": 5}
    assert await restarted.get_state(key(EMP2)) == "Form:name"
    await restarted.close()
    await db.engine.dispose()


@modes(*MODES)
async def test_returned_data_is_a_copy(db: Db, storage_factory: StorageFactory) -> None:
    storage = storage_factory(db)
    await storage.set_data(key(), {"files": [{"id": "f1"}]})

    data = await storage.get_data(key())
    data["files"].append({"id": "hacked"})
    merged = await storage.update_data(key(), {"seq": 1})
    merged["files"].clear()

    assert await storage.get_data(key()) == {"files": [{"id": "f1"}], "seq": 1}


@modes(*MODES)
async def test_data_must_be_json(db: Db, storage_factory: StorageFactory) -> None:
    storage = storage_factory(db)
    await storage.set_data(key(), {"ok": 1})

    for bad in ({"when": datetime(2026, 10, 2, 18, 0)}, {"ids": {1, 2}}, {"score": math.nan}):
        with pytest.raises(FsmDataError, match="JSON"):
            await storage.set_data(key(), bad)
        with pytest.raises(FsmDataError, match="dt_to_state"):
            await storage.update_data(key(), bad)
    with pytest.raises(DataNotDictLikeError):
        await storage.set_data(key(), [("a", 1)])  # type: ignore[arg-type]
    assert await storage.get_data(key()) == {"ok": 1}  # неудачная запись ничего не испортила

    # Как у RedisStorage (JSON): кортеж -> список, числовой ключ -> строка.
    await storage.set_data(key(), {"pair": (1, 2), 7: "seven"})
    assert await storage.get_data(key()) == {"pair": [1, 2], "7": "seven"}
    assert await stored(db, storage) == {row_key(): (None, {"pair": [1, 2], "7": "seven"})}


@modes(*MODES)
async def test_key_builder(db: Db, storage_factory: StorageFactory) -> None:
    storage = storage_factory(db, key_builder=DefaultKeyBuilder(prefix="kpi", with_destiny=True))
    await storage.set_state(key(), Form.name)
    assert set(await stored(db, storage)) == {f"kpi:{EMP}:{EMP}:default"}


@modes(*DIRECT_MODES)
async def test_concurrent_update_data_from_two_instances(db: Db, storage_factory: StorageFactory) -> None:
    """Два экземпляра бота (деплой) дополняют данные одного ключа одновременно — ничего не теряется."""
    first, second = storage_factory(db), storage_factory(db)
    await first.set_state(key(), Form.name)

    await asyncio.gather(
        *(
            (first if index % 2 else second).update_data(key(), {f"field{index}": index})
            for index in range(10)
        )
    )

    expected = {f"field{index}": index for index in range(10)}
    assert await first.get_data(key()) == expected
    assert await second.get_state(key()) == "Form:name"


# --- Режимы для SQLite -------------------------------------------------------------------------------


async def test_auto_mode_by_database(tmp_path: Path, memory_engine: AsyncEngine) -> None:
    """SQLite в файле — запись вдогонку; база в памяти — MemoryStorage."""
    file_db = await open_db(sqlite_url(tmp_path))
    try:
        assert DbStorage(file_db.sessionmaker).write_behind is True
        assert DbStorage(file_db.sessionmaker, write_behind=False).write_behind is False
    finally:
        await file_db.engine.dispose()
    in_memory = DbStorage(make_sessionmaker(memory_engine))
    assert in_memory.write_behind is False and in_memory._memory is not None


async def test_postgres_upsert_statement() -> None:
    """Для PostgreSQL строится INSERT ... ON CONFLICT (key) DO UPDATE ... RETURNING (проверка без сервера)."""
    dialect = PGDialect_asyncpg()
    statements: list[Any] = []

    class Result:
        def one(self) -> Any:
            return type("Row", (), {"state": "Form:name", "data": {"a": 1}})()

        def first(self) -> Any:
            return self.one()

    class FakeSession:
        def get_bind(self) -> Any:
            return type("Bind", (), {"dialect": dialect})()

        async def execute(self, statement: Any) -> Result:
            statements.append(statement)
            return Result()

    storage = DbStorage(async_sessionmaker(), write_behind=False)
    session: Any = FakeSession()
    assert await storage._upsert(session, "fsm:42:1:1:default", state="Form:name") == ("Form:name", {"a": 1})
    assert await storage._update_existing(session, "fsm:42:1:1:default", state=None) == ("Form:name", {"a": 1})
    upsert, update = (str(statement.compile(dialect=dialect)) for statement in statements)
    assert "ON CONFLICT (key) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at" in upsert
    assert upsert.endswith("RETURNING fsm_state.state, fsm_state.data")
    assert update.startswith("UPDATE fsm_state SET state=") and "RETURNING fsm_state.state, fsm_state.data" in update


async def test_memory_database_does_not_touch_handler_transaction(
    memory_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """База в памяти: одно соединение на все сессии — FSM не должен коммитить/откатывать чужую транзакцию."""
    sessionmaker = memory_sessionmaker
    storage = DbStorage(sessionmaker)
    async with sessionmaker() as session:
        session.add(User(tg_id=EMP, full_name="Иванов Иван Иванович", role=Role.EMPLOYEE, status=UserStatus.PENDING))
        await session.flush()
        await storage.set_state(key(), Form.name)
        await storage.update_data(key(), {"a": 1})
        assert await storage.get_state(key()) == "Form:name"
        await session.rollback()  # хендлер упал — его изменения откатываются целиком
    async with sessionmaker() as session:
        assert await session.scalar(select(User)) is None
        assert await session.scalar(select(FsmState)) is None
    assert await storage.get_data(key()) == {"a": 1}


async def test_postgres_auto_mode_is_direct_and_independent_of_handler(pg_engine: AsyncEngine) -> None:
    """PostgreSQL: режим сам выбирается прямой; запись состояния — своя короткая транзакция на своём
    соединении: откат хендлера её не отменяет, а незакоммиченная запись хендлера её не задерживает."""
    sessionmaker = make_sessionmaker(pg_engine)
    storage = DbStorage(sessionmaker)
    assert storage.write_behind is False and storage._memory is None
    async with sessionmaker() as session:
        session.add(User(tg_id=EMP, full_name="Иванов Иван Иванович", role=Role.EMPLOYEE, status=UserStatus.PENDING))
        await session.flush()  # хендлер держит открытую транзакцию с записью
        await asyncio.wait_for(storage.set_state(key(), Form.name), 5)
        assert await asyncio.wait_for(storage.update_data(key(), {"a": 1, "text": "Юрист\x00 «А»"}), 5) == {
            "a": 1,
            "text": "Юрист\x00 «А»",
        }
        await session.rollback()  # хендлер упал — откатываются только его изменения
    async with sessionmaker() as session:
        assert await session.scalar(select(User)) is None
        row = await session.scalar(select(FsmState))
        assert row is not None and row.key == row_key() and row.state == "Form:name"
        assert row.data == {"a": 1, "text": "Юрист\x00 «А»"}  # NUL в JSON-данных PostgreSQL принимает (json)
    await storage.set_state(key(), None)
    await storage.set_data(key(), {})
    async with sessionmaker() as session:
        assert await session.scalar(select(FsmState)) is None


async def test_postgres_own_storage_pool_prevents_stall(
    pg_engine: AsyncEngine, pg_engine_factory: Callable[..., AsyncEngine]
) -> None:
    """PostgreSQL: апдейты разных людей заняли все соединения основного пула (сессия хендлера держит своё
    до конца апдейта, как после UserMiddleware) и меняют состояние диалога. Хранилище на том же пуле ждёт
    свободное соединение до pool_timeout — бот «встаёт» (так было бы на Render при наплыве нажатий);
    со своим пулом (make_storage_engine, как в bot.main.main) запись проходит сразу."""
    main_engine = pg_engine_factory(pool_timeout=1)
    capacity = main_engine.pool.size() + main_engine.pool._max_overflow  # type: ignore[attr-defined]
    main_sm = make_sessionmaker(main_engine)

    async def updates(storage: DbStorage) -> list[BaseException | None]:
        all_busy = asyncio.Barrier(capacity)

        async def one(user: int) -> None:
            async with main_sm() as session:
                await session.execute(text("SELECT 1"))  # соединение основного пула занято до конца апдейта
                await all_busy.wait()
                await storage.set_state(key(user), Form.name)
                await storage.update_data(key(user), {"user": user})
                await session.commit()

        users = [EMP + index for index in range(capacity)]
        return await asyncio.wait_for(asyncio.gather(*(one(user) for user in users), return_exceptions=True), 30)

    shared = await updates(DbStorage(main_sm))
    assert any(isinstance(result, PoolTimeoutError) for result in shared), shared

    own = DbStorage(make_sessionmaker(pg_engine_factory(make_storage_engine)))
    started = time.monotonic()
    assert await updates(own) == [None] * capacity
    assert time.monotonic() - started < 5
    for index in range(capacity):
        assert await own.get_state(key(EMP + index)) == Form.name.state
        assert await own.get_data(key(EMP + index)) == {"user": EMP + index}


async def test_write_behind_does_not_wait_for_handler_transaction(tmp_path: Path) -> None:
    """Хендлер записал пользователя (flush, транзакция открыта) и меняет состояние: SQLite разрешает одну
    пишущую транзакцию, но хранилище не ждёт busy_timeout (5 с) — состояние видно сразу, в базу оно
    попадает после коммита хендлера."""
    database = await open_db(sqlite_url(tmp_path))
    storage = DbStorage(database.sessionmaker)
    try:
        async with database.sessionmaker() as session:
            session.add(User(tg_id=EMP, full_name="", role=Role.EMPLOYEE, status=UserStatus.PENDING))
            await session.flush()
            started = time.monotonic()
            await storage.set_state(key(), Form.name)
            await storage.update_data(key(), {"q_msg_id": 3})
            assert await storage.get_state(key()) == "Form:name"
            assert await storage.get_data(key()) == {"q_msg_id": 3}
            assert time.monotonic() - started < 1
            await asyncio.sleep(0.05)
            await session.commit()
        assert await stored(database, storage) == {row_key(): ("Form:name", {"q_msg_id": 3})}
    finally:
        await storage.close()
        await database.engine.dispose()


async def test_write_behind_retries_and_skips_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = await open_db(sqlite_url(tmp_path))
    storage = DbStorage(database.sessionmaker)
    monkeypatch.setattr(fsm_storage, "_RETRY_START_SEC", 0.01)
    original = DbStorage._upsert
    failures = iter([RuntimeError("disk I/O error")])

    async def flaky(self: DbStorage, *args: Any, **kwargs: Any) -> Any:
        error = next(failures, None)
        if error is not None:
            raise error
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(DbStorage, "_upsert", flaky)
    try:
        await FSMContext(storage, key()).clear()  # и так пусто — в базу не пишем
        assert storage._flush_task is None

        with caplog.at_level(logging.WARNING, logger=fsm_storage.__name__):
            await storage.set_state(key(), Form.name)
            assert await stored(database, storage) == {row_key(): ("Form:name", {})}
        assert any("повтор" in record.getMessage() for record in caplog.records)
    finally:
        await storage.close()
        await database.engine.dispose()


async def test_write_behind_close_does_not_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """База недоступна при остановке бота — close() ждёт не дольше _CLOSE_TIMEOUT_SEC и пишет в лог."""
    database = await open_db(sqlite_url(tmp_path))
    storage = DbStorage(database.sessionmaker)
    monkeypatch.setattr(fsm_storage, "_RETRY_START_SEC", 0.01)
    monkeypatch.setattr(fsm_storage, "_CLOSE_TIMEOUT_SEC", 0.2)

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(DbStorage, "_upsert", broken)
    try:
        await storage.set_state(key(), Form.name)
        with caplog.at_level(logging.WARNING, logger=fsm_storage.__name__):
            await asyncio.wait_for(storage.close(), 2)
        assert any("перед остановкой" in record.getMessage() for record in caplog.records)
        assert storage._flush_task is not None
        with contextlib.suppress(asyncio.CancelledError):
            await storage._flush_task  # остановлена, а не крутится в фоне
        assert storage._flush_task.done()
    finally:
        await database.engine.dispose()


# --- Сценарий бота: диалог продолжается после перезапуска ----------------------------------------------------


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_registration_dialog_continues_after_restart(
    backend: str, tmp_path: Path, set_env: Callable[..., None]
) -> None:
    """Сотрудник начал анкету (/start -> «введите ФИО»), бот перезапустился (новый процесс: новый движок,
    Dispatcher и хранилище; Telegram тот же) — ответ на вопрос принимается как следующий шаг анкеты."""
    from e2e.conftest import release_bot_routers
    from e2e.fakebot import BotHarness, FakeSession

    from bot.handlers.start import RegistrationSG
    from bot.main import build_dispatcher

    if backend == "postgresql" and not PG_URL:
        pytest.skip("TEST_DATABASE_URL не задан — проверка на PostgreSQL пропущена")
    set_env(BOT_TOKEN="42:TEST", ADMIN_IDS=str(MGR))
    url = PG_URL if backend == "postgresql" else sqlite_url(tmp_path)
    telegram = FakeSession()

    async def start_bot(database: Db) -> BotHarness:
        release_bot_routers()
        dp = build_dispatcher(database.sessionmaker)
        dp.fsm.storage = DbStorage(database.sessionmaker)  # события одного человека — по очереди (SimpleEventIsolation)
        bot = Bot("42:TEST", session=telegram, default=DefaultBotProperties(parse_mode="HTML"))
        return BotHarness(dp, bot, database.sessionmaker)

    database = await open_db(url, recreate=backend == "postgresql")
    try:
        h = await start_bot(database)
        await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
        await h.send_command(EMP, "start", first_name="Иван")
        assert await h.get_state(EMP) == RegistrationSG.full_name.state
        question = h.last_message(EMP)
        assert question is not None and question.buttons  # вопрос с кнопкой «Отмена»

        await h.dp.storage.close()  # остановка: Dispatcher закрывает хранилище
        database = await database.reopen()

        h = await start_bot(database)
        assert await h.get_state(EMP) == RegistrationSG.full_name.state
        await h.send_text(EMP, "Иванов Иван Иванович")
        assert "Приятно познакомиться, Иванов Иван Иванович" in (h.last_text(EMP) or "")
        assert await h.get_state(EMP) == RegistrationSG.position.state
        assert (await h.get_data(EMP))["full_name"] == "Иванов Иван Иванович"
        # q_msg_id пережил перезапуск: у вопроса из «прошлой жизни» бота убрана клавиатура.
        assert not h.api.messages[(EMP, question.message_id)].buttons

        await h.press_button(EMP, "Пропустить")
        assert await h.get_state(EMP) is None
        user = await h.get_user(EMP)
        assert user is not None and user.full_name == "Иванов Иван Иванович" and user.status == UserStatus.PENDING
        assert await stored(database, h.dp.storage) == {}  # анкета закончена — строки диалога нет
        await h.dp.storage.close()
    finally:
        release_bot_routers()
        await telegram.close()
        await database.engine.dispose()
