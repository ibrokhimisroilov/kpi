"""Приложение в Telegram (Mini App) и бот: настройки, маршруты /app и /api в веб-сервере webhook,
кнопка меню чата «Открыть» и кнопка «📱 Открыть приложение» после /start (docs/MINIAPP_SPEC.md §3, §12.3).

* Settings: WEBAPP_ENABLED (по умолчанию да), WEBAPP_DEBUG; webapp_url — только webhook + https +
  включено; webapp_debug_active — только не в webhook.
* bot/web.py: build_web_app монтирует Mini App после маршрутов бота и только при WEBAPP_ENABLED;
  /health, / и webhook работают как раньше и без БД; /api отвечает сам (вход по initData, а не по
  секрету webhook); ошибка внутри пакета приложения не роняет бота; остановка ждёт фоновые задачи
  приложения (не дольше SHUTDOWN_GRACE_SEC).
* bot/main.py: webhook — setChatMenuButton «Открыть» → <PUBLIC_URL>/app (выключено — стандартная
  кнопка; ошибка Telegram — предупреждение, запуск идёт дальше); polling кнопку не трогает, кроме
  TAKEOVER_WEBHOOK=1 (стандартная кнопка); подсказки в журнале при запуске.
* bot/handlers/start.py: /start активного пользователя в webhook — второе сообщение с inline-кнопкой
  WebApp; неактивным, при регистрации, в polling и при WEBAPP_ENABLED=0 — нет.

Telegram — фейковый (tests/e2e/fakebot.py, WebhookApi из tests/test_web.py), сети нет. Проводка web.py
проверяется на подменённом пакете приложения; тесты с настоящим пакетом bot.webapp (его пишет агент
API) пропускаются, только пока пакета нет (real_webapp).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram import methods as m
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardButton, MenuButtonDefault, MenuButtonWebApp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from e2e.fakebot import BotHarness, FakeSession
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_web import (
    BASE,
    HOOK_HEADERS,
    HOOK_PATH,
    HOOK_SECRET,
    TICK_KEY,
    WebhookApi,
    _release_bot_routers,
    free_port,
    make_bot,
    make_settings,
    no_db,
    ping_dispatcher,
    serve,
    update,
)

from bot import main as bot_main
from bot import web as bot_web
from bot.config import Settings
from bot.db.base import make_sessionmaker
from bot.handlers import start
from bot.scheduler import jobs
from bot.ui import keyboards
from bot.ui.texts import BTN_NEW_TASK

APP_URL = f"{BASE}/app"
MGR = 1001  # ADMIN_IDS в tests/conftest.py
EMP = 2001


# --- Настройки (MINIAPP_SPEC §3.1) -------------------------------------------------------------------


def test_settings_webapp_defaults() -> None:
    settings = make_settings()
    assert settings.webapp_enabled is True
    assert settings.webapp_debug is False
    assert settings.webapp_debug_active is False
    assert settings.webapp_url == APP_URL


def test_settings_webapp_from_env(set_env: Callable[..., None]) -> None:
    set_env(WEBAPP_ENABLED="0", WEBAPP_DEBUG="1")
    settings = Settings(_env_file=None)
    assert settings.webapp_enabled is False and settings.webapp_debug is True
    set_env(WEBAPP_ENABLED="1", WEBAPP_DEBUG="0")
    settings = Settings(_env_file=None)
    assert settings.webapp_enabled is True and settings.webapp_debug is False


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, APP_URL),
        ({"public_url": f"{BASE}/"}, APP_URL),
        ({"public_url": "", "render_external_url": "https://kpi.onrender.com/"}, "https://kpi.onrender.com/app"),
        ({"public_url": "HTTPS://KPI.example.com"}, "HTTPS://KPI.example.com/app"),
        ({"run_mode": "polling"}, ""),                       # веб-сервера нет — приложения тоже
        ({"public_url": "http://kpi.example.com"}, ""),      # Telegram открывает Mini App только по https
        ({"public_url": "", "render_external_url": ""}, ""),
        ({"webapp_enabled": False}, ""),
    ],
)
def test_webapp_url(overrides: dict[str, Any], expected: str) -> None:
    settings = make_settings(**overrides)
    assert settings.webapp_url == expected


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"run_mode": "polling", "webapp_debug": True}, True),
        ({"run_mode": "polling", "webapp_debug": False}, False),
        ({"run_mode": "webhook", "webapp_debug": True}, False),  # на Render отладка невозможна
    ],
)
def test_webapp_debug_active(overrides: dict[str, Any], expected: bool) -> None:
    assert make_settings(**overrides).webapp_debug_active is expected


def test_env_example_documents_webapp_settings() -> None:
    """Образец .env (его копирует run.bat) знает обе настройки — закомментированными, со значением по умолчанию."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "# WEBAPP_ENABLED=1" in text.splitlines()
    assert "# WEBAPP_DEBUG=0" in text.splitlines()


