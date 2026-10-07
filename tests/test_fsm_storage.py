"""DbStorage — состояния диалогов (FSM aiogram) в таблице fsm_state (bot/fsm_storage.py, SPEC §10.1).

* запись/чтение состояния и данных, update_data (слияние), очистка удаляет строку, разные собеседники,
  чаты и «destiny» не смешиваются, данные переживают перезапуск (новый экземпляр хранилища и движка);
* данные только JSON-совместимые: понятная ошибка FsmDataError, кортежи -> списки, как у RedisStorage;
* скорость: чтение — из памяти после первого обращения (один SELECT на ключ), изменения апдейта —
  одной записью в фоне через flush_delay (одно выражение, одна транзакция), без изменений — без записи,
  диалог, закончившийся до записи, в базу не пишется;
* два экземпляра бота (деплой): новый читает то, что записал старый; побеждает более новое изменение
  (updated_at), а не то, что дошло до базы последним, — и запись, и удаление; проигравший перечитывает
  значение из базы; cache_ttl перечитывает значения, изменённые извне;
* режимы: общая база (PostgreSQL; на SQLite в файле — для проверки), один процесс (SQLite в файле:
  хендлер держит незакоммиченную запись, а хранилище не ждёт busy_timeout), обобщённый UPSERT (прочие
  СУБД), SQLite в памяти — MemoryStorage (не трогает транзакцию хендлера);
* PostgreSQL: у хранилища свой пул (make_storage_engine) — апдейты, занявшие основной пул, не ждут
  друг друга при записи состояния;
* сценарий бота через e2e-harness: анкета сотрудника продолжается после «перезапуска» бота.

PostgreSQL — если задан TEST_DATABASE_URL (тестовая база: эти тесты пересоздают свою схему
kpi_test_fsm_<воркер>), иначе эти варианты пропускаются.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from sqlalchemy import event, func, select, text
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot import fsm_storage
from bot.db.base import Base, init_db, is_postgres_url, make_engine, make_sessionmaker, make_storage_engine
from bot.db.models import FsmState, Role, User, UserStatus
from bot.fsm_storage import DbStorage, FsmDataError

BOT_ID = 42
EMP, EMP2, MGR = 2001, 2002, 1001
PG_URL = os.environ.get("TEST_DATABASE_URL", "")
# Своя схема PostgreSQL: тесты хранилища пересоздают её и не трогают таблицы других тестов.
PG_SCHEMA = "kpi_test_fsm_" + "".join(ch if ch.isalnum() else "_" for ch in os.environ.get("PYTEST_XDIST_WORKER", "main"))
NO_DELAY = 60.0  # flush_delay «никогда сам»: запись только по flush()/close() — порядок задаёт тест


class Form(StatesGroup):
    name = State()
    position = State()


def key(user: int = EMP, chat: int | None = None, *, destiny: str = "default") -> StorageKey:
    return StorageKey(bot_id=BOT_ID, chat_id=chat if chat is not None else user, user_id=user, destiny=destiny)


def row_key(user: int = EMP, chat: int | None = None, *, destiny: str = "default") -> str:
    return f"fsm:{BOT_ID}:{chat if chat is not None else user}:{user}:{destiny}"


# --- Базы и хранилища -------------------------------------------------------------------------------


def make_test_engine(url: str) -> AsyncEngine:
    if is_postgres_url(url):
        return make_engine(url, connect_args={"server_settings": {"search_path": PG_SCHEMA}})
    return make_engine(url)


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

    async def versions(self) -> dict[str, datetime]:
        async with self.sessionmaker() as session:
            result = await session.execute(select(FsmState.key, FsmState.updated_at))
            return {row.key: row.updated_at for row in result}

    async def reopen(self) -> Db:
        """«Перезапуск бота»: старый движок закрыт, новый подключается к той же базе."""
        await self.engine.dispose()
        engine = make_test_engine(self.url)
        await init_db(engine)
        return Db(self.url, engine, make_sessionmaker(engine))


async def open_db(url: str, *, recreate: bool = False) -> Db:
    engine = make_test_engine(url)
    if recreate:
        async with engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{PG_SCHEMA}" CASCADE'))
                await conn.execute(text(f'CREATE SCHEMA "{PG_SCHEMA}"'))
            else:
                await conn.run_sync(Base.metadata.drop_all)
    await init_db(engine)
    return Db(url, engine, make_sessionmaker(engine))


def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'data' / 'bot.db').as_posix()}"


# Режимы хранилища: общая база (как PostgreSQL), один процесс (SQLite в файле), обобщённый UPSERT
# (прочие СУБД), настоящий PostgreSQL.
MODES = ["shared", "single", "generic", "postgresql"]
SHARED_MODES = ["shared", "generic", "postgresql"]  # базу делят несколько экземпляров бота


@pytest_asyncio.fixture
async def db(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Db]:
    mode = getattr(request, "param", "shared")
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


@pytest_asyncio.fixture
async def storage_factory(
    request: pytest.FixtureRequest, db: Db, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[StorageFactory]:
    """``storage_factory(db, **kwargs)`` — хранилище в режиме теста; после теста все закрываются
    (дописывают несохранённое) до закрытия базы."""
    mode = getattr(request, "param", "shared")
    made: list[DbStorage] = []

    def make(database: Db, **kwargs: Any) -> DbStorage:
        if mode == "generic":
            # СУБД без INSERT ... ON CONFLICT: SELECT ... FOR UPDATE + INSERT/UPDATE.
            monkeypatch.setattr(fsm_storage, "_UPSERT_INSERTS", {})
        kwargs.setdefault("write_behind", mode == "single")
        storage = DbStorage(database.sessionmaker, **kwargs)
        assert storage.write_behind is (mode == "single")
        made.append(storage)
        return storage

    try:
        yield make
    finally:
        for storage in made:
            await storage.close()


def modes(*names: str) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    """Прогнать тест для каждого режима хранилища (одинаковый mode у фикстур db и storage_factory)."""
    return pytest.mark.parametrize(("db", "storage_factory"), [(name, name) for name in names], indirect=True)


async def stored(database: Db, storage: DbStorage) -> dict[str, tuple[str | None, dict[str, Any]]]:
    """Что лежит в базе после того, как хранилище дописало всё."""
    await storage.flush()
    return await database.rows()


@dataclass
class SqlLog:
    """SQL, ушедший в базу: выражения и коммиты."""

    statements: list[str] = field(default_factory=list)
    commits: int = 0

    @property
    def reads(self) -> list[str]:
        """Чтения значения ключа (SELECT state, data ...) — не SELECT ... FOR UPDATE обобщённой записи."""
        return [s for s in self.statements if s.startswith("SELECT FSM_STATE.STATE, FSM_STATE.DATA")]

    @property
    def writes(self) -> list[str]:
        return [s for s in self.statements if s not in self.reads]


@contextlib.contextmanager
def sql_log(engine: AsyncEngine) -> Iterator[SqlLog]:
    log = SqlLog()

    def on_execute(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        log.statements.append(" ".join(statement.split()).upper())

    def on_commit(conn: Any) -> None:
        log.commits += 1

    sync_engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", on_execute)
    event.listen(sync_engine, "commit", on_commit)
    try:
        yield log
    finally:
        event.remove(sync_engine, "before_cursor_execute", on_execute)
        event.remove(sync_engine, "commit", on_commit)


def statements_per_written_key() -> int:
    """Выражений на записанный ключ: UPSERT одним выражением на все ключи, иначе SELECT + INSERT/UPDATE."""
    return 0 if fsm_storage._UPSERT_INSERTS else 2


@dataclass
class VersionClock:
    """Часы хранилища (fsm_storage.utcnow): версии изменений задаёт тест."""

    now: datetime = datetime(2026, 10, 7, 12, 0, 0)

    def advance(self, seconds: float = 1.0) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def version_clock(monkeypatch: pytest.MonkeyPatch) -> VersionClock:
    clock = VersionClock()
    monkeypatch.setattr(fsm_storage, "utcnow", lambda: clock.now)
    return clock


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
    assert await storage.get_value(key(), "missing", 5) == 5
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
async def test_concurrent_update_data_in_one_process(db: Db, storage_factory: StorageFactory) -> None:
    """Апдейты разных обработчиков одного процесса дополняют данные одного ключа одновременно —
    в том числе пока значение ещё читается из базы, — ничего не теряется."""
    storage = storage_factory(db)
    await asyncio.gather(*(storage.update_data(key(), {f"field{index}": index}) for index in range(10)))
    expected = {f"field{index}": index for index in range(10)}
    assert await storage.get_data(key()) == expected
    assert await stored(db, storage) == {row_key(): (None, expected)}


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
    assert await stored(db, storage) == {row_key(): ("Form:name", {"a": 1})}
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
    """Новый экземпляр хранилища на новом движке (перезапуск бота, деплой) видит незавершённый диалог:
    close() (остановка бота) дописывает изменения, не дожидаясь flush_delay."""
    storage = storage_factory(db, flush_delay=NO_DELAY)
    await storage.set_state(key(), Form.position)
    await storage.update_data(key(), {"full_name": "Иванов Иван Иванович", "q_msg_id": 5})
    await storage.set_state(key(EMP2), Form.name)
    started = time.monotonic()
    await storage.close()  # остановка бота: Dispatcher закрывает хранилище
    assert time.monotonic() - started < 5

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
    (await storage.get_value(key(), "files")).append({"id": "hacked too"})
    merged = await storage.update_data(key(), {"seq": 1})
    merged["files"].clear()

    assert await storage.get_data(key()) == {"files": [{"id": "f1"}], "seq": 1}
    assert await stored(db, storage) == {row_key(): (None, {"files": [{"id": "f1"}], "seq": 1})}


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


# --- Скорость: чтение из памяти, изменения апдейта — одной записью ------------------------------------


@modes(*MODES)
async def test_reads_come_from_memory_after_first_load(db: Db, storage_factory: StorageFactory) -> None:
    """После перезапуска значение ключа читается из базы один раз; дальше get_* — без обращений к базе.
    Ключ без диалога тоже запоминается: обычный апдейт вне диалога базу не трогает."""
    before_restart = storage_factory(db)
    await before_restart.set_state(key(), Form.name)
    await before_restart.set_data(key(), {"title": "Отчёт"})
    await before_restart.close()

    storage = storage_factory(db)
    with sql_log(db.engine) as log:
        for _ in range(3):
            assert await storage.get_state(key()) == "Form:name"
            assert await storage.get_data(key()) == {"title": "Отчёт"}
            assert await storage.get_value(key(), "title") == "Отчёт"
            assert await FSMContext(storage, key(EMP2)).get_state() is None
            assert await FSMContext(storage, key(EMP2)).get_data() == {}
    assert len(log.statements) == 2 and len(log.reads) == 2  # по одному SELECT на ключ
    assert log.commits == 0

    with sql_log(db.engine) as log:  # и изменения — без обращений к базе до записи
        await storage.update_data(key(), {"weight": 20})
        await storage.set_state(key(), Form.position)
        assert await storage.get_state(key()) == "Form:position"
        assert await storage.get_data(key()) == {"title": "Отчёт", "weight": 20}
    assert log.statements == []


@modes(*MODES)
async def test_changes_of_one_update_are_written_together(db: Db, storage_factory: StorageFactory) -> None:
    """Шаг мастера (дополнить данные, сменить состояние, запомнить id вопроса) и изменения другого
    человека в те же полсекунды — одна транзакция с одним выражением UPSERT."""
    storage = storage_factory(db)
    context, other = FSMContext(storage, key()), FSMContext(storage, key(EMP2))
    assert await context.get_state() is None and await other.get_state() is None  # первое чтение ключей

    with sql_log(db.engine) as log:
        await context.update_data(title="Отчёт")
        await context.set_state(Form.position)
        await context.update_data(prompt_id=7)
        await context.set_data({"title": "Отчёт за квартал", "prompt_id": 8})
        await other.set_state(Form.name)
        assert log.statements == []  # хендлер записи не ждёт
        await storage.flush()

    assert log.commits == 1
    assert len(log.writes) == (statements_per_written_key() * 2 or 1)
    assert await db.rows() == {
        row_key(): ("Form:position", {"title": "Отчёт за квартал", "prompt_id": 8}),
        row_key(EMP2): ("Form:name", {}),
    }


@modes(*MODES)
async def test_no_write_when_nothing_changed(db: Db, storage_factory: StorageFactory) -> None:
    storage = storage_factory(db)
    context = FSMContext(storage, key())
    await context.set_state(Form.name)
    await context.update_data(a=1)
    await storage.flush()
    assert await storage.get_state(key(EMP2)) is None

    with sql_log(db.engine) as log:
        await context.set_state(Form.name)
        await context.set_data({"a": 1})
        await context.update_data(a=1)
        await context.update_data({})
        await FSMContext(storage, key(EMP2)).clear()  # «очистить» пустое
        await storage.flush()
    assert log.statements == []
    assert storage._flush_task is not None and storage._flush_task.done()


@modes(*MODES)
async def test_dialog_finished_before_write_is_not_written(db: Db, storage_factory: StorageFactory) -> None:
    """Диалог начался и закончился (отмена) раньше записи — в базу не уходит ничего."""
    storage = storage_factory(db)
    assert await storage.get_state(key()) is None
    with sql_log(db.engine) as log:
        context = FSMContext(storage, key())
        await context.set_state(Form.name)
        await context.update_data(q_msg_id=3)
        await context.clear()
        await storage.flush()
    assert log.statements == []
    assert await db.rows() == {}


@modes(*MODES)
async def test_write_happens_after_flush_delay(db: Db, storage_factory: StorageFactory) -> None:
    """В базу изменения попадают сами через flush_delay после первого изменения; flush() и close() —
    сразу, не дожидаясь задержки."""
    storage = storage_factory(db, flush_delay=0.2)
    await storage.set_state(key(), Form.name)
    assert await db.rows() == {}  # ещё не записано: ждём остальные изменения апдейта
    started = time.monotonic()
    while not await db.rows() and time.monotonic() - started < 5:
        await asyncio.sleep(0.02)
    assert await db.rows() == {row_key(): ("Form:name", {})}

    lazy = storage_factory(db, flush_delay=NO_DELAY)
    await lazy.set_state(key(EMP2), Form.position)
    await asyncio.sleep(0.3)
    assert row_key(EMP2) not in await db.rows()
    await asyncio.wait_for(lazy.flush(), 5)
    assert (await db.rows())[row_key(EMP2)] == ("Form:position", {})


@modes("shared")
async def test_versions_grow_even_if_clock_stalls_or_lags(
    db: Db, storage_factory: StorageFactory, version_clock: VersionClock
) -> None:
    """Версия изменения (updated_at) строго растёт для ключа: часы не сдвинулись — +1 мкс; строку записал
    экземпляр с часами впереди — наше следующее изменение всё равно новее прочитанного."""
    storage = storage_factory(db)
    await storage.set_state(key(), Form.name)
    await storage.update_data(key(), {"a": 1})
    await storage.flush()
    assert (await db.versions())[row_key()] == version_clock.now + timedelta(microseconds=1)

    ahead = storage_factory(db)
    version_clock.advance(10)  # часы другого экземпляра впереди
    await ahead.set_state(key(), Form.position)
    await ahead.flush()
    version_clock.advance(-20)  # а у нас — позади
    lagging = storage_factory(db)
    await lagging.update_data(key(), {"b": 2})
    await lagging.flush()
    assert await db.rows() == {row_key(): ("Form:position", {"a": 1, "b": 2})}


# --- Два экземпляра бота (деплой) ------------------------------------------------------------------------


@modes(*SHARED_MODES)
async def test_new_instance_reads_what_old_one_wrote(db: Db, storage_factory: StorageFactory) -> None:
    """Деплой: старый экземпляр дописывает диалоги сам (flush_delay) и при остановке (close), новый читает
    каждый ключ из базы при первом обращении."""
    old = storage_factory(db, flush_delay=0.05)
    await old.set_state(key(), Form.name)
    await old.update_data(key(), {"full_name": "Иванов Иван Иванович"})
    await asyncio.sleep(0.3)  # запись вдогонку — без close()
    await old.set_state(key(EMP2), Form.position)
    await old.close()  # остановка старого экземпляра

    new = storage_factory(db)
    assert await new.get_state(key()) == "Form:name"
    assert await new.get_data(key()) == {"full_name": "Иванов Иван Иванович"}
    assert await new.get_state(key(EMP2)) == "Form:position"


@modes(*SHARED_MODES)
async def test_newer_change_wins_whatever_reaches_db_last(
    db: Db, storage_factory: StorageFactory, version_clock: VersionClock
) -> None:
    """Оба экземпляра меняют один ключ; запись более старого изменения доходит до базы последней — и не
    затирает более новое. Проигравший экземпляр забывает своё значение и перечитывает его из базы."""
    old = storage_factory(db, flush_delay=NO_DELAY)
    new = storage_factory(db, flush_delay=NO_DELAY)
    await old.set_state(key(), Form.name)
    await old.update_data(key(), {"step": 1})
    await old.flush()
    assert await new.get_data(key()) == {"step": 1}

    version_clock.advance()
    await old.update_data(key(), {"step": 2})  # изменение старого экземпляра...
    version_clock.advance()
    await new.update_data(key(), {"step": 3})  # ...и более новое — нового
    await new.flush()
    await old.flush()  # запоздавшая запись более старого изменения
    assert await db.rows() == {row_key(): ("Form:name", {"step": 3})}
    assert await old.get_data(key()) == {"step": 3}  # перечитано из базы

    version_clock.advance()
    await new.update_data(key(), {"step": 4})
    version_clock.advance()
    await old.update_data(key(), {"step": 5})  # более новое изменение доходит до базы последним — побеждает
    await new.flush()
    await old.flush()
    assert await db.rows() == {row_key(): ("Form:name", {"step": 5})}


@modes(*SHARED_MODES)
async def test_late_delete_does_not_remove_newer_dialog(
    db: Db, storage_factory: StorageFactory, version_clock: VersionClock
) -> None:
    old = storage_factory(db, flush_delay=NO_DELAY)
    new = storage_factory(db, flush_delay=NO_DELAY)
    await old.set_state(key(), Form.name)
    await old.flush()
    assert await new.get_state(key()) == "Form:name"

    version_clock.advance()
    await FSMContext(old, key()).clear()  # старый экземпляр закончил диалог...
    version_clock.advance()
    await new.set_state(key(), Form.position)  # ...а новый позже начал следующий шаг
    await new.flush()
    await old.flush()  # запоздавшее удаление более старого изменения
    assert await db.rows() == {row_key(): ("Form:position", {})}
    assert await old.get_state(key()) == "Form:position"

    version_clock.advance()
    await FSMContext(new, key()).clear()  # более новое удаление удаляет
    assert await stored(db, new) == {}


@modes(*SHARED_MODES)
async def test_cache_ttl_rereads_changes_made_elsewhere(db: Db, storage_factory: StorageFactory) -> None:
    """Значение, изменённое другим экземпляром (или вручную в базе), видно после cache_ttl; до этого —
    значение из памяти (компромисс, см. описание модуля)."""
    cached = storage_factory(db)
    fresh = storage_factory(db, cache_ttl=0)
    assert cached.cache_ttl == fsm_storage._CACHE_TTL_SEC
    assert await cached.get_state(key()) is None and await fresh.get_state(key()) is None

    writer = storage_factory(db)
    await writer.set_state(key(), Form.name)
    await writer.flush()
    assert await cached.get_state(key()) is None
    assert await fresh.get_state(key()) == "Form:name"

    with sql_log(db.engine) as log:  # перечитывается только сверенное с базой; изменения — из памяти
        await fresh.update_data(key(), {"a": 1})
        assert await fresh.get_data(key()) == {"a": 1}
    assert len(log.reads) == 1


@modes(*SHARED_MODES)
async def test_refresh_rereads_value_changed_by_other_instance(db: Db, storage_factory: StorageFactory) -> None:
    """Флаг «занято» в памяти нового экземпляра, а старый его уже снял (деплой): refresh перед ответом
    «подождите» перечитывает ключ из базы — один SELECT; значение, изменённое здесь и ещё не записанное,
    новее базы — остаётся, без обращения к базе."""
    old = storage_factory(db, flush_delay=NO_DELAY)
    new = storage_factory(db, flush_delay=NO_DELAY)
    await old.set_state(key(), Form.name)
    await old.update_data(key(), {"ai_busy": True})
    await old.flush()
    assert await new.get_data(key()) == {"ai_busy": True}
    await old.update_data(key(), {"ai_busy": False, "suggestion": "готово"})
    await old.flush()
    assert await new.get_data(key()) == {"ai_busy": True}  # из памяти — до cache_ttl

    with sql_log(db.engine) as log:
        await fsm_storage.refresh_state(FSMContext(new, key()))
        assert await new.get_data(key()) == {"ai_busy": False, "suggestion": "готово"}
        assert await new.get_state(key()) == Form.name.state
    assert len(log.reads) == 1 and not log.writes

    await new.update_data(key(), {"step": 2})  # изменено здесь и ещё не записано
    with sql_log(db.engine) as log:
        await new.refresh(key())
    assert log.statements == []
    assert await new.get_data(key()) == {"ai_busy": False, "suggestion": "готово", "step": 2}


@modes("single")
async def test_refresh_does_nothing_for_single_process(db: Db, storage_factory: StorageFactory) -> None:
    """SQLite в файле (один процесс): память и есть «истина» — сверять не с чем; MemoryStorage — тоже."""
    storage = storage_factory(db)
    await storage.set_state(key(), Form.name)
    await storage.flush()
    with sql_log(db.engine) as log:
        await fsm_storage.refresh_state(FSMContext(storage, key()))
        await fsm_storage.refresh_state(FSMContext(MemoryStorage(), key()))
    assert log.statements == []
    assert await storage.get_state(key()) == Form.name.state


@modes("shared")
async def test_refresh_failure_keeps_value_from_memory(
    db: Db, storage_factory: StorageFactory, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """База не ответила на сверку — хендлер отвечает по значению из памяти, как без сверки (не падает)."""
    storage = storage_factory(db)
    await storage.update_data(key(), {"ai_busy": True})
    await storage.flush()

    async def broken(_row_key: str) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(storage, "_read", broken)
    with caplog.at_level(logging.WARNING, logger=fsm_storage.__name__):
        await fsm_storage.refresh_state(FSMContext(storage, key()))
    assert "перечитать диалог" in caplog.text
    assert await storage.get_data(key()) == {"ai_busy": True}


# --- Конкурентность внутри процесса ----------------------------------------------------------------------


@modes("shared")
async def test_stale_read_does_not_overwrite_a_change(
    db: Db, storage_factory: StorageFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Два первых обращения к ключу читают базу одновременно (апдейт человека и сброс его диалога
    руководителем); второе успело изменить значение — запоздавший результат первого чтения его не затрёт."""
    storage = storage_factory(db)
    gates: list[asyncio.Event] = []
    original = storage._read

    async def gated_read(row_key: str) -> Any:
        gate = asyncio.Event()
        gates.append(gate)
        loaded = await original(row_key)
        await gate.wait()
        return loaded

    monkeypatch.setattr(storage, "_read", gated_read)
    reader = asyncio.create_task(storage.get_state(key()))
    writer = asyncio.create_task(storage.set_state(key(), Form.name))
    async with asyncio.timeout(5):
        while len(gates) < 2:
            await asyncio.sleep(0.01)
    gates[1].set()
    await writer
    gates[0].set()
    assert await reader == "Form:name"
    assert await storage.get_state(key()) == "Form:name"
    assert await stored(db, storage) == {row_key(): ("Form:name", {})}


