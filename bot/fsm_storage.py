"""Хранилище незавершённых диалогов (FSM aiogram) в базе данных бота.

``MemoryStorage`` держит диалоги в памяти процесса: после перезапуска бота человек, который был
на середине анкеты или создания задачи, начинал бы сначала. На бесплатном веб-хостинге (Render)
бот перезапускается при каждом обновлении, и при обновлении старый и новый экземпляры какое-то
время работают одновременно. ``DbStorage`` хранит состояние в таблице ``fsm_state``
(``bot.db.models.FsmState``) той же базы, что и задачи, — SQLite или PostgreSQL.

* Одна строка на собеседника: ключ — ``DefaultKeyBuilder(with_bot_id=True, with_destiny=True)``,
  например ``fsm:42:2001:2001:default``. Строка без состояния и без данных удаляется — таблица
  не растёт от закончившихся диалогов.
* Данные должны быть JSON-совместимыми (строки, числа, True/False, None, списки, словари) — как
  у RedisStorage aiogram: кортежи становятся списками, остальное — ``FsmDataError`` с понятным
  текстом. Даты хендлеры кладут строкой (``handlers.common.dt_to_state``).

Почему так (скорость)
---------------------
Один шаг диалога — это 5–12 операций хранилища: aiogram читает состояние на каждом апдейте
(FSMContextMiddleware), хендлер читает данные, дополняет их, меняет состояние, очищает. Отдельная
транзакция на каждую операцию — это выдача соединения, pre-ping, BEGIN, запрос, COMMIT: 6–7 обменов
с базой. С базой в другом регионе (150 мс на обмен) один шаг мастера стоил 7–10 с. Поэтому:

* **Кэш в памяти процесса.** Значение ключа (состояние + данные) читается из базы один раз — при
  первом обращении после запуска бота; дальше чтение — из памяти, без обменов с базой. Ключ без
  диалога тоже запоминается («диалога нет»), так что обычный апдейт вне диалога базу не трогает.
* **Запись вдогонку, одной транзакцией.** set_state / set_data / update_data / clear меняют значение
  в памяти сразу (следующее чтение видит его), а в базу его пишет фоновая задача через
  ``flush_delay`` (0,5 с) после первого изменения: все изменения апдейта — и всех, кто менял диалоги
  в эти полсекунды, — одним ``INSERT ... ON CONFLICT DO UPDATE`` (+ ``DELETE`` закончившихся
  диалогов) в одной транзакции. Пишется только итог; значение не изменилось — записи нет; диалог
  начался и закончился до записи — в базу не пишется ничего. Хендлер записи не ждёт. Ошибка записи —
  повтор с паузой (значения в памяти целы, бот работает). ``close()`` (остановка бота, её вызывает
  Dispatcher) и ``flush()`` пишут сразу и дожидаются записи (не дольше ``_CLOSE_TIMEOUT_SEC``).
* **Версия — ``updated_at``.** У каждого изменения своё время, для ключа строго растущее. В базу
  значение попадает, только если там не записано более новое
  (``ON CONFLICT ... DO UPDATE ... WHERE fsm_state.updated_at <= excluded.updated_at``,
  ``DELETE ... WHERE updated_at <= ...``): побеждает более новая запись, а не та, что дошла до базы
  последней. Проиграли (в базе более новое значение — от другого экземпляра бота) — значение
  убирается из кэша и при следующем обращении читается из базы.

Два экземпляра бота (деплой на Render) и на что мы согласились
---------------------------------------------------------------
Render переключает приём на новый экземпляр, старый дорабатывает уже полученные апдейты (до 20 с,
``web.SHUTDOWN_GRACE_SEC``) и при остановке дописывает несохранённые диалоги (``close``). Новый
экземпляр читает каждый ключ из базы при первом обращении и видит всё, что старый успел записать, —
а записывает тот через ``flush_delay`` после изменения. Не закрыт один случай: апдейты ОДНОГО
человека в одну и ту же секунду обрабатывают оба экземпляра (нажал кнопку в момент переключения,
пока старый ещё отвечал на предыдущее нажатие, — например, ждал ответа AI). Тогда новый может
прочитать прежнее значение и держать его в памяти, хотя старый его потом перезаписал: до ``cache_ttl``
(10 мин) или до первого изменения этого диалога на новом экземпляре. Последствие — ответы «невпопад»
(кнопка устарела / «не понял»), пока человек не повторит шаг из меню: запись нового экземпляра
новее, она побеждает, и экземпляры сходятся. Флаги «занято» (ждём ответа AI, результат уже
отправляется, задача уже создаётся) так застрять не могут: перед ответом «подождите» хендлер
перечитывает диалог из базы (``refresh_state`` / ``DbStorage.refresh`` — 1 обмен, только в этом
редком случае). Иначе новый экземпляр отвечал бы «подождите» минутами, а потом затёр бы готовый
ответ AI вариантом по правилам. Значение, которое не менялось и не перечитывалось ``cache_ttl``,
перечитывается из базы — так видны и изменения извне (другой экземпляр, правка таблицы вручную).
Закрыть этот случай полностью можно только чтением из базы на каждом апдейте — это 3–7 обменов
на операцию, то есть ровно то, что делало бота медленным.

Аварийная остановка без ``close()`` (SIGKILL, сбой хостинга) теряет изменения последних
``flush_delay`` секунд — человек повторит последний шаг диалога.

Режимы (выбираются сами по базе, ``write_behind=`` — явно):

* **PostgreSQL** (и любая база, которую могут делить несколько экземпляров бота) — всё описанное
  выше; у хранилища на PostgreSQL свой пул из одного соединения (``make_storage_engine``):
  его занимают только первое чтение ключа и фоновая запись.
* **SQLite в файле** (``write_behind=True``) — один процесс бота на одном компьютере: память процесса
  и есть «истина», значения в кэше не устаревают (``cache_ttl=None``). SQLite разрешает одну пишущую
  транзакцию на всю базу, а хендлер часто меняет состояние, пока его сессия держит незакоммиченную
  запись; фоновая запись при этом ждёт (busy_timeout) или повторяется позже — хендлер её не ждёт.
* **База в памяти** (``sqlite://``, ``:memory:`` — тесты): у всех сессий одно соединение, и отдельная
  транзакция хранилища закоммитила бы или откатила незавершённые изменения хендлера. Такая база
  и так не переживает перезапуск, поэтому диалоги хранятся в памяти (``MemoryStorage``).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.context import FSMContext
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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import FsmState, utcnow

__all__ = ["DbStorage", "FsmDataError", "refresh_state"]

log = logging.getLogger(__name__)

_TABLE: Table = FsmState.__table__  # type: ignore[assignment]

# INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING — одинаково в SQLite (3.35+) и PostgreSQL.
_UPSERT_INSERTS = {"sqlite": sqlite.insert, "postgresql": postgresql.insert}

_FLUSH_DELAY_SEC = 0.5     # запись в базу — через столько после первого изменения (изменения апдейта — одной записью)
_CACHE_TTL_SEC = 600.0     # общая база: значение, не сверявшееся с базой столько времени, перечитывается
_CACHE_MAX_KEYS = 10_000   # больше ключей в памяти — старые (уже записанные) забываются, при обращении читаются снова
_RETRY_START_SEC = 0.5     # запись не удалась — первая пауза перед повтором
_RETRY_MAX_SEC = 30.0      # максимальная пауза
_CLOSE_TIMEOUT_SEC = 15.0  # сколько ждать записи несохранённых состояний при остановке бота
_VERSION_STEP = timedelta(microseconds=1)  # версии изменений одного ключа строго растут
_AUTO: Any = object()      # cache_ttl по умолчанию: зависит от режима


class FsmDataError(TypeError):
    """Данные диалога нельзя сохранить в базе: значение не JSON-совместимое."""


class _Entry(NamedTuple):
    """Значение ключа в кэше и что о нём известно в базе."""

    state: str | None
    data: dict[str, Any]       # на месте не меняется: каждое изменение — новый словарь
    version: datetime | None   # время последнего изменения (наше) или updated_at прочитанной строки; None — строки нет
    in_db: bool                # в базе может быть строка ключа (закончится диалог — её нужно удалить)
    synced: float              # time.monotonic() последней сверки с базой (чтение или запись)

    @property
    def empty(self) -> bool:
        return self.state is None and not self.data


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


def _next_version(previous: datetime | None) -> datetime:
    """Время изменения: сейчас, но строго позже предыдущей версии ключа (часы могли отстать)."""
    now = utcnow()
    if previous is not None and now <= previous:
        return previous + _VERSION_STEP
    return now


async def refresh_state(state: FSMContext) -> None:
    """Перечитать диалог хендлера из базы (``DbStorage.refresh``) перед ответом «занято»; у других
    хранилищ (MemoryStorage) — ничего. База не ответила — остаётся значение из памяти (как без сверки)."""
    refresh = getattr(state.storage, "refresh", None)
    if refresh is None:
        return
    try:
        await refresh(state.key)
    except Exception as exc:  # noqa: BLE001 — сверка необязательна: ответим по значению из памяти
        log.warning("Не удалось перечитать диалог из базы (%s) — беру значение из памяти", type(exc).__name__)


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

    :param sessionmaker: фабрика сессий (``bot.db.base.make_sessionmaker``; на PostgreSQL — на движке
        ``make_storage_engine``); таблица создаётся ``init_db`` вместе с остальными.
    :param key_builder: как строить ключ строки; по умолчанию с id бота и «destiny» (сцены aiogram).
    :param write_behind: True — базу пишет только этот процесс (SQLite в файле): память процесса —
        «истина», значения в кэше не устаревают; False — базу могут делить несколько экземпляров
        бота (PostgreSQL): значения перечитываются через ``cache_ttl``. None — сама: да для SQLite
        в файле. В обоих режимах чтение — из памяти, запись — в фоне (см. описание модуля).
    :param flush_delay: через сколько секунд после первого изменения писать в базу (0 — сразу,
        как только освободится цикл событий).
    :param cache_ttl: через сколько секунд без сверки с базой значение перечитывается; None — никогда.
        По умолчанию — ``_CACHE_TTL_SEC`` для общей базы и None для ``write_behind=True``.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        key_builder: KeyBuilder | None = None,
        *,
        write_behind: bool | None = None,
        flush_delay: float = _FLUSH_DELAY_SEC,
        cache_ttl: float | None = _AUTO,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.key_builder = key_builder or DefaultKeyBuilder(with_bot_id=True, with_destiny=True)
        url = _database_url(sessionmaker)
        self._memory: MemoryStorage | None = MemoryStorage() if _is_memory_sqlite(url) else None
        if write_behind is None:
            write_behind = _is_sqlite(url)
        self.write_behind = bool(write_behind) and self._memory is None
        self.flush_delay = max(0.0, float(flush_delay))
        if cache_ttl is _AUTO:
            cache_ttl = None if self.write_behind else _CACHE_TTL_SEC
        self.cache_ttl: float | None = None if cache_ttl is None else max(0.0, float(cache_ttl))
        self._cache: dict[str, _Entry] = {}
        self._dirty: set[str] = set()      # изменены и ещё не записаны
        self._flushing: set[str] = set()   # пишутся прямо сейчас
        self._flush_task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None  # flush()/close(): писать сразу, не дожидаясь flush_delay

    # --- Интерфейс BaseStorage ----------------------------------------------------------------

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = _state_name(state)
        if self._memory is not None:
            await self._memory.set_state(key, value)
            return
        row_key = self._key(key)
        entry = await self._entry(row_key)
        self._change(row_key, entry, value, entry.data)

    async def get_state(self, key: StorageKey) -> str | None:
        if self._memory is not None:
            return await self._memory.get_state(key)
        return (await self._entry(self._key(key))).state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        value = _json_dict(data)
        if self._memory is not None:
            await self._memory.set_data(key, value)
            return
        row_key = self._key(key)
        entry = await self._entry(row_key)
        self._change(row_key, entry, entry.state, value)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        if self._memory is not None:
            return await self._memory.get_data(key)
        return copy.deepcopy((await self._entry(self._key(key))).data)

    async def get_value(self, storage_key: StorageKey, dict_key: str, default: Any | None = None) -> Any | None:
        if self._memory is not None:
            return await self._memory.get_value(storage_key, dict_key, default)
        data = (await self._entry(self._key(storage_key))).data
        return copy.deepcopy(data.get(dict_key, default))

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        """Дополнить данные (как ``dict.update``) за одну операцию; вернуть новые данные."""
        partial = _json_dict(data)
        if self._memory is not None:
            return await self._memory.update_data(key, partial)
        row_key = self._key(key)
        entry = await self._entry(row_key)
        merged = {**entry.data, **partial}
        self._change(row_key, entry, entry.state, merged)
        return copy.deepcopy(merged)

    async def close(self) -> None:
        """Остановка бота: записать несохранённые состояния сразу и дождаться записи (не дольше
        _CLOSE_TIMEOUT_SEC). Соединения с базой принадлежат боту (engine закрывает main)."""
        if self._memory is not None:
            await self._memory.close()
            return
        try:
            await asyncio.wait_for(self.flush(), _CLOSE_TIMEOUT_SEC)
        except TimeoutError:
            log.warning(
                "Не все незавершённые диалоги записаны в базу перед остановкой: %s",
                len(self._dirty | self._flushing),
            )
            if self._flush_task is not None:
                self._flush_task.cancel()

    async def flush(self) -> None:
        """Записать изменения сразу (не дожидаясь flush_delay) и дождаться, пока они окажутся в базе."""
        while self._flush_task is not None and not self._flush_task.done():
            if self._wake is not None:
                self._wake.set()
            await asyncio.shield(self._flush_task)

    async def refresh(self, key: StorageKey) -> None:
        """Перечитать значение ключа из базы (1 обмен), если в кэше оно могло устареть.

        Для редких проверок «занято» в хендлерах (ждём ответа AI, результат уже отправляется, задача уже
        создаётся): при обновлении бота на хостинге флаг мог снять другой экземпляр (см. описание модуля),
        а из кэша он виден ещё до ``cache_ttl``. Значение, которое этот экземпляр изменил и ещё не записал
        или пишет прямо сейчас, новее базы — оно остаётся. Один процесс (SQLite в файле: память и есть
        «истина») и база в памяти — ничего не делает.
        """
        if self._memory is not None or self.write_behind:
            return
        row_key = self._key(key)
        if row_key in self._dirty or row_key in self._flushing:
            return
        cached = self._cache.get(row_key)
        loaded = await self._read(row_key)
        if self._cache.get(row_key) is not cached:
            return  # пока читали, ключ изменили или прочитали заново — это значение новее
        self._cache[row_key] = loaded
        self._evict(keep=row_key)

    # --- Кэш ------------------------------------------------------------------------------------

    def _key(self, key: StorageKey) -> str:
        return self.key_builder.build(key)

    def _fresh(self, row_key: str, entry: _Entry) -> bool:
        """Значению из кэша можно верить: оно не записано (память новее базы) или недавно сверено."""
        if self.cache_ttl is None or row_key in self._dirty or row_key in self._flushing:
            return True
        return time.monotonic() - entry.synced < self.cache_ttl

    async def _entry(self, row_key: str) -> _Entry:
        """Текущее значение ключа: из памяти, а при первом обращении (или после cache_ttl) — из базы."""
        entry = self._cache.get(row_key)
        if entry is not None and self._fresh(row_key, entry):
            return entry
        loaded = await self._read(row_key)
        current = self._cache.get(row_key)
        if current is not None and current is not entry:
            return current  # пока читали, ключ изменили или прочитали заново — это значение новее
        self._cache[row_key] = loaded
        self._evict(keep=row_key)
        return loaded

    def _change(self, row_key: str, entry: _Entry, state: str | None, data: dict[str, Any]) -> None:
        """Новое значение ключа — в память сразу, в базу — фоновой записью (если что-то изменилось)."""
        if entry.state == state and entry.data == data:
            return  # ничего не изменилось (например, «очистить» и так пустое) — в базу не пишем
        self._cache[row_key] = entry._replace(state=state, data=data, version=_next_version(entry.version))
        self._dirty.add(row_key)
        if self._flush_task is None or self._flush_task.done():
            self._wake = asyncio.Event()
            self._flush_task = asyncio.get_running_loop().create_task(self._flush_loop(), name="fsm-db-flush")

    def _evict(self, keep: str) -> None:
        """Ограничить память: забыть самые старые записанные ключи (при обращении они прочитаются снова)."""
        excess = len(self._cache) - _CACHE_MAX_KEYS
        if excess <= 0:
            return
        for row_key in list(self._cache):
            if excess <= 0:
                break
            if row_key == keep or row_key in self._dirty or row_key in self._flushing:
                continue
            del self._cache[row_key]
            excess -= 1

    # --- Фоновая запись -------------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        delay = _RETRY_START_SEC
        retrying = False
        while self._dirty:
            if not retrying:
                await self._debounce()
            batch = {row_key: self._cache[row_key] for row_key in self._dirty if row_key in self._cache}
            self._dirty.clear()
            self._flushing.update(batch)
            error: Exception | None = None
            try:
                # SQLite: база занята записью хендлера — транзакция подождёт (busy_timeout), бот не стоит.
                outcome = await self._write(batch)
            except Exception as exc:  # noqa: BLE001 — в памяти значения целы, запишем позже
                error = exc
            except BaseException:  # отмена (остановка бота): значения остаются незаписанными
                self._dirty.update(batch)
                raise
            finally:
                self._flushing.difference_update(batch)
            if error is not None:
                self._dirty.update(batch)
                log.warning(
                    "Состояние диалогов не записано в базу (%s) — повтор через %s с", type(error).__name__, delay
                )
                retrying = True
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_SEC)
                continue
            retrying = False
            delay = _RETRY_START_SEC
            self._written(batch, outcome)

    async def _debounce(self) -> None:
        """Подождать flush_delay, чтобы остальные изменения апдейта ушли той же записью; flush() — сразу."""
        wake = self._wake
        if self.flush_delay <= 0 or wake is None or wake.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), self.flush_delay)

    def _written(self, batch: dict[str, _Entry], outcome: dict[str, bool | None]) -> None:
        """Запись прошла: отметить, что теперь в базе; проигравшие (в базе новее) — убрать из кэша."""
        now = time.monotonic()
        lost = 0
        for row_key, written in batch.items():
            result = outcome.get(row_key, False)
            current = self._cache.get(row_key)
            if current is None:
                continue
            unchanged = current is written and row_key not in self._dirty
            if result is None:  # в базе более новое значение — его записал другой экземпляр бота
                lost += 1
                if unchanged:
                    del self._cache[row_key]  # при следующем обращении прочитаем из базы
                else:
                    self._cache[row_key] = current._replace(in_db=True)
                log.debug("Диалог %s уже изменил другой экземпляр бота — берём значение из базы", row_key)
            elif unchanged:
                skipped = written.empty and not written.in_db  # писать было нечего — с базой не сверялись
                self._cache[row_key] = current._replace(in_db=result, synced=current.synced if skipped else now)
            else:
                self._cache[row_key] = current._replace(in_db=result)  # пока писали, изменили ещё раз
        if lost:
            log.info("Диалоги (%s) уже изменил другой экземпляр бота — берём более новые значения из базы", lost)

    # --- Работа с таблицей ----------------------------------------------------------------------

    async def _read(self, row_key: str) -> _Entry:
        """Значение ключа из базы. Часто это первый запрос апдейта к базе (aiogram читает состояние раньше
        UserMiddleware): соединение, оборвавшееся в пуле (перезапуск пулера или базы), — как в ``load_user``,
        один повтор на новом соединении (чтение ничего не меняет, повторять его безопасно)."""
        try:
            row = await self._select(row_key)
        except DBAPIError as exc:
            if not exc.connection_invalidated:
                raise
            log.info("Соединение с базой оборвалось (%s) — повторяю чтение диалога на новом", type(exc.orig).__name__)
            row = await self._select(row_key)
        synced = time.monotonic()
        if row is None:
            return _Entry(None, {}, None, False, synced)
        return _Entry(row.state, dict(row.data or {}), row.updated_at, True, synced)

    async def _select(self, row_key: str) -> Any:
        async with self.sessionmaker() as session:
            return (
                await session.execute(
                    select(_TABLE.c.state, _TABLE.c.data, _TABLE.c.updated_at).where(_TABLE.c.key == row_key)
                )
            ).first()

    async def _write(self, batch: dict[str, _Entry]) -> dict[str, bool | None]:
        """Записать итоговые значения ключей одной транзакцией. Результат по ключу: True — в базе наше
        значение, False — строки нет, None — в базе более новое значение (наше не записано)."""
        outcome: dict[str, bool | None] = {}
        upserts: dict[str, _Entry] = {}
        deletes: dict[str, _Entry] = {}
        for row_key, entry in batch.items():
            if not entry.empty:
                upserts[row_key] = entry
            elif entry.in_db:
                deletes[row_key] = entry
            else:
                outcome[row_key] = False  # диалог начался и закончился до записи — в базу писать нечего
        if not upserts and not deletes:
            return outcome
        async with self.sessionmaker.begin() as session:
            if upserts:
                outcome.update(await self._upsert(session, upserts))
            for row_key, entry in deletes.items():
                outcome[row_key] = await self._delete(session, row_key, entry)
        return outcome

    async def _upsert(self, session: AsyncSession, entries: dict[str, _Entry]) -> dict[str, bool | None]:
        """Одним выражением: создать строки или обновить те, где версия не новее нашей."""
        make_insert = _UPSERT_INSERTS.get(session.get_bind().dialect.name)
        if make_insert is None:
            return {row_key: await self._upsert_generic(session, row_key, entry) for row_key, entry in entries.items()}
        stmt = make_insert(_TABLE).values(
            [
                {"key": row_key, "state": entry.state, "data": entry.data, "updated_at": entry.version}
                for row_key, entry in entries.items()
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[_TABLE.c.key],
            set_={name: stmt.excluded[name] for name in ("state", "data", "updated_at")},
            where=_TABLE.c.updated_at <= stmt.excluded.updated_at,
        ).returning(_TABLE.c.key)
        written = set((await session.execute(stmt)).scalars())
        return {row_key: (True if row_key in written else None) for row_key in entries}

    async def _upsert_generic(self, session: AsyncSession, row_key: str, entry: _Entry) -> bool | None:
        """UPSERT для прочих СУБД: SELECT ... FOR UPDATE, затем INSERT или UPDATE (если в базе не новее)."""
        current = (
            await session.execute(select(_TABLE.c.updated_at).where(_TABLE.c.key == row_key).with_for_update())
        ).first()
        values = {"state": entry.state, "data": entry.data, "updated_at": entry.version}
        if current is None:
            await session.execute(insert(_TABLE).values(key=row_key, **values))
            return True
        if current.updated_at is not None and entry.version is not None and current.updated_at > entry.version:
            return None
        await session.execute(update(_TABLE).where(_TABLE.c.key == row_key).values(**values))
        return True

    async def _delete(self, session: AsyncSession, row_key: str, entry: _Entry) -> bool | None:
        """Удалить строку, если в ней не более новое значение; не удалилась — None (перечитаем)."""
        stmt = delete(_TABLE).where(_TABLE.c.key == row_key)
        if entry.version is not None:
            stmt = stmt.where(_TABLE.c.updated_at <= entry.version)
        result = await session.execute(stmt)
        return False if result.rowcount else None  # type: ignore[attr-defined]