def test_open_app_keyboard() -> None:
    markup = keyboards.open_app_kb(APP_URL)
    [[button]] = markup.inline_keyboard
    assert button.text == keyboards.BTN_OPEN_APP == "📱 Открыть приложение"
    assert button.web_app is not None and button.web_app.url == APP_URL
    assert button.callback_data is None and button.url is None


# --- bot/web.py: монтирование (подменённый пакет приложения) ------------------------------------------


class FakeWebapp:
    """Подмена bot.webapp для проверки проводки web.py: /app, sub-app /api и фоновые задачи."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.tasks: set[asyncio.Task[Any]] = set()

    def setup(self, app: web.Application, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("ошибка внутри пакета приложения")

        async def page(request: web.Request) -> web.Response:
            return web.Response(text="mini app", content_type="text/html")

        async def me(request: web.Request) -> web.Response:
            if not request.headers.get("X-Telegram-Init-Data"):
                return web.json_response({"error": "нет входа", "code": "auth_missing"}, status=401)
            return web.json_response({"access": "active"})

        app.router.add_get("/app", page)
        api = web.Application()
        api.router.add_get("/me", me)
        app.add_subapp("/api", api)

    def pending(self, app: web.Application) -> set[asyncio.Task[Any]]:
        return set(self.tasks)


async def settle_updates(app: web.Application) -> None:
    """Дождаться фоновой обработки апдейтов webhook."""
    pending = app[bot_web.RECEIVER].pending()
    if pending:
        await asyncio.wait(pending, timeout=5)


@pytest.fixture
def fake_webapp(monkeypatch: pytest.MonkeyPatch) -> FakeWebapp:
    fake = FakeWebapp()
    monkeypatch.setattr(bot_web, "_webapp_entry", lambda: (fake.setup, fake.pending))
    return fake


async def test_build_web_app_mounts_webapp_next_to_bot_routes(fake_webapp: FakeWebapp) -> None:
    """Mini App подключается к тому же серверу; /health, / и webhook работают как раньше (без БД),
    /api отвечает сам — секрет webhook для него не нужен и не мешает."""
    handled: list[int] = []
    bot = make_bot()
    settings = make_settings()
    app = bot_web.build_web_app(bot, ping_dispatcher(handled), no_db, settings)  # type: ignore[arg-type]
    assert app[bot_web.WEBAPP_MOUNTED] is True
    [call] = fake_webapp.calls
    assert call == {"bot": bot, "sessionmaker": no_db, "settings": settings}
    async with serve(app) as client:
        response = await client.get("/app")
        assert response.status == 200 and await response.text() == "mini app"
        response = await client.get("/api/me")
        assert response.status == 401 and (await response.json())["code"] == "auth_missing"
        response = await client.get("/api/me", headers={"X-Telegram-Init-Data": "x"})
        assert response.status == 200
        response = await client.get("/health")
        assert response.status == 200 and await response.text() == "ok"
        response = await client.get("/")
        assert response.status == 200
        response = await client.post(HOOK_PATH, json=update(1))
        assert response.status == 403
        response = await client.post(HOOK_PATH, json=update(2), headers=HOOK_HEADERS)
        assert response.status == 200
        response = await client.get(f"/tick?key={TICK_KEY}x")
        assert response.status == 403
        await settle_updates(app)
    assert handled == [2]
    await bot.session.close()


async def test_build_web_app_without_webapp_when_disabled(
    fake_webapp: FakeWebapp, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="bot.web")
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings(webapp_enabled=False))  # type: ignore[arg-type]
    assert app[bot_web.WEBAPP_MOUNTED] is False and fake_webapp.calls == []
    assert "Mini App выключен (WEBAPP_ENABLED=0)" in caplog.text
    async with serve(app) as client:
        for path in ("/app", "/app/", "/app/static/app.js", "/api/me"):
            response = await client.get(path)
            assert response.status == 404, path
        response = await client.get("/health")
        assert response.status == 200
    await bot.session.close()


async def test_webapp_failure_does_not_break_the_bot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ошибка внутри пакета приложения — чат работает как раньше (трейсбек в журнале, приложения нет)."""
    broken = FakeWebapp(fail=True)
    monkeypatch.setattr(bot_web, "_webapp_entry", lambda: (broken.setup, broken.pending))
    handled: list[int] = []
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher(handled), no_db, make_settings())  # type: ignore[arg-type]
    assert app[bot_web.WEBAPP_MOUNTED] is False and len(broken.calls) == 1
    assert "Mini App не подключён из-за ошибки" in caplog.text
    async with serve(app) as client:
        assert (await client.get("/health")).status == 200
        assert (await client.post(HOOK_PATH, json=update(5), headers=HOOK_HEADERS)).status == 200
        await settle_updates(app)
    assert handled == [5]
    assert bot_web.webapp_pending(app) == set()
    await bot.session.close()


