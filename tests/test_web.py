"""Режим webhook: веб-сервер бота (bot/web.py) и запуск в режимах webhook / polling (bot/main.py).

Сервер aiohttp поднимается на 127.0.0.1 (aiohttp.test_utils), Telegram — фейковый
(tests/e2e/fakebot.py FakeSession + getWebhookInfo/setWebhook/deleteWebhook): никакой сети.

* POST /tg/<секрет>: без верного заголовка X-Telegram-Bot-Api-Secret-Token — 403; с верным —
  200 сразу, апдейт обрабатывается в фоне (долгий хендлер не задерживает ответ Telegram),
  повтор апдейта с тем же update_id не обрабатывается дважды; настоящий бот отвечает на /start.
* GET /tick: неверный ключ — 403; верный — 200 мгновенно, задания в фоне; пока они идут,
  новые не запускаются.
* /health и / — без БД; остановка дожидается апдейтов в работе.
* setWebhook — только если у Telegram другой адрес/секрет/типы апдейтов; polling отключает webhook.
* Запуск webhook включает фоновый цикл (самопробуждение и задания по расписанию), остановка его
  выключает; подробно цикл проверяется в tests/test_keepalive.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import sys
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp
import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram import methods as m
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.types import Message, WebhookInfo
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from e2e.fakebot import FakeSession
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import main as bot_main
from bot import web as bot_web
from bot.config import Settings
from bot.db.models import Role, User, UserStatus
from bot.scheduler import jobs

BASE = "https://kpi-bot.example.com"
HOOK_SECRET = "hook_Secret-123"
TICK_KEY = "tick-key-456"
EMP = 2001
MGR = 1001  # ADMIN_IDS в tests/conftest.py


def make_settings(**overrides: Any) -> Settings:
    """Настройки режима webhook без .env разработчика."""
    values: dict[str, Any] = {
        "bot_token": "42:TEST",
        "run_mode": "webhook",
        "public_url": BASE,
        "render_external_url": "",
        "webhook_secret": HOOK_SECRET,
        "tick_secret": TICK_KEY,
        "port": 8080,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class WebhookApi(FakeSession):
    """FakeSession + методы webhook, как у Telegram (getWebhookInfo помнит setWebhook)."""

    def __init__(self, *, url: str = "", allowed_updates: list[str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.webhook_url = url
        self.webhook_allowed = allowed_updates
        self.webhook_secret: str | None = None
        self.set_calls: list[m.SetWebhook] = []
        self.delete_calls: list[m.DeleteWebhook] = []

    async def _handle(self, bot: Bot, method: TelegramMethod[Any], call: Any) -> Any:
        if isinstance(method, m.GetWebhookInfo):
            return WebhookInfo(
                url=self.webhook_url,
                has_custom_certificate=False,
                pending_update_count=0,
                allowed_updates=self.webhook_allowed,
            )
        if isinstance(method, m.SetWebhook):
            self.set_calls.append(method)
            self.webhook_url, self.webhook_allowed = method.url, method.allowed_updates
            self.webhook_secret = method.secret_token
            return True
        if isinstance(method, m.DeleteWebhook):
            self.delete_calls.append(method)
            self.webhook_url, self.webhook_allowed = "", None
            return True
        if isinstance(method, m.GetUpdates):
            await asyncio.sleep(0.01)  # как long polling: не крутить цикл без пауз
            return []
        return await super()._handle(bot, method, call)


def make_bot(api: FakeSession | None = None) -> Bot:
    return Bot("42:TEST", session=api or WebhookApi(), default=DefaultBotProperties(parse_mode="HTML"))


def no_db() -> Any:
    raise AssertionError("этот маршрут не должен обращаться к базе")


def ping_dispatcher(handled: list[int], gate: asyncio.Event | None = None) -> Dispatcher:
    """Мини-бот: на /ping отвечает «pong» (gate — задержать хендлер, как долгая AI-оценка)."""
    dp = Dispatcher(storage=MemoryStorage())
    router = Router()

    @router.message(Command("ping"))
    async def ping(message: Message) -> None:
        if gate is not None:
            await gate.wait()
        handled.append(message.message_id)
        await message.answer("pong")

    dp.include_router(router)
    return dp


def update(update_id: int, text: str = "/ping", user_id: int = EMP) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id,
        "date": 1790000000,
        "chat": {"id": user_id, "type": "private", "first_name": "Иван"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Иван"},
        "text": text,
    }
    if text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"update_id": update_id, "message": message}


HOOK_PATH = f"/tg/{HOOK_SECRET}"
HOOK_HEADERS = {bot_web.SECRET_HEADER: HOOK_SECRET}


@contextlib.asynccontextmanager
async def serve(app: web.Application) -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def settle(app: web.Application, timeout: float = 5) -> None:
    """Дождаться фоновой обработки апдейтов и заданий /tick."""
    pending = app[bot_web.RECEIVER].pending() | app[bot_web.TICKER].pending()
    if pending:
        await asyncio.wait(pending, timeout=timeout)


def texts_to(bot: Bot, chat_id: int) -> list[str]:
    api = bot.session
    assert isinstance(api, FakeSession)
    return [
        request.text
        for request in api.requests
        if isinstance(request, m.SendMessage) and request.chat_id == chat_id
    ]


# --- /health и / --------------------------------------------------------------------------------


async def test_health_and_index_without_database() -> None:
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings())  # type: ignore[arg-type]
    async with serve(app) as client:
        response = await client.get("/health")
        assert response.status == 200 and await response.text() == "ok"
        response = await client.head("/health")
        assert response.status == 200
        response = await client.get("/")
        assert response.status == 200 and "KPI bot is running" in await response.text()
        response = await client.head("/")
        assert response.status == 200
    await bot.session.close()


# --- Webhook ------------------------------------------------------------------------------------


async def test_webhook_rejects_missing_or_wrong_secret() -> None:
    handled: list[int] = []
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher(handled), no_db, make_settings())  # type: ignore[arg-type]
    async with serve(app) as client:
        response = await client.post(HOOK_PATH, json=update(1))
        assert response.status == 403
        response = await client.post(HOOK_PATH, json=update(2), headers={bot_web.SECRET_HEADER: "wrong"})
        assert response.status == 403
        response = await client.post(HOOK_PATH, json=update(3), headers={bot_web.SECRET_HEADER: "пароль"})
        assert response.status == 403  # не-ASCII в заголовке — не падение, а отказ
        response = await client.post("/tg/guess", json=update(4), headers=HOOK_HEADERS)
        assert response.status == 404
        response = await client.get(HOOK_PATH)
        assert response.status == 405
        await settle(app)
    assert handled == []
    assert texts_to(bot, EMP) == []
    await bot.session.close()


async def test_webhook_update_reaches_handler() -> None:
    handled: list[int] = []
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher(handled), no_db, make_settings())  # type: ignore[arg-type]
    async with serve(app) as client:
        response = await client.post(HOOK_PATH, json=update(10), headers=HOOK_HEADERS)
        assert response.status == 200 and await response.json() == {}
        await settle(app)
    assert handled == [10]
    assert texts_to(bot, EMP) == ["pong"]
    await bot.session.close()


async def test_webhook_answers_telegram_before_slow_handler_finishes() -> None:
    """Хендлер работает долго (AI-оценка) — Telegram получает 200 сразу, обработка идёт в фоне."""
    handled: list[int] = []
    gate = asyncio.Event()
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher(handled, gate), no_db, make_settings())  # type: ignore[arg-type]
    async with serve(app) as client:
        started = time.monotonic()
        response = await client.post(HOOK_PATH, json=update(20), headers=HOOK_HEADERS)
        assert response.status == 200
        assert time.monotonic() - started < 2
        await asyncio.sleep(0.05)
        assert handled == [] and len(app[bot_web.RECEIVER].pending()) == 1
        gate.set()
        await settle(app)
    assert handled == [20] and texts_to(bot, EMP) == ["pong"]
    await bot.session.close()


async def test_webhook_repeated_update_processed_once() -> None:
    """Telegram не дождался ответа и прислал тот же апдейт снова — он не обрабатывается второй раз."""
    handled: list[int] = []
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher(handled), no_db, make_settings())  # type: ignore[arg-type]
    async with serve(app) as client:
        for _ in range(2):
            response = await client.post(HOOK_PATH, json=update(30), headers=HOOK_HEADERS)
            assert response.status == 200
        response = await client.post(HOOK_PATH, json=update(31), headers=HOOK_HEADERS)
        assert response.status == 200
        await settle(app)
    assert sorted(handled) == [30, 31]
    await bot.session.close()


@pytest.mark.filterwarnings("ignore:Detected unknown update type")
async def test_webhook_bad_body() -> None:
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings())  # type: ignore[arg-type]
    async with serve(app) as client:
        response = await client.post(HOOK_PATH, data=b"not json", headers=HOOK_HEADERS)
        assert response.status == 400
        response = await client.post(HOOK_PATH, json=[1, 2], headers=HOOK_HEADERS)
        assert response.status == 400
        # Апдейт неизвестного вида Telegram не должен получать ошибку (иначе будет слать его снова).
        response = await client.post(HOOK_PATH, json={"update_id": 40}, headers=HOOK_HEADERS)
        assert response.status == 200
        await settle(app)
    await bot.session.close()


def _release_bot_routers() -> None:
    """Отвязать модульные роутеры бота от Dispatcher'ов, собранных раньше (как в tests/e2e/conftest.py)."""
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
        router._parent_router = None


