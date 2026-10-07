"""Фикстуры замеров обменов с БД (tests/perf): бот целиком + ``RoundTripProbe``.

Фикстура ``perf`` собирает бота так же, как ``bot.main.main`` в продакшне на PostgreSQL:

* ``build_dispatcher`` с хранилищем диалогов ``DbStorage`` (кэш в памяти, запись в базу — фоновой
  задачей) на ОТДЕЛЬНОМ движке (``make_storage_engine``), как для общей базы (``write_behind=False``);
* движки — ``bot.db.base.make_engine`` как есть: без pre-ping, проверка соединения только после
  простоя, отложенный BEGIN (на SQLite его моделирует счётчик, см. tests/perf/roundtrips.py);
* фоновая запись хранилища — в конце каждого замера (``flush_delay`` большой, а ``RoundTripProbe``
  вызывает ``storage.flush``): запись шага диалога относится к этому шагу и считается отдельно
  («bg RTs» — её пользователь не ждёт);
* Telegram — фейковый (tests/e2e/fakebot.py), AI выключен (AI_PROVIDER=none).

База: по умолчанию SQLite — основная в памяти, хранилище диалогов в отдельном файле во временной
папке (две разные базы не блокируют друг друга, как два пула PostgreSQL). С TEST_DATABASE_URL —
PostgreSQL с настоящими пулами (фикстуры ``engine`` и ``storage_engine`` из tests/conftest.py).
Модель обменов (tests/perf/roundtrips.py) на обеих базах даёт одинаковые счётчики.

Задержка на обмен: ``PERF_LATENCY_MS`` (мс). Если не задана: 150 мс при запуске с ``-m perf``
(замер «как в проде»), иначе 0 — тесты быстрые, время в таблице — по модели (со знаком «~»).
Итоговая таблица печатается в конце запуска; ``PERF_SQL=1`` — ещё и журнал SQL каждого сценария,
``PERF_REPORT=путь.json`` — замеры в JSON.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parents[1]
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from aiogram import Bot, Router  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402

from bot.fsm_storage import DbStorage  # noqa: E402

from e2e.fakebot import BotHarness, FakeSession  # noqa: E402

from .roundtrips import (  # noqa: E402
    DEFAULT_LATENCY_MS,
    Measurement,
    RoundTripProbe,
    dump_json,
    format_sql,
    format_table,
)

PERF_ENV: dict[str, str] = {
    "BOT_TOKEN": "42:TEST",
    "ADMIN_IDS": "1001",
    "AI_PROVIDER": "none",
    "TIMEZONE": "Asia/Tashkent",
}

# Хранилище диалогов пишет в базу только по storage.flush() в конце замера (см. описание модуля):
# счётчики не зависят от скорости машины и от задержки.
PERF_FLUSH_DELAY_SEC = 3600.0

# Замеры всех тестов запуска — для итоговой таблицы (pytest_terminal_summary).
RESULTS: list[Measurement] = []
_LATENCY: dict[str, float] = {}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "perf: замеры обменов с БД (tests/perf); с -m perf — с задержкой 150 мс на обмен (PERF_LATENCY_MS)",
    )


def resolve_latency_ms(config: pytest.Config) -> float:
    """PERF_LATENCY_MS; иначе 150 мс, если запуск выбран маркером perf (-m perf), иначе 0."""
    raw = os.environ.get("PERF_LATENCY_MS", "").strip()
    if raw:
        return float(raw)
    markexpr = (config.getoption("markexpr", default="") or "").replace(" ", "")
    selected = "perf" in markexpr and "notperf" not in markexpr
    return DEFAULT_LATENCY_MS if selected else 0.0


def release_bot_routers() -> None:
    """Отвязать модульные роутеры bot/handlers от Dispatcher'ов прошлых тестов (как tests/e2e/conftest.py)."""
    module_routers: dict[int, Router] = {}
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "bot" or name.startswith("bot.")):
            continue
        for value in list(vars(module).values()):
            if isinstance(value, Router):
                module_routers[id(value)] = value
    for router in module_routers.values():
        parent = router.parent_router
        if parent is None or id(parent) in module_routers:
            continue
        if router in parent.sub_routers:
            parent.sub_routers.remove(router)
        router._parent_router = None  # публичного API для отвязки в aiogram нет