async def test_webapp_package_missing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(bot_web, "_webapp_entry", lambda: None)
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings())  # type: ignore[arg-type]
    assert app[bot_web.WEBAPP_MOUNTED] is False
    assert "пакета bot.webapp нет" in caplog.text
    async with serve(app) as client:
        assert (await client.get("/app")).status == 404
        assert (await client.get("/health")).status == 200
    await bot.session.close()


async def test_shutdown_waits_for_webapp_background_task(fake_webapp: FakeWebapp) -> None:
    """SIGTERM во время фоновой задачи приложения (оценка сдачи, Excel): сервер её дожидается, затем
    закрывает Dispatcher."""
    order: list[str] = []
    gate = asyncio.Event()
    dp = ping_dispatcher([])

    @dp.shutdown()
    async def on_shutdown() -> None:
        order.append("dispatcher closed")

    async def background_job() -> None:
        await gate.wait()
        order.append("webapp job done")

    bot = make_bot()
    app = bot_web.build_web_app(bot, dp, no_db, make_settings())  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    job = asyncio.create_task(background_job())
    fake_webapp.tasks.add(job)
    assert bot_web.webapp_pending(app) == {job}
    asyncio.get_running_loop().call_later(0.2, gate.set)
    await client.close()
    assert job.done() and not job.cancelled()
    assert order == ["webapp job done", "dispatcher closed"]
    await bot.session.close()


async def test_shutdown_does_not_wait_forever_for_webapp(
    fake_webapp: FakeWebapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot_web, "SHUTDOWN_GRACE_SEC", 0.2)
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings())  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    job = asyncio.create_task(asyncio.Event().wait())  # не закончится никогда
    fake_webapp.tasks.add(job)
    started = time.monotonic()
    await client.close()
    assert time.monotonic() - started < 5
    assert job.cancelled()
    await bot.session.close()