async def test_real_bot_answers_start_via_webhook(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Настоящий Dispatcher бота (build_dispatcher) за webhook: руководитель из ADMIN_IDS пишет /start —
    бот регистрирует его руководителем и показывает меню."""
    _release_bot_routers()
    try:
        dp = bot_main.build_dispatcher(sessionmaker)
        bot = make_bot()
        app = bot_web.build_web_app(bot, dp, sessionmaker, make_settings())
        async with serve(app) as client:
            response = await client.post(HOOK_PATH, json=update(50, "/start", MGR), headers=HOOK_HEADERS)
            assert response.status == 200
            await settle(app)
        assert texts_to(bot, MGR), "бот не ответил на /start"
        async with sessionmaker() as session:
            user = await session.scalar(select(User).where(User.tg_id == MGR))
        assert user is not None and user.role == Role.MANAGER and user.status == UserStatus.ACTIVE
        await bot.session.close()
    finally:
        _release_bot_routers()


# --- /tick ---------------------------------------------------------------------------------------


class SlowJobs:
    """Подмена run_due_jobs: считает вызовы и ждёт gate (долгие задания)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.gate = asyncio.Event()
        self.fail = False

    async def __call__(self, bot: Bot, sessionmaker: Any, now: Any = None) -> dict[str, object]:
        self.calls.append((bot, sessionmaker))
        await self.gate.wait()
        if self.fail:
            raise RuntimeError("сбой")
        return {"reminders": 1, "digest": "not_due", "backup": "done"}


@pytest.fixture
def slow_jobs(monkeypatch: pytest.MonkeyPatch) -> SlowJobs:
    fake = SlowJobs()
    monkeypatch.setattr(jobs, "run_due_jobs", fake)
    return fake


async def test_tick_rejects_wrong_key(slow_jobs: SlowJobs) -> None:
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings())  # type: ignore[arg-type]
    async with serve(app) as client:
        for query in ("", "?key=", "?key=wrong", f"?key={TICK_KEY}x", "?key=ключ", f"?other={TICK_KEY}"):
            response = await client.get("/tick" + query)
            assert response.status == 403, query
    assert slow_jobs.calls == []
    await bot.session.close()