@dataclass
class PerfApp:
    """Бот + счётчик: ``h`` — BotHarness, ``probe`` — RoundTripProbe (движки "main" и "fsm")."""

    h: BotHarness
    probe: RoundTripProbe
    sessionmaker: async_sessionmaker[AsyncSession]
    main_engine: AsyncEngine
    fsm_engine: AsyncEngine
    backend: str
    storage: DbStorage

    async def recycle_connections(self) -> None:
        """Оба пула переподключаются при следующем обращении (pool_recycle, деплой, сон Render)."""
        await self.probe.recycle(self.main_engine, "main")
        await self.probe.recycle(self.fsm_engine, "fsm")

    async def idle_connections(self) -> None:
        """Соединения обоих пулов простояли дольше PG_PING_IDLE_SEC: следующая выдача их проверит."""
        await self.probe.idle(self.main_engine, "main")
        await self.probe.idle(self.fsm_engine, "fsm")

    async def forget_dialogs(self) -> None:
        """Кэш хранилища диалогов пуст, как после перезапуска бота или через cache_ttl (10 мин):
        следующее обращение к каждому ключу читает его из базы."""
        await self.storage.flush()
        self.storage._cache.clear()  # noqa: SLF001 - тестовый инструмент

    def record(self, measurement: Measurement) -> Measurement:
        """Запомнить замер для итоговой таблицы и напечатать его (видно с -s)."""
        measurement.note = measurement.note or self.backend
        RESULTS.append(measurement)
        print()
        print(format_table([measurement], latency_ms=DEFAULT_LATENCY_MS))
        if os.environ.get("PERF_SQL", "").strip() not in ("", "0"):
            print(format_sql(measurement))
            for step in measurement.steps:
                print(format_sql(step))
        return measurement


@pytest.fixture
def perf_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    from bot.config import get_settings

    for key, value in PERF_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@asynccontextmanager
async def open_perf_app(
    main_engine: AsyncEngine, fsm_engine: AsyncEngine, *, backend: str, probe: RoundTripProbe
) -> AsyncIterator[PerfApp]:
    """Бот на готовых движках (таблицы уже созданы): Dispatcher как в bot.main.main на PostgreSQL —
    DbStorage (общая база: ``write_behind=False``) на движке ``fsm_engine``; ``probe`` подключается
    к обоим движкам, дописывает фоновые записи хранилища в конце замера и считает запросы к Telegram."""
    from bot.db.base import make_sessionmaker
    from bot.main import build_dispatcher

    probe.attach(main_engine, "main")
    probe.attach(fsm_engine, "fsm")
    sessionmaker = make_sessionmaker(main_engine)
    storage = DbStorage(make_sessionmaker(fsm_engine), write_behind=False, flush_delay=PERF_FLUSH_DELAY_SEC)
    release_bot_routers()
    dp = build_dispatcher(sessionmaker, storage)
    session = FakeSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    harness = BotHarness(dp, bot, sessionmaker)
    probe.mark_background(storage, "_flush_loop")  # запись хранилища в базу — фоновая, её не ждут
    probe.flush = storage.flush
    probe.api_calls = lambda: len(session.calls)
    try:
        yield PerfApp(harness, probe, sessionmaker, main_engine, fsm_engine, backend, storage)
    finally:
        try:
            await storage.close()
        finally:
            probe.detach()
            await bot.session.close()
            release_bot_routers()


@pytest_asyncio.fixture
async def perf(
    request: pytest.FixtureRequest,
    perf_env: None,
    engine: AsyncEngine,
    storage_engine: AsyncEngine | None,
    tmp_path: Path,
) -> AsyncIterator[PerfApp]:
    """Бот как в продакшне (DbStorage на своём пуле, движки make_engine) + счётчик обменов на обоих движках."""
    from bot.db.base import init_db, make_engine

    own: list[AsyncEngine] = []
    if engine.dialect.name == "postgresql" and storage_engine is not None:
        main_engine, fsm_engine, backend = engine, storage_engine, "postgresql"
    else:
        main_engine = make_engine("sqlite+aiosqlite:///:memory:")
        fsm_engine = make_engine(f"sqlite+aiosqlite:///{(tmp_path / 'fsm.db').as_posix()}")
        own += [main_engine, fsm_engine]
        await init_db(main_engine)
        await init_db(fsm_engine)
        backend = "sqlite"

    latency = _LATENCY.setdefault("ms", resolve_latency_ms(request.config))
    try:
        async with open_perf_app(
            main_engine, fsm_engine, backend=backend, probe=RoundTripProbe(latency_ms=latency)
        ) as app:
            yield app
    finally:
        for own_engine in own:
            await own_engine.dispose()


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    if not RESULTS:
        return
    latency = _LATENCY.get("ms", 0.0)
    mode = (
        f"задержка {latency:g} мс на обмен добавлялась на самом деле"
        if latency
        else "без задержки; время «~» = замер + обмены × 150 мс (запуск с -m perf добавит задержку на самом деле)"
    )
    terminalreporter.section(f"Обмены с БД по сценариям ({RESULTS[0].note}; {mode})")
    for line in format_table(RESULTS, latency_ms=DEFAULT_LATENCY_MS).splitlines():
        terminalreporter.write_line(line)
    terminalreporter.write_line(
        "RTs — обмены с базой, которые ждёт пользователь, по модели PostgreSQL+asyncpg (в скобках — из них "
        "хранилище диалогов): statements + prepares + BEGIN + COMMIT/ROLLBACK + pings × 1 + new conns × 7. "
        "bg RTs — фоновая запись хранилища диалогов (её никто не ждёт); tg — запросы к Telegram."
    )
    report = os.environ.get("PERF_REPORT", "").strip()
    if report:
        dump_json(RESULTS, report)
        terminalreporter.write_line(f"Замеры сохранены: {report}")