def test_webapp_pending_survives_broken_registry(fake_webapp: FakeWebapp) -> None:
    app = bot_web.build_web_app(make_bot(), ping_dispatcher([]), no_db, make_settings())  # type: ignore[arg-type]

    def broken(app: web.Application) -> set[asyncio.Task[Any]]:
        raise RuntimeError("сбой")

    app[bot_web._WEBAPP_PENDING] = broken
    assert bot_web.webapp_pending(app) == set()


# --- bot/web.py: настоящий пакет bot.webapp ------------------------------------------------------------


def real_webapp() -> Any:
    """Пакет bot.webapp (API + собранный SPA в bot/webapp/static) — часть проекта: без пропусков."""
    import bot.webapp as webapp

    assert callable(webapp.setup_webapp) and callable(webapp.pending_tasks)
    return webapp


async def test_real_webapp_routes_mounted() -> None:
    """С настоящим пакетом: /app отдаёт собранный SPA (index + app.js + app.css), /api без initData — 401
    JSON без обращения к базе; секрет webhook входом в API не является."""
    real_webapp()
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings())  # type: ignore[arg-type]
    assert app[bot_web.WEBAPP_MOUNTED] is True
    async with serve(app) as client:
        for path in ("/app", "/app/"):
            response = await client.get(path)
            assert response.status == 200, path
            assert response.content_type == "text/html"
            page = await response.text()
            assert "__ASSET_VERSION__" not in page and "__KPI_CONFIG__" not in page
            assert "/app/static/app.js?v=" in page and "/app/static/app.css?v=" in page
        assert (await client.head("/app")).status == 200
        for name, kind in (("app.js", "text/javascript"), ("app.css", "text/css")):
            response = await client.get(f"/app/static/{name}")
            assert response.status == 200 and response.content_type == kind, name
        for headers in ({}, HOOK_HEADERS):
            response = await client.get("/api/me", headers=headers)
            assert response.status == 401
            body = await response.json()
            assert body["code"] == "auth_missing" and body["error"]
        response = await client.get("/health")
        assert response.status == 200 and await response.text() == "ok"
        response = await client.post(HOOK_PATH, json=update(7))
        assert response.status == 403
    await bot.session.close()


async def test_real_webapp_not_mounted_when_disabled() -> None:
    real_webapp()
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings(webapp_enabled=False))  # type: ignore[arg-type]
    assert app[bot_web.WEBAPP_MOUNTED] is False
    async with serve(app) as client:
        for path in ("/app", "/app/", "/app/static/app.js", "/api/me", "/api/tasks"):
            assert (await client.get(path)).status == 404, path
        assert (await client.get("/health")).status == 200
    await bot.session.close()


async def test_real_webapp_background_task_awaited_on_shutdown() -> None:
    """Фоновая задача из реестра приложения (TaskRegistry) — в webapp_pending; остановка её дожидается."""
    webapp = real_webapp()
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings())  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    gate = asyncio.Event()
    done: list[str] = []

    async def job() -> None:
        await gate.wait()
        done.append("ok")

    try:
        task = app[webapp.TASKS].spawn(job(), name="webapp-test-job")
        assert task in bot_web.webapp_pending(app)
        asyncio.get_running_loop().call_later(0.2, gate.set)
    finally:
        await client.close()
        await bot.session.close()
    assert done == ["ok"] and task.done()


# --- bot/main.py: кнопка меню чата ---------------------------------------------------------------------