async def test_tick_answers_fast_and_runs_one_job_at_a_time(slow_jobs: SlowJobs) -> None:
    """Задания идут долго — /tick отвечает сразу; повторный вызов, пока они не закончились, нового
    запуска не начинает; после окончания следующий вызов запускает их снова."""
    bot = make_bot()
    marker = object()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), marker, make_settings())  # type: ignore[arg-type]
    async with serve(app) as client:
        started = time.monotonic()
        response = await client.get(f"/tick?key={TICK_KEY}")
        assert response.status == 200
        assert time.monotonic() - started < 2
        body = await response.json()
        assert body["ok"] is True and body["status"] == "started" and body["last_result"] is None

        await asyncio.sleep(0.05)
        response = await client.get(f"/tick?key={TICK_KEY}")
        assert response.status == 200 and (await response.json())["status"] == "busy"
        assert len(slow_jobs.calls) == 1 and slow_jobs.calls[0] == (bot, marker)

        slow_jobs.gate.set()
        await settle(app)
        response = await client.get(f"/tick?key={TICK_KEY}")
        body = await response.json()
        assert body["status"] == "started"
        assert body["last_result"] == {"reminders": 1, "digest": "not_due", "backup": "done"}
        assert body["last_finished"]
        await settle(app)
    assert len(slow_jobs.calls) == 2
    await bot.session.close()


async def test_tick_failure_does_not_block_next_tick(slow_jobs: SlowJobs) -> None:
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings())  # type: ignore[arg-type]
    slow_jobs.fail = True
    slow_jobs.gate.set()
    async with serve(app) as client:
        assert (await (await client.get(f"/tick?key={TICK_KEY}")).json())["status"] == "started"
        await settle(app)
        body = await (await client.get(f"/tick?key={TICK_KEY}")).json()
        assert body["status"] == "started" and body["last_result"] == {"error": "exception"}
        await settle(app)
    assert len(slow_jobs.calls) == 2
    await bot.session.close()


async def test_hung_tick_is_interrupted_by_timeout(slow_jobs: SlowJobs) -> None:
    """Зависшие задания (например, БД не отвечает) прерываются по таймауту и не блокируют будильник."""
    runner = bot_web.TickRunner(make_bot(), no_db, timeout=0.1)  # type: ignore[arg-type]
    assert runner.start() is True
    assert runner.start() is False
    assert runner.task is not None
    await asyncio.wait_for(runner.task, timeout=5)
    assert runner.last_result == {"error": "timeout"} and not runner.running
    assert runner.start() is True
    slow_jobs.gate.set()
    await asyncio.wait_for(runner.task, timeout=5)
    await runner.bot.session.close()