@modes("shared")
async def test_value_being_written_is_not_reread(
    db: Db, storage_factory: StorageFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пока значение пишется, в базе ещё старое — читать его оттуда нельзя (даже с cache_ttl=0)."""
    storage = storage_factory(db, cache_ttl=0)
    gate, entered = asyncio.Event(), asyncio.Event()
    original = storage._write

    async def slow_write(batch: Any) -> Any:
        entered.set()
        await gate.wait()
        return await original(batch)

    monkeypatch.setattr(storage, "_write", slow_write)
    await storage.set_state(key(), Form.name)
    flushing = asyncio.create_task(storage.flush())
    await asyncio.wait_for(entered.wait(), 5)
    with sql_log(db.engine) as log:
        assert await storage.get_state(key()) == "Form:name"
    assert log.statements == []
    gate.set()
    await flushing
    with sql_log(db.engine) as log:
        assert await storage.get_state(key()) == "Form:name"  # записано и сверено — cache_ttl=0 перечитывает
    assert len(log.reads) == 1


# --- Режимы -------------------------------------------------------------------------------------------


async def test_auto_mode_by_database(tmp_path: Path, memory_engine: AsyncEngine) -> None:
    """SQLite в файле — один процесс (кэш не устаревает); база в памяти — MemoryStorage; иначе — общая база."""
    file_db = await open_db(sqlite_url(tmp_path))
    try:
        single = DbStorage(file_db.sessionmaker)
        assert single.write_behind is True and single.cache_ttl is None
        assert single.flush_delay == fsm_storage._FLUSH_DELAY_SEC <= 1
        shared = DbStorage(file_db.sessionmaker, write_behind=False)
        assert shared.write_behind is False and shared.cache_ttl == fsm_storage._CACHE_TTL_SEC
        assert DbStorage(file_db.sessionmaker, write_behind=False, cache_ttl=None).cache_ttl is None
    finally:
        await file_db.engine.dispose()
    in_memory = DbStorage(make_sessionmaker(memory_engine))
    assert in_memory.write_behind is False and in_memory._memory is not None


async def test_postgres_statements() -> None:
    """Для PostgreSQL: INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE (не новее) ... RETURNING и
    DELETE ... WHERE updated_at <= версия (проверка без сервера)."""
    dialect = PGDialect_asyncpg()
    statements: list[Any] = []
    written_key = "fsm:42:1:1:default"

    class Result:
        rowcount = 1

        def scalars(self) -> Iterator[str]:
            return iter([written_key])

    class FakeSession:
        def get_bind(self) -> Any:
            return type("Bind", (), {"dialect": dialect})()

        async def execute(self, statement: Any) -> Result:
            statements.append(statement)
            return Result()

    storage = DbStorage(async_sessionmaker(), write_behind=False)
    session: Any = FakeSession()
    entry = fsm_storage._Entry("Form:name", {"a": 1}, datetime(2026, 10, 7, 12, 0), True, 0.0)
    other = entry._replace(state=None)
    assert await storage._upsert(session, {written_key: entry, "fsm:42:2:2:default": other}) == {
        written_key: True,
        "fsm:42:2:2:default": None,  # не вернулась — в базе более новое значение
    }
    assert await storage._delete(session, written_key, entry) is False
    upsert, delete = (str(statement.compile(dialect=dialect)) for statement in statements)
    assert upsert.count("::VARCHAR, ") == 4  # обе строки — одним выражением
    assert (
        "ON CONFLICT (key) DO UPDATE SET state = excluded.state, data = excluded.data, "
        "updated_at = excluded.updated_at WHERE fsm_state.updated_at <= excluded.updated_at"
    ) in upsert
    assert upsert.endswith("RETURNING fsm_state.key")
    assert delete.startswith("DELETE FROM fsm_state WHERE fsm_state.key = ")
    assert "fsm_state.updated_at <= " in delete


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


async def test_postgres_storage_is_independent_of_handler_transaction(pg_engine: AsyncEngine) -> None:
    """PostgreSQL: режим сам выбирается «общая база»; запись состояния — своя короткая транзакция на своём
    соединении: откат хендлера её не отменяет, а незакоммиченная запись хендлера её не задерживает."""
    sessionmaker = make_sessionmaker(pg_engine)
    storage = DbStorage(sessionmaker)
    assert storage.write_behind is False and storage._memory is None
    assert storage.cache_ttl == fsm_storage._CACHE_TTL_SEC
    try:
        async with sessionmaker() as session:
            session.add(
                User(tg_id=EMP, full_name="Иванов Иван Иванович", role=Role.EMPLOYEE, status=UserStatus.PENDING)
            )
            await session.flush()  # хендлер держит открытую транзакцию с записью
            await asyncio.wait_for(storage.set_state(key(), Form.name), 5)
            assert await asyncio.wait_for(storage.update_data(key(), {"a": 1, "text": "Юрист\x00 «А»"}), 5) == {
                "a": 1,
                "text": "Юрист\x00 «А»",
            }
            await asyncio.wait_for(storage.flush(), 5)
            await session.rollback()  # хендлер упал — откатываются только его изменения
        async with sessionmaker() as session:
            assert await session.scalar(select(User)) is None
            row = await session.scalar(select(FsmState))
            assert row is not None and row.key == row_key() and row.state == "Form:name"
            assert row.data == {"a": 1, "text": "Юрист\x00 «А»"}  # NUL в JSON-данных PostgreSQL принимает (json)
        await storage.set_state(key(), None)
        await storage.set_data(key(), {})
        await storage.flush()
        async with sessionmaker() as session:
            assert await session.scalar(select(FsmState)) is None
    finally:
        await storage.close()


async def test_postgres_own_storage_pool_prevents_stall(
    pg_engine: AsyncEngine, pg_engine_factory: Callable[..., AsyncEngine]
) -> None:
    """PostgreSQL: апдейты разных людей заняли все соединения основного пула (сессия хендлера держит своё
    до конца апдейта, как после UserMiddleware) и впервые обращаются к своим диалогам. Хранилище на том же
    пуле ждёт свободное соединение до pool_timeout — бот «встаёт» (так было бы на Render при наплыве
    нажатий); со своим пулом (make_storage_engine, как в bot.main.main) всё проходит сразу."""
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

    shared = DbStorage(main_sm)
    own = DbStorage(make_sessionmaker(pg_engine_factory(make_storage_engine)))
    try:
        assert any(isinstance(result, PoolTimeoutError) for result in await updates(shared))

        started = time.monotonic()
        assert await updates(own) == [None] * capacity
        assert time.monotonic() - started < 5
        for index in range(capacity):
            assert await own.get_state(key(EMP + index)) == Form.name.state
            assert await own.get_data(key(EMP + index)) == {"user": EMP + index}
        await asyncio.wait_for(own.flush(), 10)
        async with main_sm() as session:
            assert await session.scalar(select(func.count()).select_from(FsmState)) == capacity
    finally:
        await shared.close()
        await own.close()


async def _kill_backends(admin: AsyncEngine, engine: AsyncEngine) -> None:
    """Оборвать серверные соединения пула ``engine`` (перезапуск пулера / базы) и дождаться, пока их не станет."""
    async with engine.connect() as conn:
        pid = int(await conn.scalar(text("SELECT pg_backend_pid()")))
    async with admin.connect() as conn:
        assert await conn.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        for _ in range(100):
            if await conn.scalar(text("SELECT count(*) = 0 FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}):
                return
            await asyncio.sleep(0.05)
    raise AssertionError("серверное соединение не завершилось")


async def test_postgres_dialog_read_retries_on_dropped_connection(
    pg_engine: AsyncEngine, pg_engine_factory: Callable[..., AsyncEngine], caplog: pytest.LogCaptureFixture
) -> None:
    """Соединение пула хранилища оборвалось, пока лежало в пуле (перезапуск пулера или базы), меньше чем
    через PG_PING_IDLE_SEC после использования — без проверки. Чтение диалога — первый запрос апдейта
    к базе (FSMContextMiddleware aiogram идёт раньше UserMiddleware), поэтому, как load_user, повторяется
    один раз на новом соединении, а не превращается в «⚠️ Произошла ошибка»."""
    storage_engine = pg_engine_factory(make_storage_engine)
    sessionmaker = make_sessionmaker(storage_engine)
    old = DbStorage(sessionmaker, flush_delay=NO_DELAY)
    new = DbStorage(sessionmaker, flush_delay=NO_DELAY)  # ключа нет в кэше: как новый собеседник или через cache_ttl
    try:
        await old.set_state(key(), Form.name)
        await old.update_data(key(), {"step": 1})
        await old.flush()
        await _kill_backends(pg_engine, storage_engine)  # соединение только что использовано — без проверки
        with caplog.at_level(logging.INFO, logger=fsm_storage.__name__):
            assert await asyncio.wait_for(new.get_state(key()), 10) == Form.name.state
        assert "повторяю" in caplog.text
        assert await new.get_data(key()) == {"step": 1}
        assert await new.get_state(key(EMP2)) is None
    finally:
        await old.close()
        await new.close()


async def test_single_process_does_not_wait_for_handler_transaction(tmp_path: Path) -> None:
    """SQLite в файле: хендлер записал пользователя (flush, транзакция открыта) и меняет состояние. SQLite
    разрешает одну пишущую транзакцию, но хранилище не ждёт busy_timeout (5 с) — состояние видно сразу,
    в базу оно попадает после коммита хендлера."""
    database = await open_db(sqlite_url(tmp_path))
    storage = DbStorage(database.sessionmaker, flush_delay=0)
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


async def test_write_retries_and_skips_noop(
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
            assert await storage.get_state(key()) == "Form:name"  # бот работает, пока база недоступна
            assert await stored(database, storage) == {row_key(): ("Form:name", {})}
        assert any("повтор" in record.getMessage() for record in caplog.records)
    finally:
        await storage.close()
        await database.engine.dispose()


async def test_close_does_not_hang(
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


# --- Сценарий бота: деплой — два экземпляра обрабатывают один диалог ------------------------------------------


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_deploy_overlap_new_instance_rechecks_ai_busy(
    backend: str, tmp_path: Path, set_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обновление на хостинге: старый экземпляр (A) ждёт ответа AI для черновика руководителя, а следующий
    апдейт руководителя уже приходит на новый (B). B читает диалог из базы — там ai_busy — и честно отвечает
    «подождите». A получает ответ AI, показывает его с кнопками, снимает флаг и останавливается. «✅ Принять»
    на B: в памяти B флаг ещё стоит, но перед ответом «подождите» B сверяется с базой — принят вариант AI
    (раньше B отвечал «подождите» до cache_ttl, а через ~1,5 мин затирал ответ AI вариантом по правилам)."""
    from e2e.conftest import E2E_ENV, release_bot_routers
    from e2e.fakebot import BotHarness, FakeSession

    from bot.ai import formulate, provider
    from bot.handlers import task_create
    from bot.main import build_dispatcher
    from bot.ui.texts import BTN_NEW_TASK

    if backend == "postgresql" and not PG_URL:
        pytest.skip("TEST_DATABASE_URL не задан — проверка на PostgreSQL пропущена")
    set_env(**E2E_ENV)
    ai_result = "Проверить 100 договоров поставщиков и представить отчёт в Excel"
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_ai(**_: Any) -> tuple[dict[str, Any], str]:
        started.set()
        await release.wait()
        answer = {"expected_result": ai_result, "plan_value": 100, "plan_unit": "договоров", "note": None}
        return answer, "gemini-test"

    for module in (provider, formulate):
        monkeypatch.setattr(module, "ai_available", lambda: True)
        monkeypatch.setattr(module, "generate_json", slow_ai)

    url = PG_URL if backend == "postgresql" else sqlite_url(tmp_path)
    database = await open_db(url, recreate=backend == "postgresql")
    telegram = FakeSession()
    # Общая база двух экземпляров (как PostgreSQL); пишут в базу только по flush()/close() — порядок задаёт тест.
    old = DbStorage(database.sessionmaker, write_behind=False, flush_delay=NO_DELAY)
    new = DbStorage(database.sessionmaker, write_behind=False, flush_delay=NO_DELAY)
    release_bot_routers()
    dp = build_dispatcher(database.sessionmaker, old)
    bot = Bot("42:TEST", session=telegram, default=DefaultBotProperties(parse_mode="HTML"))
    h = BotHarness(dp, bot, database.sessionmaker)
    try:
        await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
        await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
        await h.send_command(MGR, "start")
        await h.press_menu(MGR, BTN_NEW_TASK)
        await h.press_button(MGR, "Иванов")
        await h.send_text(MGR, "Провести анализ договоров")
        pending = asyncio.create_task(h.send_text(MGR, "проверить 100 договоров и представить отчёт"))
        await asyncio.wait_for(started.wait(), 10)  # A ждёт ответа AI
        await old.flush()  # ai_busy в базе (A пишет через flush_delay)

        # Render переключил приём на B: свои хранилище диалогов и очередь событий.
        dp.fsm.storage, dp.fsm.events_isolation = new, SimpleEventIsolation()
        await h.send_text(MGR, "ну что там?")
        assert h.last_text(MGR) == task_create.AI_BUSY_TEXT

        release.set()
        await asyncio.wait_for(pending, 10)  # A показал вариант AI с кнопками и снял ai_busy
        await old.close()  # остановка A: несохранённое — в базу
        assert ai_result in h.last_text(MGR) and "✅ Принять" in h.buttons(MGR)
        assert (await h.get_data(MGR))["ai_busy"] is True  # в памяти B — ещё «занято»

        await h.press_button(MGR, "Принять")
        assert "шаг 4 из 6" in h.last_text(MGR)
        data = await h.get_data(MGR)
        assert data["expected_result"] == ai_result and data["plan_value"] == 100
    finally:
        release.set()
        await new.close()
        release_bot_routers()
        await telegram.close()
        await database.engine.dispose()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_deploy_overlap_new_instance_rechecks_sending(
    backend: str, tmp_path: Path, set_env: Callable[..., None]
) -> None:
    """Деплой: старый экземпляр (A) начал отправку сдачи (диалог «отправляется» уже в базе), а нажатие
    «📤 Отправить» пришло на новый (B) — «⏳ уже отправляется». Отправка на A не удалась (сбой базы), сводка
    снова рабочая. Следующее нажатие на B сверяет «отправляется» с базой и отправляет результат (раньше B
    отвечал «уже отправляется» до cache_ttl)."""
    from e2e.conftest import E2E_ENV, release_bot_routers
    from e2e.fakebot import BotHarness, FakeSession

    from bot.db.models import Priority, Submission, Task, TaskSource, TaskStatus
    from bot.handlers.task_submit import SubmitSG
    from bot.main import build_dispatcher
    from bot.ui.texts import BTN_SUBMIT
    from bot.utils.dates import utcnow

    if backend == "postgresql" and not PG_URL:
        pytest.skip("TEST_DATABASE_URL не задан — проверка на PostgreSQL пропущена")
    set_env(**E2E_ENV)
    url = PG_URL if backend == "postgresql" else sqlite_url(tmp_path)
    database = await open_db(url, recreate=backend == "postgresql")
    telegram = FakeSession()
    old = DbStorage(database.sessionmaker, write_behind=False, flush_delay=NO_DELAY)
    new = DbStorage(database.sessionmaker, write_behind=False, flush_delay=NO_DELAY)
    release_bot_routers()
    dp = build_dispatcher(database.sessionmaker, old)
    bot = Bot("42:TEST", session=telegram, default=DefaultBotProperties(parse_mode="HTML"))
    h = BotHarness(dp, bot, database.sessionmaker)
    try:
        mgr = await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
        emp = await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
        async with h.db() as session:
            task = Task(
                title="Анализ договоров", expected_result="Проверить 100 договоров", plan_value=100.0,
                plan_unit="договоров", deadline=utcnow() + timedelta(days=3), priority=Priority.MEDIUM, weight=20,
                status=TaskStatus.ACTIVE, source=TaskSource.MANAGER, assignee_id=emp.id, created_by_id=mgr.id,
                manager_id=mgr.id,
            )
            session.add(task)
            await session.commit()
            task_id = task.id
        await h.send_command(EMP, "start")
        await h.press_menu(EMP, BTN_SUBMIT)
        await h.press_button(EMP, "Анализ договоров")
        await h.send_text(EMP, "Проверено 110 договоров")
        await h.send_text(EMP, "Отчёт с нарушениями")
        if "Фактическое значение" in (h.last_text(EMP) or ""):
            await h.send_text(EMP, "110")
        await h.press_button(EMP, "Без файлов")
        assert "Проверьте перед отправкой" in (h.last_text(EMP) or "")
        on_old = FSMContext(old, key(EMP))
        await on_old.set_state(SubmitSG.sending)  # A: «📤 Отправить» нажато, сдача сохраняется
        await old.flush()

        dp.fsm.storage, dp.fsm.events_isolation = new, SimpleEventIsolation()  # приём переключён на B
        log = await h.press_button(EMP, "Отправить")
        assert [answer.text for answer in log.answers] == ["⏳ Результат уже отправляется…"]

        await on_old.set_state(SubmitSG.confirm)  # на A сохранить не удалось — сводка снова рабочая
        await old.close()
        log = await h.press_button(EMP, "Отправить")
        assert "📤 Отправлено" in [answer.text for answer in log.answers]
        assert "Результат отправлен руководителю" in (h.last_text(EMP) or "")
        async with h.db() as session:
            assert len(list(await session.scalars(select(Submission).where(Submission.task_id == task_id)))) == 1
        assert await h.get_state(EMP) is None
    finally:
        await new.close()
        release_bot_routers()
        await telegram.close()
        await database.engine.dispose()