class MenuApi(WebhookApi):
    """WebhookApi, у которого setChatMenuButton может падать (fail_menu)."""

    def __init__(self, *, fail_menu: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail_menu = fail_menu

    async def _handle(self, bot: Bot, method: TelegramMethod[Any], call: Any) -> Any:
        if isinstance(method, m.SetChatMenuButton) and self.fail_menu:
            raise TelegramBadRequest(method=method, message="Bad Request: menu button is invalid")
        return await super()._handle(bot, method, call)

    def menu_calls(self) -> list[m.SetChatMenuButton]:
        return [request for request in self.requests if isinstance(request, m.SetChatMenuButton)]

    def names(self) -> list[str]:
        return [type(request).__name__ for request in self.requests]


async def _until(check: Callable[[], bool], task: asyncio.Task[Any], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if task.done():
            task.result()  # исключение запуска — сразу в тест
            raise AssertionError("запуск завершился раньше времени")
        if time.monotonic() > deadline:
            raise AssertionError("не дождались")
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def running_webhook(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, api: WebhookApi
) -> AsyncIterator[web.Application]:
    """bot_main._run_webhook на 127.0.0.1 до выхода из блока (фоновый цикл ничего не запускает)."""
    built: list[web.Application] = []
    real_build = bot_main.build_web_app

    def build(*args: Any, **kwargs: Any) -> web.Application:
        built.append(real_build(*args, **kwargs))
        return built[-1]

    real_site = web.TCPSite

    def local_site(runner: web.BaseRunner, host: str, port: int, **kwargs: Any) -> web.TCPSite:
        return real_site(runner, "127.0.0.1", port, **kwargs)  # без запроса брандмауэра Windows

    stop = asyncio.Event()

    async def wait_for_stop() -> None:
        await stop.wait()

    async def no_jobs(*args: Any, **kwargs: Any) -> dict[str, object]:
        return {}

    monkeypatch.setattr(bot_main, "build_web_app", build)
    monkeypatch.setattr(bot_main.web, "TCPSite", local_site)
    monkeypatch.setattr(bot_main, "_wait_for_stop", wait_for_stop)
    monkeypatch.setattr(bot_web, "JOB_FIRST_DELAY_SEC", 3600)
    monkeypatch.setattr(bot_web, "KEEPALIVE_FIRST_DELAY_SEC", 3600)
    monkeypatch.setattr(jobs, "run_due_jobs", no_jobs)
    bot = make_bot(api)
    task = asyncio.create_task(bot_main._run_webhook(settings, bot, ping_dispatcher([]), no_db))  # type: ignore[arg-type]
    try:
        await _until(lambda: bool(built) and built[0][bot_web.BACKGROUND].running, task)
        yield built[0]
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
        await bot.session.close()


async def test_run_webhook_sets_menu_button_to_webapp(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Запуск в режиме webhook: кнопка меню чата «Открыть» → <PUBLIC_URL>/app для всех личных чатов
    (без chat_id) — после webhook и команд; секретов в журнале нет."""
    caplog.set_level(logging.INFO)
    api = MenuApi()
    async with running_webhook(monkeypatch, make_settings(port=free_port()), api):
        [call] = api.menu_calls()
    assert call.chat_id is None
    button = call.menu_button
    assert isinstance(button, MenuButtonWebApp)
    assert button.text == bot_main.MENU_BUTTON_TEXT == "Открыть"
    assert button.web_app.url == APP_URL
    names = api.names()
    assert names[:3] == ["GetMe", "GetWebhookInfo", "SetWebhook"]
    assert names.index("SetMyCommands") < names.index("SetChatMenuButton")
    assert HOOK_SECRET not in caplog.text and TICK_KEY not in caplog.text


async def test_run_webhook_sets_default_menu_button_when_webapp_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """WEBAPP_ENABLED=0 — стандартная кнопка (раньше поставленная «Открыть» убирается)."""
    api = MenuApi()
    async with running_webhook(monkeypatch, make_settings(port=free_port(), webapp_enabled=False), api):
        [call] = api.menu_calls()
    assert isinstance(call.menu_button, MenuButtonDefault) and call.chat_id is None


async def test_run_webhook_continues_when_menu_button_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Telegram не принял кнопку меню — предупреждение в журнале (тип ошибки), бот работает дальше."""
    caplog.set_level(logging.INFO)
    api = MenuApi(fail_menu=True)
    async with running_webhook(monkeypatch, make_settings(port=free_port()), api) as app:
        assert len(api.menu_calls()) == 1
        assert app[bot_web.BACKGROUND].running
    assert "Не удалось настроить кнопку меню чата: TelegramBadRequest" in caplog.text


async def test_setup_menu_button_directly() -> None:
    api = MenuApi()
    bot = make_bot(api)
    try:
        await bot_main._setup_menu_button(bot, make_settings())
        await bot_main._setup_menu_button(bot, make_settings(run_mode="polling"))
        await bot_main._setup_menu_button(bot, make_settings(public_url="http://kpi.example.com"))
    finally:
        await bot.session.close()
    first, polling, plain_http = (call.menu_button for call in api.menu_calls())
    assert isinstance(first, MenuButtonWebApp) and first.web_app.url == APP_URL
    assert isinstance(polling, MenuButtonDefault) and isinstance(plain_http, MenuButtonDefault)


async def _run_polling_until_updates(
    api: WebhookApi, sessionmaker: async_sessionmaker[AsyncSession], *, takeover: bool = False
) -> None:
    bot = make_bot(api)
    dp = ping_dispatcher([])
    try:
        task = asyncio.create_task(bot_main._run_polling(bot, dp, sessionmaker, takeover_webhook=takeover))
        await _until(lambda: any(isinstance(r, m.GetUpdates) for r in api.requests), task)
        await dp.stop_polling()
        await asyncio.wait_for(task, timeout=10)
    finally:
        await bot.session.close()


async def test_polling_does_not_touch_menu_button(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """polling (свой ПК): публичного https нет — кнопку меню не трогаем, чат как раньше."""
    api = MenuApi()
    await _run_polling_until_updates(api, sessionmaker)
    assert api.menu_calls() == []
    assert api.names()[:3] == ["GetMe", "GetWebhookInfo", "SetMyCommands"]


async def test_polling_takeover_resets_menu_button(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """TAKEOVER_WEBHOOK=1: облачная копия выведена из работы — её «Открыть» заменяется стандартной кнопкой
    (после команд: порядок первых запросов polling прежний)."""
    api = MenuApi(url=f"{BASE}/tg/{HOOK_SECRET}")
    await _run_polling_until_updates(api, sessionmaker, takeover=True)
    [call] = api.menu_calls()
    assert isinstance(call.menu_button, MenuButtonDefault) and call.chat_id is None
    # Команды ставятся дважды: общий список и список для узбекского языка Telegram.
    assert api.names()[:6] == [
        "GetMe", "GetWebhookInfo", "DeleteWebhook", "SetMyCommands", "SetMyCommands", "SetChatMenuButton",
    ]


async def test_polling_takeover_menu_button_error_is_not_fatal(
    sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    api = MenuApi(url=f"{BASE}/tg/{HOOK_SECRET}", fail_menu=True)
    await _run_polling_until_updates(api, sessionmaker, takeover=True)
    assert any(isinstance(r, m.GetUpdates) for r in api.requests)  # polling работает
    assert len(api.menu_calls()) == 1
    assert "Не удалось вернуть стандартную кнопку меню чата: TelegramBadRequest" in caplog.text


async def test_drop_webhook_reports_whether_webhook_was_removed() -> None:
    api = MenuApi()
    bot = make_bot(api)
    try:
        assert await bot_main._drop_webhook(bot, takeover=True) is False  # webhook не было
        api.webhook_url = f"{BASE}/tg/{HOOK_SECRET}"
        assert await bot_main._drop_webhook(bot, takeover=True) is True
        assert api.webhook_url == ""
    finally:
        await bot.session.close()


def test_startup_hints_about_webapp(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    bot_main._log_startup_hints(make_settings())
    assert f"Mini App: {APP_URL} (кнопка «Открыть» в чате)" in caplog.text
    assert "WEBAPP_DEBUG" not in caplog.text
    caplog.clear()

    bot_main._log_startup_hints(make_settings(webapp_enabled=False))
    assert "Mini App выключен (WEBAPP_ENABLED=0)" in caplog.text
    caplog.clear()

    bot_main._log_startup_hints(make_settings(webapp_debug=True))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "WEBAPP_DEBUG игнорируется в режиме webhook" in warnings
    caplog.clear()

    bot_main._log_startup_hints(make_settings(run_mode="polling", webapp_debug=True))
    assert "Mini App" not in caplog.text and "WEBAPP_DEBUG" not in caplog.text
    assert HOOK_SECRET not in caplog.text and TICK_KEY not in caplog.text


# --- bot/handlers/start.py: кнопка «📱 Открыть приложение» после /start ----------------------------------


@pytest_asyncio.fixture
async def chat(engine: AsyncEngine, storage_engine: AsyncEngine | None) -> AsyncIterator[BotHarness]:
    """Бот целиком (build_dispatcher) на фейковом Telegram и тестовой БД — как фикстура app в tests/e2e."""
    from bot.main import build_dispatcher, default_storage

    sessionmaker = make_sessionmaker(engine)
    storage = default_storage(make_sessionmaker(storage_engine)) if storage_engine is not None else None
    _release_bot_routers()
    dp = build_dispatcher(sessionmaker, storage)
    bot = Bot("42:TEST", session=FakeSession(), default=DefaultBotProperties(parse_mode="HTML"))
    harness = BotHarness(dp, bot, sessionmaker)
    try:
        yield harness
    finally:
        await bot.session.close()
        _release_bot_routers()


@pytest.fixture
def webhook_env(set_env: Callable[..., None]) -> None:
    """Настройки бота на Render: webhook, публичный https-адрес, приложение включено."""
    set_env(RUN_MODE="webhook", PUBLIC_URL=BASE, RENDER_EXTERNAL_URL="", WEBAPP_ENABLED="1")


def app_buttons(h: BotHarness, chat_id: int) -> list[InlineKeyboardButton]:
    return [button for message in h.messages(chat_id) for button in message.buttons if button.web_app is not None]


async def test_start_manager_gets_app_button_in_webhook_mode(chat: BotHarness, webhook_env: None) -> None:
    h = chat
    await h.send_command(MGR, "start", first_name="Анна")
    messages = h.messages(MGR)
    assert len(messages) == 2
    greeting, app_message = messages
    assert "начальник" in greeting.text
    assert BTN_NEW_TASK in (h.reply_keyboard(MGR) or [])  # главное меню осталось на месте
    assert app_message.text == start.TXT_APP_MANAGER
    [button] = app_message.buttons
    assert button.text == "📱 Открыть приложение"
    assert button.web_app is not None and button.web_app.url == APP_URL


async def test_start_employee_gets_app_button_in_webhook_mode(chat: BotHarness, webhook_env: None) -> None:
    h = chat
    await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    await h.send_command(EMP, "start")
    assert h.last_text(EMP) == start.TXT_APP_EMPLOYEE
    [button] = app_buttons(h, EMP)
    assert button.web_app is not None and button.web_app.url == APP_URL
    assert len(h.messages(EMP)) == 2


@pytest.mark.parametrize(
    ("status", "full_name", "expected"),
    [
        ("pending", "Петров Пётр Петрович", start.TXT_PENDING),
        ("blocked", "Петров Пётр Петрович", start.TXT_BLOCKED),
        (None, None, None),  # новый пользователь — анкета регистрации
    ],
)
async def test_start_inactive_users_get_no_app_button(
    chat: BotHarness, webhook_env: None, status: str | None, full_name: str | None, expected: str | None
) -> None:
    h = chat
    if status is not None:
        assert full_name is not None
        await h.seed_user(EMP, full_name, status=status)
    await h.send_command(EMP, "start", first_name="Пётр")
    assert app_buttons(h, EMP) == []
    # Новому человеку перед анкетой приходит ещё и выбор языка.
    assert len(h.messages(EMP)) == (1 if status is not None else 2)
    if expected is not None:
        assert h.last_text(EMP) == expected
    else:
        assert "Шаг 1 из 2" in (h.last_text(EMP) or "")


async def test_start_in_polling_mode_has_no_app_button(chat: BotHarness) -> None:
    """polling (по умолчанию) — /start как раньше: одно сообщение, никаких кнопок приложения."""
    h = chat
    await h.send_command(MGR, "start", first_name="Анна")
    await h.seed_user(EMP, "Иванов Иван Иванович")
    await h.send_command(EMP, "start")
    assert app_buttons(h, MGR) == [] and app_buttons(h, EMP) == []
    assert len(h.messages(MGR)) == 1 and len(h.messages(EMP)) == 1


async def test_start_without_app_button_when_webapp_disabled(
    chat: BotHarness, set_env: Callable[..., None]
) -> None:
    set_env(RUN_MODE="webhook", PUBLIC_URL=BASE, RENDER_EXTERNAL_URL="", WEBAPP_ENABLED="0")
    h = chat
    await h.send_command(MGR, "start", first_name="Анна")
    assert app_buttons(h, MGR) == [] and len(h.messages(MGR)) == 1


async def test_start_app_button_after_registration_approval(chat: BotHarness, webhook_env: None) -> None:
    """Сотрудника подтвердили — его следующий /start уже с кнопкой приложения; /menu и /help не меняются."""
    h = chat
    await h.seed_user(EMP, "Иванов Иван Иванович", status="pending")
    await h.send_command(EMP, "start")
    assert app_buttons(h, EMP) == []
    async with h.db() as session:
        from sqlalchemy import update as sql_update

        from bot.db.models import User, UserStatus

        await session.execute(sql_update(User).where(User.tg_id == EMP).values(status=UserStatus.ACTIVE))
        await session.commit()
    await h.send_command(EMP, "start")
    assert len(app_buttons(h, EMP)) == 1
    before = len(h.messages(EMP))
    await h.send_command(EMP, "menu")
    await h.send_command(EMP, "help")
    assert len(app_buttons(h, EMP)) == 1 and len(h.messages(EMP)) == before + 2


# --- Кнопка меню чата на языке человека (SPEC.md §14) --------------------------------------------


def _menu_buttons(h: BotHarness) -> list[m.SetChatMenuButton]:
    return [request for request in h.requests if isinstance(request, m.SetChatMenuButton)]


async def test_uzbek_user_gets_own_menu_button_and_russian_gets_the_common_one(
    chat: BotHarness, webhook_env: None
) -> None:
    """Выбрал узбекский — кнопка меню его чата «Ochish» открывает то же приложение; вернулся на русский —
    ему снова показывается общая кнопка бота «Открыть»."""
    h = chat
    await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    await h.send_command(EMP, "start")
    assert _menu_buttons(h) == []  # язык не меняли — кнопку не трогаем

    await h.send_command(EMP, "lang")
    await h.press_button(EMP, "Oʻzbekcha")
    [call] = _menu_buttons(h)
    assert call.chat_id == EMP and isinstance(call.menu_button, MenuButtonWebApp)
    assert call.menu_button.text == "Ochish" and call.menu_button.web_app.url == APP_URL

    await h.send_command(EMP, "lang")
    await h.press_button(EMP, "Русский")
    back = _menu_buttons(h)[-1]
    assert back.chat_id == EMP and isinstance(back.menu_button, MenuButtonDefault)


async def test_menu_button_is_left_alone_without_the_app(chat: BotHarness) -> None:
    """Приложения нет (не webhook) — смена языка кнопку меню не трогает."""
    h = chat
    await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    await h.send_command(EMP, "lang")
    await h.press_button(EMP, "Oʻzbekcha")
    assert _menu_buttons(h) == []