# --- Остановка ------------------------------------------------------------------------------------


async def test_shutdown_waits_for_update_in_progress() -> None:
    """SIGTERM во время обработки апдейта: сервер дожидается её, затем закрывает Dispatcher."""
    handled: list[int] = []
    order: list[str] = []
    gate = asyncio.Event()
    dp = ping_dispatcher(handled, gate)

    @dp.shutdown()
    async def on_shutdown() -> None:
        order.append(f"shutdown after {handled}")

    bot = make_bot()
    app = bot_web.build_web_app(bot, dp, no_db, make_settings())  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    response = await client.post(HOOK_PATH, json=update(60), headers=HOOK_HEADERS)
    assert response.status == 200
    asyncio.get_running_loop().call_later(0.2, gate.set)
    await client.close()
    assert handled == [60]
    assert order == ["shutdown after [60]"]
    await bot.session.close()


async def test_shutdown_does_not_wait_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_web, "SHUTDOWN_GRACE_SEC", 0.2)
    handled: list[int] = []
    bot = make_bot()
    dp = ping_dispatcher(handled, asyncio.Event())  # хендлер не закончится никогда
    app = bot_web.build_web_app(bot, dp, no_db, make_settings())  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    await client.post(HOOK_PATH, json=update(70), headers=HOOK_HEADERS)
    started = time.monotonic()
    await client.close()
    assert time.monotonic() - started < 5
    assert handled == [] and not app[bot_web.RECEIVER].pending()
    await bot.session.close()


# --- setWebhook только при изменениях --------------------------------------------------------------


async def test_ensure_webhook_sets_only_when_changed() -> None:
    api = WebhookApi()
    bot = make_bot(api)
    dp = ping_dispatcher([])
    allowed = sorted(dp.resolve_used_update_types())
    settings = make_settings()

    assert await bot_web.ensure_webhook(bot, dp, settings) is True
    [call] = api.set_calls
    assert call.url == f"{BASE}/tg/{HOOK_SECRET}"
    assert call.secret_token == HOOK_SECRET
    assert call.allowed_updates == allowed
    assert call.drop_pending_updates is False

    # Перезапуск (или второй экземпляр при обновлении) — у Telegram уже этот адрес: не трогаем.
    assert await bot_web.ensure_webhook(bot, dp, settings) is False
    assert await bot_web.ensure_webhook(bot, dp, make_settings()) is False
    assert len(api.set_calls) == 1

    # Сменился секрет (он часть адреса) — адрес обновляется.
    assert await bot_web.ensure_webhook(bot, dp, make_settings(webhook_secret="new_secret")) is True
    assert api.set_calls[-1].url == f"{BASE}/tg/new_secret" and api.set_calls[-1].secret_token == "new_secret"

    # Сменился адрес бота (Render: RENDER_EXTERNAL_URL без PUBLIC_URL).
    moved = make_settings(webhook_secret="new_secret", public_url="", render_external_url="https://kpi.onrender.com/")
    assert await bot_web.ensure_webhook(bot, dp, moved) is True
    assert api.set_calls[-1].url == "https://kpi.onrender.com/tg/new_secret"

    # Типы апдейтов у Telegram другие (новая версия бота обрабатывает больше) — обновляем.
    api.webhook_allowed = ["callback_query"]
    assert await bot_web.ensure_webhook(bot, dp, moved) is True
    assert len(api.set_calls) == 4
    assert not api.delete_calls
    await bot.session.close()


async def test_ensure_webhook_with_derived_secret() -> None:
    """WEBHOOK_SECRET не задан — секрет выводится из BOT_TOKEN: годится для Telegram и стабилен."""
    api = WebhookApi()
    bot = make_bot(api)
    settings = make_settings(webhook_secret="")
    secret = settings.webhook_secret_value
    assert secret and secret.isalnum() and secret == make_settings(webhook_secret="").webhook_secret_value
    assert await bot_web.ensure_webhook(bot, ping_dispatcher([]), settings) is True
    assert api.set_calls[0].url == f"{BASE}/tg/{secret}" and api.set_calls[0].secret_token == secret
    await bot.session.close()


def test_secrets_equal() -> None:
    assert bot_web.secrets_equal("abc", "abc")
    assert not bot_web.secrets_equal("abd", "abc")
    assert not bot_web.secrets_equal("", "abc")
    assert not bot_web.secrets_equal("", "")
    assert not bot_web.secrets_equal("ключ", "abc")
    # Заголовок с недопустимыми байтами (aiohttp: utf-8 + surrogateescape) — не ошибка, а «не совпало».
    assert not bot_web.secrets_equal(b"\xff\xfe".decode("utf-8", "surrogateescape"), "abc")
    assert not bot_web.secrets_equal("\ud800", "abc")


