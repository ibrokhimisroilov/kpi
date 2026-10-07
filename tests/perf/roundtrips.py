"""Счётчик обменов с базой данных и искусственная задержка сети — только для тестов.

Зачем
=====
Бот на Render ходит в базу Supabase в другом регионе: один обмен с базой (round trip, RT) стоит
130–190 мс, новое соединение (TCP + TLS + SCRAM) — 1–1,4 с. Скорость ответа бота определяется не
процессором, а ЧИСЛОМ обменов. ``RoundTripProbe`` считает их на любом участке кода и по желанию
добавляет к каждому обмену задержку — как будто база далеко. Продакшн-код не меняется: счётчик
подключается к движкам SQLAlchemy событиями и снимается после теста (``detach``).

Что считается (события SQLAlchemy на ``engine.sync_engine``, отдельно по каждому движку)
---------------------------------------------------------------------------------------
* ``statements`` — SQL-выражения, ушедшие в драйвер (``before_cursor_execute``): каждый SELECT,
  каждая selectin-подгрузка связей, каждый flush (INSERT/UPDATE/DELETE), SAVEPOINT;
* ``begins`` / ``commits`` / ``rollbacks`` — так, как их шлёт asyncpg движка ``bot.db.base``
  (отложенный BEGIN): BEGIN — перед первым выражением, которому нужна транзакция (запись, FOR UPDATE,
  текстовый SQL…); чтения до него идут без транзакции. COMMIT/ROLLBACK — только у транзакции, где
  был BEGIN: сессия, которая только читала, их не шлёт. На PostgreSQL это читается из настоящего
  состояния драйвера (``autocommit``), на SQLite — моделируется тем же правилом
  (``bot.db.base._is_plain_read``), поэтому числа на обеих базах одинаковые;
* ``prepares`` — подготовка выражения (Parse/Describe): SQLAlchemy готовит каждое выражение через
  ``asyncpg.prepare`` и кэширует его на соединении, поэтому +1 обмен — при первом выполнении текста
  на этом соединении (после нового соединения кэш пуст). ``statement_cache=False`` — кэша нет
  (transaction pooler Supabase, порт 6543): +1 обмен на каждое выражение;
* ``checkouts`` — выдачи соединения из пула (каждая сессия, первое чтение и фоновая запись
  хранилища диалогов);
* ``pings`` — проверка соединения, простоявшего в пуле дольше ``PG_PING_IDLE_SEC`` (60 с): простой
  запрос ``SELECT 1`` — ``PING_ROUND_TRIPS`` = 1 обмен. В тесте «простой» задаёт ``idle()``;
* ``connects`` — новые соединения; стоимость одного — ``CONNECT_ROUND_TRIPS`` обменов.

``round_trips`` = statements + prepares + begins + commits + rollbacks + pings × PING
+ connects × CONNECT — модель обменов PostgreSQL + asyncpg. Сверена с настоящим трафиком asyncpg
через TCP-прокси (``WireCounter``, тест ``test_model_matches_postgres_wire``): совпадает до обмена.

Ожидание пользователя и фон
---------------------------
Хранилище диалогов (``bot.fsm_storage.DbStorage``) пишет в базу фоновой задачей через полсекунды
после изменения — пользователь эту запись не ждёт. Обмены фоновой работы считаются отдельно
(``Measurement.background``): её корутину помечает ``mark_background`` (признак — в contextvars,
поэтому его наследуют и задачи, которые SQLAlchemy создаёт для COMMIT при выходе из сессии).
``flush`` — функция, которая дописывает фоновые записи (``storage.flush``): ``measure`` вызывает её
в конце замера, так что запись шага относится к этому шагу (следующее сообщение человека приходит
позже, чем через полсекунды).

Задержка
--------
``RoundTripProbe(latency_ms=150)`` внутри ``measure`` на каждом событии ждёт
``asyncio.sleep(latency × обменов)``. Код SQLAlchemy асинхронного движка выполняется в greenlet,
поэтому ожидание идёт через ``sqlalchemy.util.concurrency.await_`` — цикл событий в это время
свободен, как при настоящей сетевой задержке. Вне ``measure`` (подготовка данных) задержки нет.

Использование
-------------
.. code-block:: python

    probe = RoundTripProbe(latency_ms=150, flush=storage.flush)
    probe.attach(engine, "main")             # движок хендлеров
    probe.attach(storage_engine, "fsm")      # пул хранилища диалогов
    probe.mark_background(storage, "_flush_loop")  # фоновая запись хранилища — отдельно
    async with probe.measure("S9 текст без диалога") as m:
        await h.send_text(2001, "привет")
    print(format_table([m]))

    async with probe.scenario("S5 мастер задачи") as sc:      # по шагам
        await sc.step("меню", h.press_menu(1001, BTN_NEW_TASK))
        await sc.step("сотрудник", h.press_button(1001, "Иванов"))

    await probe.idle(engine, "main")         # следующая выдача — после простоя (проверка SELECT 1)
    await probe.recycle(engine, "main")      # следующая выдача — новое соединение (pool_recycle)
    probe.detach()
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.db.base import _LAST_USED, PG_PING_IDLE_SEC, _is_plain_read

try:  # SQLAlchemy 2.1
    from sqlalchemy.util.concurrency import await_, in_greenlet
except ImportError:  # pragma: no cover - SQLAlchemy 2.0
    from sqlalchemy.util import await_only as await_  # type: ignore[no-redef]
    from sqlalchemy.util.concurrency import in_greenlet  # type: ignore[no-redef]

__all__ = [
    "CONNECT_ROUND_TRIPS",
    "DEFAULT_LATENCY_MS",
    "LOCAL_CONNECT_ROUND_TRIPS",
    "PING_ROUND_TRIPS",
    "Counts",
    "Measurement",
    "RoundTripProbe",
    "Scenario",
    "WireCounter",
    "dump_json",
    "format_sql",
    "format_table",
]

T = TypeVar("T")

DEFAULT_LATENCY_MS = 150.0  # Render (Франкфурт) -> Supabase (Мумбаи): 130–190 мс на обмен
FRANKFURT_LATENCY_MS = 2.0  # Render (Франкфурт) -> Supabase (Франкфурт)
PING_ROUND_TRIPS = 1  # проверка простоявшего соединения: простой запрос SELECT 1 (bot.db.base._ping)
# Новое соединение: TCP 1 + SSLRequest 1 + TLS 1–2 + startup/SCRAM 3 ≈ 7 обменов ≈ 1,05–1,3 с при 150–190 мс
# (замер в проде — 1–1,4 с). Локальный PostgreSQL без TLS через прокси: 4 обмена (SSLRequest + 3).
CONNECT_ROUND_TRIPS = 7
# Идёт фоновая работа (mark_background): её обмены пользователь не ждёт.
_IN_BACKGROUND: ContextVar[bool] = ContextVar("perf_in_background", default=False)


@dataclass
class Counts:
    """Счётчики обменов одного движка (или сумма по нескольким)."""

    statements: int = 0
    prepares: int = 0
    begins: int = 0
    commits: int = 0
    rollbacks: int = 0
    checkouts: int = 0
    pings: int = 0
    connects: int = 0

    def copy(self) -> Counts:
        return Counts(**{f.name: getattr(self, f.name) for f in fields(self)})

    def __add__(self, other: Counts) -> Counts:
        return Counts(**{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)})

    def __sub__(self, other: Counts) -> Counts:
        return Counts(**{f.name: getattr(self, f.name) - getattr(other, f.name) for f in fields(self)})

    @property
    def transactions(self) -> int:
        """Транзакции, дошедшие до базы (по одному BEGIN на каждую)."""
        return self.begins

    def round_trips(self, *, ping: int = PING_ROUND_TRIPS, connect: int = CONNECT_ROUND_TRIPS) -> int:
        """Обмены с базой по модели PostgreSQL + asyncpg (см. описание модуля)."""
        return (
            self.statements
            + self.prepares
            + self.begins
            + self.commits
            + self.rollbacks
            + self.pings * ping
            + self.connects * connect
        )

    def as_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _sum(counts: Iterable[Counts]) -> Counts:
    result = Counts()
    for item in counts:
        result = result + item
    return result


@dataclass
class Measurement:
    """Результат одного замера (сценарий или шаг сценария).

    ``by_engine`` — обмены, которые ждёт пользователь (код апдейта); ``background`` — обмены фоновых
    задач (запись хранилища диалогов), по движкам.
    """

    name: str
    latency_ms: float = 0.0  # задержка, которая реально добавлялась к каждому обмену
    ping_round_trips: int = PING_ROUND_TRIPS
    connect_round_trips: int = CONNECT_ROUND_TRIPS
    by_engine: dict[str, Counts] = field(default_factory=dict)
    background: dict[str, Counts] = field(default_factory=dict)
    wall_ms: float = 0.0  # время, которое ждал пользователь (без фоновой записи)
    api_calls: int = 0  # запросы к Telegram Bot API за замер (если probe знает, как их считать)
    sql: list[tuple[str, str]] = field(default_factory=list)  # (движок, текст выражения)
    steps: list[Measurement] = field(default_factory=list)
    note: str = ""

    @property
    def total(self) -> Counts:
        """Обмены, которые ждёт пользователь, по всем движкам."""
        return _sum(self.by_engine.values())

    @property
    def background_total(self) -> Counts:
        return _sum(self.background.values())

    def engine(self, label: str) -> Counts:
        return self.by_engine.get(label, Counts())

    def round_trips_of(self, counts: Counts) -> int:
        return counts.round_trips(ping=self.ping_round_trips, connect=self.connect_round_trips)

    @property
    def round_trips(self) -> int:
        """Обмены, которые ждёт пользователь."""
        return self.round_trips_of(self.total)

    @property
    def background_round_trips(self) -> int:
        return self.round_trips_of(self.background_total)

    @property
    def all_round_trips(self) -> int:
        """Все обмены замера: ожидание пользователя + фон (столько видит сеть)."""
        return self.round_trips + self.background_round_trips

    def ms_at(self, latency_ms: float) -> float:
        """Время ожидания при задержке latency_ms на обмен: замер, если задержка была именно такой, иначе
        модель «замер + обмены × (latency_ms − добавленная задержка)»."""
        return self.wall_ms + self.round_trips * (latency_ms - self.latency_ms)

    @property
    def cpu_ms(self) -> float:
        """Время ожидания без сетевой задержки (работа процессора и SQLite на этой машине)."""
        return self.ms_at(0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "note": self.note,
            "latency_ms": self.latency_ms,
            "wall_ms": round(self.wall_ms, 1),
            "cpu_ms": round(self.cpu_ms, 1),
            "round_trips": self.round_trips,
            "background_round_trips": self.background_round_trips,
            "api_calls": self.api_calls,
            "total": self.total.as_dict(),
            "background_total": self.background_total.as_dict(),
            "by_engine": {label: counts.as_dict() for label, counts in self.by_engine.items()},
            "background": {label: counts.as_dict() for label, counts in self.background.items()},
            "steps": [step.as_dict() for step in self.steps],
        }


class Scenario:
    """Сценарий из шагов: ``await sc.step("имя", корутина)`` замеряет шаг и добавляет его в итог."""

    def __init__(self, probe: RoundTripProbe, measurement: Measurement) -> None:
        self.probe = probe
        self.measurement = measurement

    async def step(self, name: str, awaitable: Awaitable[T]) -> T:
        async with self.probe.measure(name) as step:
            result = await awaitable
        self.measurement.steps.append(step)
        return result


class RoundTripProbe:
    """Счётчик обменов с базой для одного или нескольких движков (см. описание модуля).

    :param latency_ms: задержка на каждый обмен внутри ``measure`` (0 — только счёт).
    :param ping_round_trips: обменов на одну проверку простоявшего соединения.
    :param connect_round_trips: обменов на одно новое соединение.
    :param statement_cache: кэш подготовленных выражений на соединении (session pooler, по умолчанию);
        False — как в transaction pooler (порт 6543): каждое выражение готовится заново.
    :param flush: дописать фоновые записи (``DbStorage.flush``) — вызывается в начале и в конце замера.
    :param api_calls: сколько запросов к Telegram сделано к этому моменту (для колонки «tg»).
    """

    def __init__(
        self,
        *,
        latency_ms: float = 0.0,
        ping_round_trips: int = PING_ROUND_TRIPS,
        connect_round_trips: int = CONNECT_ROUND_TRIPS,
        statement_cache: bool = True,
        flush: Callable[[], Awaitable[None]] | None = None,
        api_calls: Callable[[], int] | None = None,
    ) -> None:
        self.latency_ms = float(latency_ms)
        self.ping_round_trips = ping_round_trips
        self.connect_round_trips = connect_round_trips
        self.statement_cache = statement_cache
        self.flush = flush
        self.api_calls = api_calls
        self.counts: dict[str, Counts] = {}  # обмены, которые ждёт пользователь
        self.bg_counts: dict[str, Counts] = {}  # обмены фоновых задач
        self.sql: list[tuple[str, str]] = []
        self._depth = 0  # вложенность measure: задержка и журнал SQL — только внутри
        self._pending: set[int] = set()  # id(Connection): begin() был, BEGIN ещё не ушёл в базу
        self._started: set[int] = set()  # id(Connection): BEGIN ушёл, ждём COMMIT/ROLLBACK
        self._prepared: dict[int, set[str]] = {}  # id(DBAPI-соединения) -> подготовленные на нём выражения
        self._cold: set[str] = set()  # движки, у которых следующая выдача — «новое соединение»
        self._idle: set[str] = set()  # движки (SQLite), у которых следующая выдача — «после простоя»
        self._records: dict[str, dict[int, Any]] = {}  # движок -> записи пула, возвращавшиеся в пул
        self._undo: list[Callable[[], None]] = []

    # --- Подключение к движкам ------------------------------------------------------------------

    @staticmethod
    def _in_background() -> bool:
        return _IN_BACKGROUND.get()

    def mark_background(self, owner: Any, method: str) -> None:
        """Обмены корутины ``owner.method`` (и задач, которые она создаёт) — фоновые: их пользователь не
        ждёт (``Measurement.background``). Например, ``mark_background(storage, "_flush_loop")``."""
        original = getattr(owner, method)

        async def background(*args: Any, **kwargs: Any) -> Any:
            token = _IN_BACKGROUND.set(True)  # задачи, созданные внутри, копируют контекст с признаком
            try:
                return await original(*args, **kwargs)
            finally:
                _IN_BACKGROUND.reset(token)

        setattr(owner, method, background)
        self._undo.append(lambda: vars(owner).pop(method, None))

    def attach(self, engine: AsyncEngine, label: str, *, deferred_begin: bool | None = None) -> None:
        """Считать обмены движка ``engine`` под именем ``label`` (до ``detach``).

        ``deferred_begin`` — как считать BEGIN: None — PostgreSQL по настоящему состоянию драйвера,
        прочие базы — как PostgreSQL-движок бота (отложенный BEGIN); True/False — модель явно
        (False — BEGIN перед первым выражением любой транзакции, как на transaction pooler).
        """
        if label in self.counts:
            raise ValueError(f"движок {label!r} уже подключён")
        sync_engine = engine.sync_engine
        postgres = engine.dialect.name == "postgresql"
        modelled = None if (postgres and deferred_begin is None) else (True if deferred_begin is None else deferred_begin)
        self.counts[label] = Counts()
        self.bg_counts[label] = Counts()

        def target() -> Counts:
            return (self.bg_counts if self._in_background() else self.counts)[label]

        def opens_transaction(conn: Any, statement: str, context: Any) -> bool:
            """asyncpg отправит BEGIN перед этим выражением (транзакция сессии уже начата в SQLAlchemy)."""
            if modelled is None:  # PostgreSQL: autocommit=True — выражение идёт без транзакции
                return getattr(conn.connection.dbapi_connection, "autocommit", False) is not True
            return not modelled or not _is_plain_read(statement, context)

        def on_begin(conn: Any) -> None:
            self._pending.add(id(conn))

        def on_execute(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
            background = self._in_background()
            counts = (self.bg_counts if background else self.counts)[label]
            key = id(conn)
            trips = 1
            if key in self._pending and opens_transaction(conn, statement, context):
                self._pending.discard(key)
                self._started.add(key)
                counts.begins += 1
                trips += 1
            prepared = self._prepared.setdefault(id(conn.connection.dbapi_connection), set())
            if not self.statement_cache or statement not in prepared:  # asyncpg.prepare: Parse/Describe
                prepared.add(statement)
                counts.prepares += 1
                trips += 1
            counts.statements += 1
            if self._depth:
                self.sql.append((f"{label}, фон" if background else label, statement))
            self._wait(trips)

        def on_end(kind: str) -> Callable[[Any], None]:
            def handler(conn: Any) -> None:
                key = id(conn)
                self._pending.discard(key)
                if key not in self._started:
                    return  # транзакция без BEGIN (только чтения или ничего): asyncpg ничего не шлёт
                self._started.discard(key)
                counts = target()
                if kind == "commit":
                    counts.commits += 1
                else:
                    counts.rollbacks += 1
                self._wait(1)

            return handler

        def on_checkout(dbapi_connection: Any, record: Any, proxy: Any) -> None:
            counts = target()
            counts.checkouts += 1
            if label in self._cold:  # соединение «пересоздано» (см. recycle): вместо выдачи — подключение
                self._cold.discard(label)
                self._forget(dbapi_connection)
                counts.connects += 1
                self._wait(self.connect_round_trips)
                return
            if label in self._idle:
                self._idle.discard(label)
                pinged = True
            else:  # PostgreSQL: то же правило, что у bot.db.base (обработчик выдачи уже отработал до нас)
                last_used = record.info.get(_LAST_USED)
                pinged = last_used is not None and time.monotonic() - last_used > PG_PING_IDLE_SEC
            if pinged:
                counts.pings += 1
                self._wait(self.ping_round_trips)

        def on_connect(dbapi_connection: Any, record: Any) -> None:
            self._forget(dbapi_connection)
            target().connects += 1
            self._wait(self.connect_round_trips)

        records = self._records.setdefault(label, {})

        def on_checkin(dbapi_connection: Any, record: Any) -> None:
            records[id(record)] = record  # для idle(): чьё время возврата сдвигать

        listeners: list[tuple[str, Callable[..., Any]]] = [
            ("begin", on_begin),
            ("before_cursor_execute", on_execute),
            ("commit", on_end("commit")),
            ("rollback", on_end("rollback")),
            ("checkout", on_checkout),  # события пула можно вешать на Engine
            ("connect", on_connect),
            ("checkin", on_checkin),
        ]
        for name, fn in listeners:
            event.listen(sync_engine, name, fn)
            self._undo.append(lambda name=name, fn=fn: event.remove(sync_engine, name, fn))

    def _forget(self, dbapi_connection: Any) -> None:
        """Новое соединение: на нём ещё ничего не подготовлено."""
        self._prepared[id(dbapi_connection)] = set()

    def detach(self) -> None:
        """Снять все обработчики событий (движки возвращаются в исходное состояние)."""
        while self._undo:
            self._undo.pop()()
        self._cold.clear()
        self._idle.clear()
        self._records.clear()

    @staticmethod
    def _in_memory(engine: AsyncEngine) -> bool:
        url = engine.url
        return url.get_backend_name() == "sqlite" and (
            (url.database or "") in ("", ":memory:") or url.query.get("mode") == "memory"
        )

    async def recycle(self, engine: AsyncEngine, label: str) -> None:
        """Следующая выдача соединения движка — новое соединение, как после ``pool_recycle``
        (30 мин), деплоя или сна бесплатного Render.

        PostgreSQL и SQLite в файле — ``engine.dispose()`` (настоящее переподключение). SQLite в памяти
        переподключить нельзя (база исчезнет): там следующая выдача СЧИТАЕТСЯ новым соединением,
        с теми же счётчиками и задержкой.
        """
        if self._in_memory(engine):
            self._cold.add(label)
        else:
            await engine.dispose()

    async def idle(self, engine: AsyncEngine, label: str) -> None:
        """Соединения пула простояли дольше ``PG_PING_IDLE_SEC``: следующая выдача проверит соединение
        (``bot.db.base``, один обмен SELECT 1).

        PostgreSQL — по-настоящему: время возврата соединений в пул сдвигается в прошлое, и проверку
        выполняет сам движок. SQLite — следующая выдача движка СЧИТАЕТСЯ выдачей после простоя.
        """
        if engine.dialect.name != "postgresql":
            self._idle.add(label)
            return
        for record in self._records.get(label, {}).values():
            if _LAST_USED in record.info:
                record.info[_LAST_USED] = time.monotonic() - PG_PING_IDLE_SEC - 1

    # --- Замеры ----------------------------------------------------------------------------------

    def _wait(self, round_trips: int) -> None:
        """Задержка «сети» на round_trips обменов (только внутри measure и внутри greenlet SQLAlchemy)."""
        if self._depth and self.latency_ms > 0 and round_trips > 0 and in_greenlet():
            await_(asyncio.sleep(self.latency_ms * round_trips / 1000))

    @asynccontextmanager
    async def measure(self, name: str, note: str = "") -> AsyncIterator[Measurement]:
        """Замерить блок: счётчики по движкам (ожидание и фон), SQL и время; задержка — ``latency_ms``
        на обмен. Фоновые записи, сделанные до замера, в него не попадают; сделанные в замере —
        дописываются в его конце (``flush``) и попадают в ``background``."""
        if not self._depth and self.flush is not None:
            await self.flush()  # чужие (подготовка данных) фоновые записи — до замера
        measurement = Measurement(
            name=name,
            note=note,
            latency_ms=self.latency_ms,
            ping_round_trips=self.ping_round_trips,
            connect_round_trips=self.connect_round_trips,
        )
        before = {label: counts.copy() for label, counts in self.counts.items()}
        before_bg = {label: counts.copy() for label, counts in self.bg_counts.items()}
        api_before = self.api_calls() if self.api_calls is not None else 0
        sql_start = len(self.sql)
        self._depth += 1
        started = time.perf_counter()
        try:
            yield measurement
        finally:
            measurement.wall_ms = (time.perf_counter() - started) * 1000
            if self.api_calls is not None:
                measurement.api_calls = self.api_calls() - api_before
            try:
                if self.flush is not None:
                    await self.flush()
            finally:
                self._depth -= 1
                measurement.by_engine = {
                    label: counts - before.get(label, Counts()) for label, counts in self.counts.items()
                }
                measurement.background = {
                    label: counts - before_bg.get(label, Counts()) for label, counts in self.bg_counts.items()
                }
                measurement.sql = self.sql[sql_start:]
                if not self._depth:
                    self.sql.clear()

    @asynccontextmanager
    async def scenario(self, name: str, note: str = "") -> AsyncIterator[Scenario]:
        """Замер сценария по шагам: счётчики — весь блок, шаги — ``Scenario.step``; время ожидания —
        сумма шагов (фоновые записи между шагами и проверки теста пользователь не ждёт)."""
        async with self.measure(name, note) as measurement:
            yield Scenario(self, measurement)
        if measurement.steps:
            measurement.wall_ms = sum(step.wall_ms for step in measurement.steps)
            measurement.api_calls = sum(step.api_calls for step in measurement.steps)


# --- Сверка модели с настоящим трафиком PostgreSQL ---------------------------------------------------

LOCAL_CONNECT_ROUND_TRIPS = 4  # локальный PostgreSQL без TLS: SSLRequest + startup + 2 шага SCRAM


class WireCounter:
    """TCP-прокси к PostgreSQL, считающий настоящие обмены клиента с сервером (для сверки модели).

    Обмен — клиент начал писать после ответа сервера (или впервые на соединении): asyncpg не
    отправляет следующий запрос, не дождавшись ответа на предыдущий. Движки нужно подключить к
    ``127.0.0.1:port`` прокси (``url.set(host="127.0.0.1", port=wire.port)``).
    """

    def __init__(self, host: str, port: int) -> None:
        self.target = (host, port)
        self.port = 0
        self.round_trips = 0
        self.connections = 0
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> WireCounter:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except TimeoutError:
                pass

    async def _handle(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(client_writer)
        try:
            server_reader, server_writer = await asyncio.open_connection(*self.target)
        except OSError:
            client_writer.close()
            return
        self._writers.append(server_writer)
        last: list[str] = [""]

        def from_client() -> None:
            if last[0] != "client":
                self.round_trips += 1
            last[0] = "client"

        def from_server() -> None:
            last[0] = "server"

        await asyncio.gather(
            self._pipe(client_reader, server_writer, from_client),
            self._pipe(server_reader, client_writer, from_server),
        )

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, seen: Callable[[], None]) -> None:
        try:
            while data := await reader.read(65536):
                seen()
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()


# --- Отчёт ------------------------------------------------------------------------------------------


def _transactions(counts: Counts) -> str:
    """«5 (4c/1r)» — транзакций, из них COMMIT / ROLLBACK."""
    return f"{counts.begins} ({counts.commits}c/{counts.rollbacks}r)"


def _ms(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def _row(measurement: Measurement, latency_ms: float, *, indent: str = "") -> str:
    total = measurement.total
    fsm = measurement.engine("fsm")
    model = "" if measurement.latency_ms == latency_ms else "~"
    cells = [
        indent + measurement.name,
        str(total.statements),
        str(total.prepares),
        _transactions(total),
        str(total.checkouts),
        str(total.connects),
        str(total.pings),
        f"{measurement.round_trips} ({measurement.round_trips_of(fsm)})",
        str(measurement.background_round_trips),
        str(measurement.api_calls),
        f"{model}{_ms(measurement.ms_at(latency_ms))}",
        f"~{_ms(measurement.ms_at(FRANKFURT_LATENCY_MS))}",
    ]
    return "| " + " | ".join(cells) + " |"


def format_table(measurements: Iterable[Measurement], *, latency_ms: float = DEFAULT_LATENCY_MS) -> str:
    """Markdown-таблица замеров (шаги сценариев — строками с отступом).

    Колонки — то, что ждёт пользователь (statements … RTs); «bg RTs» — фоновая запись хранилища
    диалогов, «tg» — запросы к Telegram. «~» во времени — модель (замер без задержки + обмены × задержка),
    без «~» — замер с настоящей задержкой latency_ms на каждый обмен. Время — только обмены с базой
    и процессор этой машины, без запросов к Telegram.
    """
    items = list(measurements)
    header = (
        f"| scenario | statements | prepares | transactions (commit/rollback) | checkouts | new conns | pings "
        f"| RTs (fsm) | bg RTs | tg | ms @{latency_ms:g}ms RTT | ms @{FRANKFURT_LATENCY_MS:g}ms RTT |"
    )
    lines = [header, "|" + "---|" * 12]
    for measurement in items:
        lines.append(_row(measurement, latency_ms))
        for step in measurement.steps:
            lines.append(_row(step, latency_ms, indent="↳ "))
    return "\n".join(lines)


def format_sql(measurement: Measurement, *, width: int = 160) -> str:
    """Журнал выражений замера: «[движок] первая строка SQL» — для поиска лишних запросов."""
    lines = [
        f"--- {measurement.name}: {measurement.total.statements} выражений "
        f"(+ {measurement.background_total.statements} в фоне)"
    ]
    for label, statement in measurement.sql:
        text = " ".join(statement.split())
        lines.append(f"  [{label}] {text[:width]}{'…' if len(text) > width else ''}")
    return "\n".join(lines)


def dump_json(measurements: Iterable[Measurement], path: str | Path) -> None:
    """Сохранить замеры в JSON (для сравнения «до/после»)."""
    data = [measurement.as_dict() for measurement in measurements]
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
