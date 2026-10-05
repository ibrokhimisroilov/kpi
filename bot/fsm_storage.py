"""Хранилище незавершённых диалогов (FSM aiogram) в базе данных бота.

``MemoryStorage`` держит диалоги в памяти процесса: после перезапуска бота человек, который был
на середине анкеты или создания задачи, начинал бы сначала. На бесплатном веб-хостинге (Render)
бот засыпает без запросов, перезапускается при каждом обновлении, и при обновлении старый и новый
экземпляры какое-то время работают одновременно. ``DbStorage`` хранит состояние в таблице
``fsm_state`` (``bot.db.models.FsmState``) той же базы, что и задачи, — SQLite или PostgreSQL.

* Одна строка на собеседника: ключ — ``DefaultKeyBuilder(with_bot_id=True, with_destiny=True)``,
  например ``fsm:42:2001:2001:default``. Строка без состояния и без данных удаляется — таблица
  не растёт от закончившихся диалогов.
* Данные должны быть JSON-совместимыми (строки, числа, True/False, None, списки, словари) — как
  у RedisStorage aiogram: кортежи становятся списками, остальное — ``FsmDataError`` с понятным
  текстом. Даты хендлеры кладут строкой (``handlers.common.dt_to_state``).

Два режима записи (выбираются сами по базе, ``write_behind=`` — явно):

* **PostgreSQL** (и прочие СУБД) — каждая операция идёт прямо в базу отдельной короткой транзакцией.
  Запись начинается с UPSERT/UPDATE, который блокирует строку до конца транзакции, поэтому
  ``update_data`` (прочитать, дополнить, записать) не теряет изменений, даже если тот же ключ
  одновременно меняет второй экземпляр бота. Строки блокируются только в ``fsm_state`` — сессия
  хендлера им не мешает.
* **SQLite в файле** — «запись вдогонку». SQLite разрешает одну пишущую транзакцию на всю базу,
  а хендлер часто меняет состояние, пока его собственная сессия держит незакоммиченную запись
  (``session.flush()``, затем ``state.set_state(...)``): прямая запись ждала бы этот же хендлер
  и падала через busy_timeout («database is locked»). Поэтому состояние сразу меняется в памяти
  (чтение видит его немедленно), а в базу пишется фоновой задачей, как только база свободна;
  при ошибке — повтор с паузой. ``close()`` (остановка бота) дожидается записи. SQLite в файле —
  это один процесс бота на одном компьютере, так что память процесса и есть «истина».
* **База в памяти** (``sqlite://``, ``:memory:`` — тесты): у всех сессий одно соединение, и отдельная
  транзакция хранилища закоммитила бы или откатила незавершённые изменения хендлера. Такая база
  и так не переживает перезапуск, поэтому диалоги хранятся в памяти (``MemoryStorage``).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import Mapping
from typing import Any

from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import (
    BaseStorage,
    DefaultKeyBuilder,
    KeyBuilder,
    StateType,
    StorageKey,
)
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import Table, delete, insert, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import FsmState, utcnow

__all__ = ["DbStorage", "FsmDataError"]

log = logging.getLogger(__name__)

_TABLE: Table = FsmState.__table__  # type: ignore[assignment]

# INSERT ... ON CONFLICT DO UPDATE ... RETURNING — одинаково в SQLite (3.35+) и PostgreSQL.
_UPSERT_INSERTS = {"sqlite": sqlite.insert, "postgresql": postgresql.insert}

_RETRY_START_SEC = 0.5   # запись вдогонку не удалась — первая пауза перед повтором
_RETRY_MAX_SEC = 30.0    # максимальная пауза
_CLOSE_TIMEOUT_SEC = 15.0  # сколько ждать записи несохранённых состояний при остановке бота

Row = tuple[str | None, dict[str, Any]]  # (state, data)


class FsmDataError(TypeError):
    """Данные диалога нельзя сохранить в базе: значение не JSON-совместимое."""


def _json_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    """Новая копия данных диалога в том виде, в каком она вернётся из базы (через JSON)."""
    if not isinstance(data, Mapping):
        raise DataNotDictLikeError(f"Data must be a dict or dict-like object, got {type(data).__name__}")
    try:
        text = json.dumps(dict(data), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FsmDataError(
            "Данные диалога (FSM) хранятся в базе как JSON: допустимы строки, числа, True/False, None, "
            f"списки и словари (дату кладите строкой — handlers.common.dt_to_state). Ошибка: {exc}"
        ) from exc
    loaded: dict[str, Any] = json.loads(text)
    return loaded


def _state_name(state: StateType) -> str | None:
    return state.state if isinstance(state, State) else state


def _database_url(sessionmaker: async_sessionmaker[AsyncSession]) -> URL | None:
    url = getattr(sessionmaker.kw.get("bind"), "url", None)
    return make_url(url) if url is not None else None


def _is_sqlite(url: URL | None) -> bool:
    return url is not None and url.get_backend_name() == "sqlite"


def _is_memory_sqlite(url: URL | None) -> bool:
    """SQLite в памяти: одно соединение на все сессии, данные живут до перезапуска."""
    if url is None or not _is_sqlite(url):
        return False
    return (url.database or "") in ("", ":memory:") or url.query.get("mode") == "memory"


class DbStorage(BaseStorage):
    """FSM-хранилище aiogram в таблице ``fsm_state``: ``Dispatcher(storage=DbStorage(sessionmaker))``.

    :param sessionmaker: фабрика сессий бота (``bot.db.base.make_sessionmaker``); таблица создаётся
        ``init_db`` вместе с остальными.
    :param key_builder: как строить ключ строки; по умолчанию с id бота и «destiny» (сцены aiogram).
    :param write_behind: запись вдогонку (см. описание модуля); None — сама: да для SQLite в файле.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        key_builder: KeyBuilder | None = None,
        *,
        write_behind: bool | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.key_builder = key_builder or DefaultKeyBuilder(with_bot_id=True, with_destiny=True)
        url = _database_url(sessionmaker)
        self._memory: MemoryStorage | None = MemoryStorage() if _is_memory_sqlite(url) else None
        if write_behind is None:
            write_behind = _is_sqlite(url)
        self.write_behind = bool(write_behind) and self._memory is None
        # Запись вдогонку: актуальные значения (ключ -> (state, data)) и ещё не записанные ключи.
        self._cache: dict[str, Row] = {}
        self._dirty: set[str] = set()
        self._flush_task: asyncio.Task[None] | None = None

    # --- Интерфейс BaseStorage ----------------------------------------------------------------

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = _state_name(state)
        if self._memory is not None:
            await self._memory.set_state(key, value)
            return
        row_key = self._key(key)
        if self.write_behind:
            _old_state, data = await self._cached(row_key)
            self._put(row_key, value, data)
            return
        async with self.sessionmaker.begin() as session:
            if value is not None:
                await self._upsert(session, row_key, state=value)
                return
            row = await self._update_existing(session, row_key, state=None)
            if row is not None and not row[1]:
                await self._delete(session, row_key)

    async def get_state(self, key: StorageKey) -> str | None:
        if self._memory is not None:
            return await self._memory.get_state(key)
        row_key = self._key(key)
        if self.write_behind:
            return (await self._cached(row_key))[0]
        return (await self._read(row_key))[0]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        value = _json_dict(data)
        if self._memory is not None:
            await self._memory.set_data(key, value)
            return
        row_key = self._key(key)
        if self.write_behind:
            state, _old_data = await self._cached(row_key)
            self._put(row_key, state, value)
            return
        async with self.sessionmaker.begin() as session:
            if value:
                await self._upsert(session, row_key, data=value)
                return
            row = await self._update_existing(session, row_key, data={})
            if row is not None and row[0] is None:
                await self._delete(session, row_key)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        if self._memory is not None:
            return await self._memory.get_data(key)
        row_key = self._key(key)
        if self.write_behind:
            return copy.deepcopy((await self._cached(row_key))[1])
        return (await self._read(row_key))[1]

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        """Дополнить данные (как ``dict.update``) за одну операцию; вернуть новые данные."""
        partial = _json_dict(data)
        if self._memory is not None:
            return await self._memory.update_data(key, partial)
        row_key = self._key(key)
        if self.write_behind:
            state, current = await self._cached(row_key)
            merged = {**current, **partial}
            self._put(row_key, state, merged)
            return copy.deepcopy(merged)
        if not partial:
            return await self.get_data(key)
        async with self.sessionmaker.begin() as session:
            # Сначала «трогаем» строку (создаём, если её нет): она блокируется до конца транзакции,
            # и параллельная запись того же ключа дождётся нас, а не перезапишет слияние.
            _state, current = await self._upsert(session, row_key)
            merged = {**current, **partial}
            await session.execute(
                update(_TABLE).where(_TABLE.c.key == row_key).values(data=merged, updated_at=utcnow())
            )
        return merged

    async def close(self) -> None:
        """Остановка бота: дождаться записи несохранённых состояний (не дольше _CLOSE_TIMEOUT_SEC).
        Соединения с базой принадлежат боту (engine закрывает main)."""
        if self._memory is not None:
            await self._memory.close()
            return
        try:
            await asyncio.wait_for(self.flush(), _CLOSE_TIMEOUT_SEC)
        except TimeoutError:
            log.warning("Не все незавершённые диалоги записаны в базу перед остановкой: %s", len(self._dirty))
            if self._flush_task is not None:
                self._flush_task.cancel()

    async def flush(self) -> None:
        """Дождаться, пока изменения записи вдогонку окажутся в базе (в прямом режиме — сразу)."""
        while self._flush_task is not None and not self._flush_task.done():
            await asyncio.shield(self._flush_task)

    # --- Запись вдогонку (SQLite в файле) -------------------------------------------------------

    async def _cached(self, row_key: str) -> Row:
        """Текущие (state, data) ключа: из памяти, а при первом обращении — из базы."""
        row = self._cache.get(row_key)
        if row is None:
            loaded = await self._read(row_key)
            # Пока читали, ключ мог измениться (сброс диалога руководителем) — свежее значение важнее.
            row = self._cache.setdefault(row_key, loaded)
        return row

    def _put(self, row_key: str, state: str | None, data: dict[str, Any]) -> None:
        if self._cache.get(row_key) == (state, data):
            return  # ничего не изменилось (например, «очистить» и так пустое) — в базу не пишем
        self._cache[row_key] = (state, data)
        self._dirty.add(row_key)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.get_running_loop().create_task(self._flush_loop(), name="fsm-db-flush")

    async def _flush_loop(self) -> None:
        delay = _RETRY_START_SEC
        while self._dirty:
            batch = {row_key: self._cache[row_key] for row_key in self._dirty}
            self._dirty.clear()
            try:
                # База занята записью хендлера — транзакция подождёт (busy_timeout), бот не стоит.
                async with self.sessionmaker.begin() as session:
                    for row_key, (state, data) in batch.items():
                        if state is None and not data:
                            await self._delete(session, row_key)
                        else:
                            await self._upsert(session, row_key, state=state, data=data)
            except Exception as exc:  # noqa: BLE001 — в памяти значения целы, запишем позже
                self._dirty.update(batch)
                log.warning(
                    "Состояние диалогов не записано в базу (%s) — повтор через %s с", type(exc).__name__, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_SEC)
            else:
                delay = _RETRY_START_SEC

    # --- Работа с таблицей ----------------------------------------------------------------------

    def _key(self, key: StorageKey) -> str:
        return self.key_builder.build(key)

    async def _read(self, row_key: str) -> Row:
        async with self.sessionmaker() as session:
            row = (
                await session.execute(select(_TABLE.c.state, _TABLE.c.data).where(_TABLE.c.key == row_key))
            ).first()
        if row is None:
            return None, {}
        return row.state, dict(row.data or {})

    async def _upsert(self, session: AsyncSession, row_key: str, **values: Any) -> Row:
        """Создать строку или обновить в ней только переданные поля (``state``/``data``) и вернуть
        итоговые (state, data). До конца транзакции строка заблокирована для других записей."""
        now = utcnow()
        row_values: dict[str, Any] = {"key": row_key, "state": None, "data": {}, "updated_at": now, **values}
        make_insert = _UPSERT_INSERTS.get(session.get_bind().dialect.name)
        if make_insert is None:
            return await self._upsert_generic(session, row_key, row_values, values)
        stmt = make_insert(_TABLE).values(row_values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[_TABLE.c.key],
            set_={name: stmt.excluded[name] for name in (*values, "updated_at")},
        ).returning(_TABLE.c.state, _TABLE.c.data)
        row = (await session.execute(stmt)).one()
        return row.state, dict(row.data or {})

    async def _upsert_generic(
        self, session: AsyncSession, row_key: str, row_values: dict[str, Any], values: dict[str, Any]
    ) -> Row:
        """UPSERT для прочих СУБД: SELECT ... FOR UPDATE, затем INSERT или UPDATE."""
        current = (
            await session.execute(
                select(_TABLE.c.state, _TABLE.c.data).where(_TABLE.c.key == row_key).with_for_update()
            )
        ).first()
        if current is None:
            await session.execute(insert(_TABLE).values(row_values))
            return row_values["state"], dict(row_values["data"])
        await session.execute(
            update(_TABLE).where(_TABLE.c.key == row_key).values(**values, updated_at=row_values["updated_at"])
        )
        return values.get("state", current.state), dict(values.get("data", current.data) or {})

    async def _update_existing(self, session: AsyncSession, row_key: str, **values: Any) -> Row | None:
        """Обновить поля строки, если она есть, и вернуть итоговые (state, data); строки нет — None
        (ничего не создаётся: «очистить» несуществующее состояние — без записи в базу)."""
        stmt = update(_TABLE).where(_TABLE.c.key == row_key).values(**values, updated_at=utcnow())
        if session.get_bind().dialect.update_returning:
            row = (await session.execute(stmt.returning(_TABLE.c.state, _TABLE.c.data))).first()
            return (row.state, dict(row.data or {})) if row is not None else None
        current = (
            await session.execute(
                select(_TABLE.c.state, _TABLE.c.data).where(_TABLE.c.key == row_key).with_for_update()
            )
        ).first()
        if current is None:
            return None
        await session.execute(stmt)
        return values.get("state", current.state), dict(values.get("data", current.data) or {})

    async def _delete(self, session: AsyncSession, row_key: str) -> None:
        await session.execute(delete(_TABLE).where(_TABLE.c.key == row_key))