async def test_webhook_secret_header_with_invalid_bytes_gets_403(caplog: pytest.LogCaptureFixture) -> None:
    """Сырые байты \\xff в секретном заголовке (так может прислать кто угодно) — ответ 403, а не 500
    с трейсбеком в журнале: журнал Render владелец отправляет разработчику, засорять его нельзя."""
    handled: list[int] = []
    app = bot_web.build_web_app(make_bot(), ping_dispatcher(handled), no_db, make_settings())  # type: ignore[arg-type]
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    caplog.set_level(logging.INFO)
    try:
        body = b'{"update_id": 1}'
        head = (
            f"POST /tg/{HOOK_SECRET} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n"
        ).encode()
        secret_line = bot_web.SECRET_HEADER.encode() + b": \xff\xfe" + HOOK_SECRET.encode() + b"\r\n\r\n"
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(head + secret_line + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=10)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        await server.close()
    status_line = response.split(b"\r\n", 1)[0]
    assert status_line.split()[1:2] == [b"403"], status_line
    assert not handled
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


# --- main.py: проверка настроек и запуск ------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"public_url": "", "render_external_url": ""}, "PUBLIC_URL"),
        ({"public_url": "http://kpi.example.com"}, "https://"),
        ({"webhook_secret": "секрет с пробелом"}, "WEBHOOK_SECRET"),
        ({"webhook_secret": "a/b"}, "WEBHOOK_SECRET"),
        ({"port": 0}, "PORT"),
    ],
)
async def test_webhook_mode_fails_fast_with_clear_error(overrides: dict[str, Any], expected: str) -> None:
    settings = make_settings(**overrides)
    with pytest.raises(bot_main.ConfigError) as error:
        bot_main.check_run_mode(settings)
    assert expected in str(error.value)
    with pytest.raises(bot_main.ConfigError):
        await bot_main.main(settings)  # падает до подключения к БД и Telegram


def test_run_mode_settings_accepted() -> None:
    bot_main.check_run_mode(make_settings())
    bot_main.check_run_mode(make_settings(public_url="", render_external_url="https://kpi.onrender.com"))
    bot_main.check_run_mode(make_settings(webhook_secret=""))
    # polling адрес не нужен.
    bot_main.check_run_mode(make_settings(run_mode="polling", public_url="", render_external_url=""))


async def test_polling_refuses_to_take_webhook_from_cloud() -> None:
    """Webhook включён (бот работает на Render), а на компьютере запустили run.bat (polling): запуск
    останавливается понятной ошибкой, webhook НЕ снимается — облачный бот продолжает получать сообщения.
    В тексте — только имя сервера, без секретного пути."""
    api = WebhookApi(url=f"https://kpi-bot.onrender.com/tg/{HOOK_SECRET}")
    bot = make_bot(api)
    with pytest.raises(bot_main.ConfigError) as error:
        await bot_main._drop_webhook(bot)
    assert "kpi-bot.onrender.com" in str(error.value) and "TAKEOVER_WEBHOOK=1" in str(error.value)
    assert HOOK_SECRET not in str(error.value)
    assert not api.delete_calls and api.webhook_url.endswith(HOOK_SECRET)
    await bot.session.close()


