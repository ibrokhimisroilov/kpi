"""Фикстуры e2e-тестов: настоящий Dispatcher бота + фейковый Telegram API (tests/e2e/fakebot.py).

* ``app`` — ``BotHarness``: БД из фикстуры ``engine`` (tests/conftest.py: in-memory SQLite, а с
  TEST_DATABASE_URL — очищенный PostgreSQL), ``bot.main.build_dispatcher(sessionmaker)``,
  ``Bot("42:TEST", session=FakeSession(), parse_mode=HTML)``. Руководитель по ADMIN_IDS — 1001
  (``fakebot.MANAGER_TG_ID``). AI выключен (AI_PROVIDER=none) — всё работает на правилах.
* ``db`` — фабрика сессий БД бота: ``async with db() as s: ...``.

Переменные окружения выставляются ДО импорта bot.config (и ещё раз — на время каждого теста,
с ``get_settings.cache_clear()``), чтобы Settings бота видели тестовые значения, а не .env.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

E2E_ENV: dict[str, str] = {
    "BOT_TOKEN": "42:TEST",
    "ADMIN_IDS": "1001",
    "AI_PROVIDER": "none",
    "GEMINI_API_KEY": "",
    "GROQ_API_KEY": "",
    "CLOUDFLARE_API_TOKEN": "",
    "CLOUDFLARE_ACCOUNT_ID": "",
    "MISTRAL_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "TIMEZONE": "Asia/Tashkent",
    # Подстраховка: бот в тестах не должен трогать data/bot.db, даже если кто-то создаст engine сам.
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "DATABASE_PASSWORD": "",
}
os.environ.update(E2E_ENV)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from aiogram import Bot, Router  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine  # noqa: E402

from bot.config import get_settings  # noqa: E402

get_settings.cache_clear()

from .fakebot import BotHarness, FakeSession  # noqa: E402


def release_bot_routers() -> None:
    """Отвязать роутеры хендлеров бота от Dispatcher'ов, собранных раньше.

    Роутеры (``router = Router(...)`` в bot/handlers/*.py) — синглтоны модулей, а aiogram
    не даёт подключить роутер ко второму родителю («Router is already attached»). Каждый тест
    собирает свой Dispatcher, поэтому связь «модульный роутер -> объект, созданный при сборке
    (Dispatcher или промежуточный Router)» разрывается. Связи между модульными роутерами
    (include_router внутри модулей) не трогаются.
    """
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


@pytest.fixture
def e2e_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Тестовые настройки бота на время теста (другие тесты могли поменять окружение)."""
    for key, value in E2E_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def app(e2e_env: None, engine: AsyncEngine, storage_engine: AsyncEngine | None) -> AsyncIterator[BotHarness]:
    """Бот целиком (bot.main.build_dispatcher) на фейковом Telegram API и чистой тестовой БД.

    ``engine`` — фикстура tests/conftest.py: in-memory SQLite или (TEST_DATABASE_URL) PostgreSQL;
    таблицы уже созданы, движок закрывается после теста той же фикстурой. На PostgreSQL хранилище
    диалогов, как в bot.main.main, работает через свой пул (фикстура ``storage_engine``).
    """
    from bot.db.base import make_sessionmaker
    from bot.main import build_dispatcher, default_storage  # импорт здесь: сбор тестов не зависит от main.py

    sessionmaker = make_sessionmaker(engine)
    storage = default_storage(make_sessionmaker(storage_engine)) if storage_engine is not None else None
    release_bot_routers()
    dp = build_dispatcher(sessionmaker, storage)
    bot = Bot("42:TEST", session=FakeSession(), default=DefaultBotProperties(parse_mode="HTML"))
    harness = BotHarness(dp, bot, sessionmaker)
    try:
        yield harness
    finally:
        await bot.session.close()
        release_bot_routers()


@pytest.fixture
def db(app: BotHarness) -> Callable[[], object]:
    """Фабрика сессий БД бота: ``async with db() as s: await s.get(Task, 1)``."""
    return app.db