async def test_polling_takeover_drops_leftover_webhook() -> None:
    """TAKEOVER_WEBHOOK=1 — webhook снимается явно (накопившиеся сообщения не сбрасываются)."""
    api = WebhookApi(url=f"{BASE}/tg/{HOOK_SECRET}")
    bot = make_bot(api)
    await bot_main._drop_webhook(bot, takeover=True)
    [call] = api.delete_calls
    assert call.drop_pending_updates is False
    await bot_main._drop_webhook(bot)  # webhook уже нет — deleteWebhook не нужен, ошибки нет
    assert len(api.delete_calls) == 1
    await bot.session.close()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_until(check: Callable[[], Any], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if await check():
                return
        except aiohttp.ClientError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError("не дождались")
        await asyncio.sleep(0.05)


async def test_run_webhook_serves_sets_webhook_and_keeps_it_on_stop(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    slow_jobs: SlowJobs,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Запуск в режиме webhook: сервер на 0.0.0.0:PORT, setWebhook, /health и /tick работают,
    фоновый цикл (самопробуждение и задания) запущен; по сигналу остановки цикл и сервер
    останавливаются, а webhook у Telegram остаётся (его использует новый экземпляр бота
    при обновлении на хостинге)."""
    # Первые срабатывания фонового цикла — не раньше чем через час: здесь нет запросов в интернет
    # (публичный адрес — kpi-bot.example.com); сам цикл проверяется в tests/test_keepalive.py.
    monkeypatch.setattr(bot_web, "JOB_FIRST_DELAY_SEC", 3600)
    monkeypatch.setattr(bot_web, "KEEPALIVE_FIRST_DELAY_SEC", 3600)
    built: list[web.Application] = []
    real_build = bot_main.build_web_app

    def build(*args: Any, **kwargs: Any) -> web.Application:
        built.append(real_build(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr(bot_main, "build_web_app", build)
    port = free_port()
    hosts: list[str] = []
    real_site = web.TCPSite

    def local_site(runner: web.BaseRunner, host: str, port: int, **kwargs: Any) -> web.TCPSite:
        hosts.append(host)
        return real_site(runner, "127.0.0.1", port, **kwargs)  # без запроса брандмауэра Windows

    stop = asyncio.Event()

    async def wait_for_stop() -> None:
        await stop.wait()

    monkeypatch.setattr(bot_main.web, "TCPSite", local_site)
    monkeypatch.setattr(bot_main, "_wait_for_stop", wait_for_stop)
    slow_jobs.gate.set()
    api = WebhookApi()
    bot = make_bot(api)
    settings = make_settings(port=port)
    _release_bot_routers()
    try:
        dp = bot_main.build_dispatcher(sessionmaker)
        caplog.set_level(logging.INFO)
        task = asyncio.create_task(bot_main._run_webhook(settings, bot, dp, sessionmaker))
        url = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as http:

            async def healthy() -> bool:
                async with http.get(url + "/health") as response:
                    return response.status == 200

            await wait_until(healthy)
            await wait_until(lambda: asyncio.sleep(0, result=bool(api.set_calls)))
            [app] = built
            background = app[bot_web.BACKGROUND]
            await wait_until(lambda: asyncio.sleep(0, result=background.running))
            async with http.get(f"{url}/tick?key={TICK_KEY}") as response:
                assert response.status == 200
            await wait_until(lambda: asyncio.sleep(0, result=bool(slow_jobs.calls)))
            stop.set()
            await asyncio.wait_for(task, timeout=10)
            with pytest.raises(aiohttp.ClientError):
                async with http.get(url + "/health") as response:
                    pass
        assert not background.running and background.task is not None and background.task.cancelled()
        assert background.keepalive_url == f"{BASE}/health"
        assert "Самопробуждение: каждые 10 мин; задания по расписанию: каждые 5 мин" in caplog.text
        assert "cron-job.org" not in caplog.text
        assert TICK_KEY not in caplog.text and HOOK_SECRET not in caplog.text
        assert hosts == ["0.0.0.0"]
        assert api.webhook_url == f"{BASE}/tg/{HOOK_SECRET}" and api.webhook_secret == HOOK_SECRET
        assert not api.delete_calls
        names = [type(request).__name__ for request in api.requests]
        assert names[:3] == ["GetMe", "GetWebhookInfo", "SetWebhook"] and "SetMyCommands" in names
    finally:
        _release_bot_routers()
        await bot.session.close()


async def test_run_polling_refuses_while_webhook_is_active(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Без TAKEOVER_WEBHOOK polling при включённом webhook не стартует: ни deleteWebhook, ни getUpdates."""
    api = WebhookApi(url=f"{BASE}/tg/{HOOK_SECRET}")
    bot = make_bot(api)
    _release_bot_routers()
    try:
        dp = bot_main.build_dispatcher(sessionmaker)
        with pytest.raises(bot_main.ConfigError):
            await asyncio.wait_for(bot_main._run_polling(bot, dp, sessionmaker), timeout=10)
        names = [type(request).__name__ for request in api.requests]
        assert names == ["GetMe", "GetWebhookInfo"] and not api.delete_calls
    finally:
        _release_bot_routers()
        await bot.session.close()


async def test_run_polling_drops_webhook_and_polls(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Режим polling работает как раньше: getMe, отключение webhook (если был и TAKEOVER_WEBHOOK=1),
    команды, планировщик, getUpdates; остановка — по stop_polling."""
    api = WebhookApi(url=f"{BASE}/tg/{HOOK_SECRET}")
    bot = make_bot(api)
    _release_bot_routers()
    try:
        dp = bot_main.build_dispatcher(sessionmaker)
        task = asyncio.create_task(bot_main._run_polling(bot, dp, sessionmaker, takeover_webhook=True))
        await wait_until(lambda: asyncio.sleep(0, result=any(isinstance(r, m.GetUpdates) for r in api.requests)))
        await dp.stop_polling()
        await asyncio.wait_for(task, timeout=10)
        names = [type(request).__name__ for request in api.requests]
        assert names[:4] == ["GetMe", "GetWebhookInfo", "DeleteWebhook", "SetMyCommands"]
        assert api.delete_calls[0].drop_pending_updates is False and api.webhook_url == ""
        assert not api.set_calls
    finally:
        _release_bot_routers()
        await bot.session.close()


def test_build_dispatcher_storage(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """По умолчанию FSM — в БД (bot.fsm_storage.DbStorage, если модуль есть); тесты могут передать своё."""
    _release_bot_routers()
    try:
        own = MemoryStorage()
        assert bot_main.build_dispatcher(sessionmaker, storage=own).storage is own
        _release_bot_routers()
        storage = bot_main.build_dispatcher(sessionmaker).storage
        try:
            from bot.fsm_storage import DbStorage
        except ImportError:
            assert isinstance(storage, MemoryStorage)
        else:
            assert isinstance(storage, DbStorage)
    finally:
        _release_bot_routers()


@pytest.mark.parametrize("backend", ["postgresql", "sqlite"])
async def test_main_gives_dialog_storage_own_pool_on_postgres(
    backend: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main(): на PostgreSQL хранилище диалогов работает через свой пул из одного соединения
    (bot.db.base.make_storage_engine) — апдейты, занявшие основной пул, не ждут друг друга при записи
    состояния; на SQLite — тот же движок (запись вдогонку). Без сети: к базе никто не подключается."""
    from bot.fsm_storage import DbStorage

    if backend == "postgresql":
        url = "postgresql://postgres.ref:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    else:
        url = f"sqlite+aiosqlite:///{(tmp_path / 'data' / 'bot.db').as_posix()}"
    captured: dict[str, Any] = {}

    async def no_init(engine: Any) -> None:
        captured["init_engine"] = engine

    async def fake_polling(
        bot: Bot, dp: Dispatcher, sessionmaker: async_sessionmaker[AsyncSession], **kwargs: Any
    ) -> None:
        captured["storage"] = dp.storage
        captured["main_engine"] = sessionmaker.kw["bind"]

    monkeypatch.setattr(bot_main, "init_db", no_init)
    monkeypatch.setattr(bot_main, "_run_polling", fake_polling)
    _release_bot_routers()
    try:
        await bot_main.main(make_settings(run_mode="polling", database_url=url))
    finally:
        _release_bot_routers()
    storage, main_engine = captured["storage"], captured["main_engine"]
    assert isinstance(storage, DbStorage) and captured["init_engine"] is main_engine
    storage_engine = storage.sessionmaker.kw["bind"]
    if backend == "postgresql":
        assert storage_engine is not main_engine and storage.write_behind is False
        assert (storage_engine.pool.size(), storage_engine.pool._max_overflow) == (1, 0)
        assert (main_engine.pool.size(), main_engine.pool._max_overflow) == (3, 1)
        assert storage_engine.url == main_engine.url
    else:
        assert storage_engine is main_engine and storage.write_behind is True


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"database_url": "ВСТАВЬТЕ_СЮДА_СТРОКУ_SESSION_POOLER_ИЗ_SUPABASE"}, "DATABASE_URL"),
        ({"database_url": "postgresql://u:[YOUR-PASSWORD]@h.example.com:5432/db",
          "database_password": "ВСТАВЬТЕ_СЮДА_ПАРОЛЬ_БАЗЫ_SUPABASE"}, "DATABASE_PASSWORD"),
        ({"database_url": "  "}, "DATABASE_URL пуст"),
        ({"database_url": "postgresql://u:SeCrEt-77@h.example.com:notaport/db"}, "DATABASE_URL записан неверно"),
        ({"database_url": "SeCrEt-77"}, "DATABASE_URL записан неверно"),
        ({"database_url": "mysql://u:SeCrEt-77@h.example.com/db"}, "DATABASE_URL записан неверно"),
        ({"database_url": "postgresql://u:[YOUR-PASSWORD]@h.example.com:5432/db"}, "DATABASE_PASSWORD не задан"),
        ({"database_url": "postgresql://u:[YOUR-PASSWORD]@h.example.com:5432/db",
          "database_password": "  "}, "DATABASE_PASSWORD не задан"),
    ],
)
async def test_database_settings_fail_fast_with_clear_error(overrides: dict[str, Any], expected: str) -> None:
    """Заглушки из deploy/make_render_env.py («ВСТАВЬТЕ_СЮДА_…») и неразборчивый DATABASE_URL — понятная
    ошибка по-русски до подключения к базе; значения (пароль) в текст ошибки не попадают."""
    settings = make_settings(run_mode="polling", **overrides)
    with pytest.raises(bot_main.ConfigError) as error:
        bot_main.check_database(settings)
    assert expected in str(error.value) and "SeCrEt" not in str(error.value)
    with pytest.raises(bot_main.ConfigError):
        await bot_main.main(settings)  # падает до подключения к БД и Telegram


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"database_url": "sqlite+aiosqlite:///data/bot.db"},
        {"database_url": "postgresql://postgres.ref:[YOUR-PASSWORD]@aws-1-eu-central-1.pooler.supabase.com:5432/postgres",
         "database_password": "Ваш8Пароль#$"},
        {"database_url": '"postgres://u:p@h.example.com:5432/db?sslmode=require"'},
        # Пароль уже в адресе — DATABASE_PASSWORD не используется, заглушка в нём не мешает
        # (так пишет deploy/make_render_env.py: «строку можно не заполнять»).
        {"database_url": "postgresql://postgres.ref:RealPw123@aws-1-eu-central-1.pooler.supabase.com:5432/postgres",
         "database_password": "ВСТАВЬТЕ_СЮДА_ПАРОЛЬ_БАЗЫ_SUPABASE"},
        {"database_url": "sqlite+aiosqlite:///data/bot.db", "database_password": "ВСТАВЬТЕ_СЮДА_ПАРОЛЬ_БАЗЫ_SUPABASE"},
        # Без пароля и без заглушки (локальный сервер с trust, .pgpass) — как раньше.
        {"database_url": "postgresql://postgres@localhost:5432/kpi"},
        # Direct connection на своём компьютере (polling) — может работать по IPv6, не запрещаем.
        {"database_url": "postgresql://postgres:[YOUR-PASSWORD]@db.abcdefghijklmnop.supabase.co:5432/postgres",
         "database_password": "pw"},
    ],
)
def test_database_settings_accepted(overrides: dict[str, Any]) -> None:
    bot_main.check_database(make_settings(run_mode="polling", **overrides))
    if ".supabase.co" not in overrides.get("database_url", ""):
        bot_main.check_database(make_settings(**overrides))  # и в режиме webhook


async def test_direct_connection_rejected_in_webhook_mode() -> None:
    """Render + строка Supabase «Direct connection» (db.….supabase.co, только IPv6) — понятная ошибка до
    подключения к базе, а не трейсбек сетевой ошибки."""
    settings = make_settings(
        database_url="postgresql://postgres:[YOUR-PASSWORD]@db.abcdefghijklmnop.supabase.co:5432/postgres",
        database_password="SeCrEt-77",
    )
    with pytest.raises(bot_main.ConfigError) as error:
        bot_main.check_database(settings)
    assert "Session pooler" in str(error.value) and "SeCrEt" not in str(error.value)
    with pytest.raises(bot_main.ConfigError):
        await bot_main.main(settings)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (socket.gaierror(11001, "getaddrinfo failed"), "адрес сервера не найден"),
        (TimeoutError(), "не ответил вовремя"),
        (ConnectionRefusedError(10061, "refused"), "отказал в подключении"),
        (OSError(10065, "unreachable"), "сетевая ошибка"),
    ],
)
def test_database_network_errors_explained(exc: BaseException, expected: str) -> None:
    problem = bot_main._db_startup_problem(exc)
    assert problem is not None and expected in problem and "Session pooler" in problem
    assert bot_main._db_startup_problem(RuntimeError("другое")) is None


async def test_main_explains_unreachable_database() -> None:
    """Сервер PostgreSQL из DATABASE_URL не отвечает — main() завершается ConfigError по-русски (run() пишет
    одну строку CRITICAL и выходит с кодом 2) вместо длинного трейсбека; пароля в тексте нет."""
    settings = make_settings(run_mode="polling", database_url="postgresql://u:SeCrEt-77@127.0.0.1:1/db")
    with pytest.raises(bot_main.ConfigError) as error:
        await asyncio.wait_for(bot_main.main(settings), timeout=60)
    assert "Не удалось подключиться к базе данных" in str(error.value) and "SeCrEt" not in str(error.value)


async def test_main_explains_wrong_database_password() -> None:
    """Настоящий PostgreSQL (TEST_DATABASE_URL) и неверный DATABASE_PASSWORD — понятная причина
    («не приняла пароль»), пароль в текст не попадает."""
    import os

    from sqlalchemy.engine import make_url

    raw = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not raw.startswith("postgres"):
        pytest.skip("нужен PostgreSQL: задайте TEST_DATABASE_URL")
    url = make_url(raw).set(password="[YOUR-PASSWORD]").render_as_string(hide_password=False)
    settings = make_settings(run_mode="polling", database_url=url, database_password="WrOnG-pAsS-123")
    with pytest.raises(bot_main.ConfigError) as error:
        await asyncio.wait_for(bot_main.main(settings), timeout=60)
    text = str(error.value)
    assert "не приняла пароль" in text and "WrOnG" not in text
