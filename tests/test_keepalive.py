"""Фоновый цикл режима webhook (bot.web.BackgroundLoop): бот сам не засыпает и сам запускает задания.

* каждые 5 мин — run_due_jobs через тот же TickRunner, что и /tick (в процессе запуски не
  накладываются: пока идёт один, другой пропускается);
* каждые 10 мин (первый раз — через минуту) — GET <публичный адрес>/health: для Render это входящий
  запрос, и бесплатный сервис не засыпает; нет публичного адреса — самопробуждения нет;
* вместе с заданиями — проверка webhook (reclaim_webhook): сняла копия бота в режиме polling — вернуть;
* остановка цикла — чистая (и явная, и при остановке веб-приложения); режим polling цикл не трогает.

Расписание проверяется на «ненастоящих» часах (FakeClock: sleep мгновенно сдвигает время), запросы
самопробуждения — настоящие, к серверу aiohttp на 127.0.0.1. Telegram — фейковый, сети наружу нет.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_web import (
    BASE,
    HOOK_SECRET,
    SlowJobs,
    WebhookApi,
    _release_bot_routers,
    free_port,
    make_bot,
    make_settings,
    no_db,
    ping_dispatcher,
    wait_until,
)

from bot import main as bot_main
from bot import web as bot_web
from bot.scheduler import jobs

TICK_KEY = "tick-key-456"  # как в tests/test_web.py (make_settings)
HOUR = 3600.0


class FakeClock:
    """Часы цикла под контролем теста: sleep(d) мгновенно сдвигает «сейчас» на d.

    Дойдя до limit, sleep «замирает» (до отмены цикла) и выставляет reached — тест проверяет,
    что успело произойти за это время. wakeups — моменты, когда цикл просыпался.
    """

    def __init__(self, limit: float) -> None:
        self.now = 0.0
        self.limit = limit
        self.wakeups: list[float] = []
        self.reached = asyncio.Event()

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        for _ in range(5):
            await asyncio.sleep(0)  # задачи, которые цикл только что запустил, работают в «текущий» момент
        if self.now + delay > self.limit:
            self.reached.set()
            await asyncio.Event().wait()  # дальше limit время не идёт — ждём stop()
        self.now += delay
        self.wakeups.append(self.now)

    async def run(self, loop: bot_web.BackgroundLoop) -> None:
        """Прогнать цикл до limit и остановить его."""
        assert loop.start() is True
        await asyncio.wait_for(self.reached.wait(), timeout=10)
        await loop.stop()


class RecordingJobs:
    """Подмена run_due_jobs: запоминает «время» (FakeClock) каждого запуска; gate — долгие задания."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock
        self.times: list[float] = []
        self.gate = asyncio.Event()
        self.gate.set()

    async def __call__(self, bot: Any, sessionmaker: Any, now: Any = None) -> dict[str, object]:
        self.times.append(self.clock.now if self.clock else time.monotonic())
        await self.gate.wait()
        return {"reminders": 0, "digest": "not_due", "backup": "not_due"}


def fake_clock_loop(app: web.Application, clock: FakeClock) -> bot_web.BackgroundLoop:
    """Заменить цикл приложения таким же, но на часах FakeClock (до старта сервера)."""
    old = app[bot_web.BACKGROUND]
    loop = bot_web.BackgroundLoop(old.ticker, old.keepalive_url, clock=clock.monotonic, sleep=clock.sleep)
    app[bot_web.BACKGROUND] = loop
    return loop


def count_keepalive(app: web.Application, hits: list[float], clock: Callable[[], float]) -> None:
    """Middleware: запомнить «время» каждого запроса самопробуждения (GET /health с его User-Agent)."""

    @web.middleware
    async def middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        if request.path == "/health" and request.headers.get("User-Agent") == bot_web.KEEPALIVE_USER_AGENT:
            assert request.method == "GET"
            hits.append(clock())
        return await handler(request)

    app.middlewares.append(middleware)


async def start_local(app: web.Application, port: int) -> TestClient:
    client = TestClient(TestServer(app, host="127.0.0.1", port=port))
    await client.start_server()
    return client


def local_settings(port: int) -> Any:
    """Настройки, где публичный адрес бота — локальный сервер на port (самопробуждение без интернета)."""
    return make_settings(public_url=f"http://127.0.0.1:{port}", port=port)


# --- Константы и строка в лог -------------------------------------------------------------------------


def test_intervals_fit_render_free() -> None:
    """Render Free засыпает после 15 мин без входящих запросов: будим чаще; задания — каждые 5 мин."""
    assert bot_web.JOB_INTERVAL_SEC == 5 * 60
    assert bot_web.KEEPALIVE_INTERVAL_SEC == 10 * 60
    assert bot_web.KEEPALIVE_INTERVAL_SEC < 15 * 60
    assert bot_web.KEEPALIVE_FIRST_DELAY_SEC == 60
    assert bot_web.KEEPALIVE_RETRY_SEC < bot_web.KEEPALIVE_INTERVAL_SEC
    assert bot_web.KEEPALIVE_TIMEOUT_SEC < 60


def test_describe_and_keepalive_url() -> None:
    settings = make_settings()
    app = bot_web.build_web_app(make_bot(), ping_dispatcher([]), no_db, settings)  # type: ignore[arg-type]
    loop = app[bot_web.BACKGROUND]
    assert loop.ticker is app[bot_web.TICKER]  # тот же single-flight, что и у /tick
    assert loop.keepalive_url == "https://kpi-bot.example.com/health"
    assert loop.describe() == "Самопробуждение: каждые 10 мин; задания по расписанию: каждые 5 мин"
    assert not loop.running  # сам не запускается — его запускает main.py после старта сервера

    render = make_settings(public_url="", render_external_url="https://kpi.onrender.com/")
    assert bot_web.keepalive_url(render) == "https://kpi.onrender.com/health"
    assert bot_web.keepalive_url(make_settings(public_url="", render_external_url="")) is None


# --- Расписание (FakeClock) ---------------------------------------------------------------------------


async def test_loop_runs_jobs_every_5_min_and_wakes_itself_every_10_min(monkeypatch: pytest.MonkeyPatch) -> None:
    """Час работы: задания — на 30-й секунде и дальше каждые 5 мин (12 раз); самопробуждение —
    через минуту и дальше каждые 10 мин (6 раз), настоящий GET /health работающего приложения."""
    clock = FakeClock(limit=HOUR)
    fake_jobs = RecordingJobs(clock)
    monkeypatch.setattr(jobs, "run_due_jobs", fake_jobs)
    port = free_port()
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, local_settings(port))  # type: ignore[arg-type]
    loop = fake_clock_loop(app, clock)
    hits: list[float] = []
    count_keepalive(app, hits, clock.monotonic)
    client = await start_local(app, port)
    try:
        await clock.run(loop)
    finally:
        await client.close()
        await bot.session.close()

    assert fake_jobs.times == [30 + 300 * n for n in range(12)]
    assert loop.job_starts == 12 and loop.job_skips == 0
    assert hits == [60 + 600 * n for n in range(6)]
    assert loop.pings_ok == 6 and loop.pings_failed == 0
    assert not loop.running


async def test_no_keepalive_without_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет публичного адреса (PUBLIC_URL и RENDER_EXTERNAL_URL пусты) — самопробуждения нет,
    задания по расписанию идут как обычно."""
    clock = FakeClock(limit=HOUR)
    fake_jobs = RecordingJobs(clock)
    monkeypatch.setattr(jobs, "run_due_jobs", fake_jobs)
    created: list[Any] = []
    real_session = aiohttp.ClientSession

    def session_spy(*args: Any, **kwargs: Any) -> aiohttp.ClientSession:
        created.append(kwargs)
        return real_session(*args, **kwargs)

    monkeypatch.setattr(bot_web.aiohttp, "ClientSession", session_spy)
    bot = make_bot()
    settings = make_settings(public_url="", render_external_url="")
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, settings)  # type: ignore[arg-type]
    loop = fake_clock_loop(app, clock)
    assert loop.keepalive_url is None
    assert loop.describe().startswith("Самопробуждение: выключено")
    await clock.run(loop)
    await bot.session.close()

    assert fake_jobs.times == [30 + 300 * n for n in range(12)]
    assert clock.wakeups == fake_jobs.times  # цикл просыпался только ради заданий
    assert created == [] and loop.pings_ok == loop.pings_failed == 0


async def test_keepalive_failures_are_retried_sooner_and_logged_without_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """/health ответил 503 — следующая попытка через 2 мин (а не через 10: иначе Render успеет усыпить);
    в лог — одно предупреждение без адреса бота, после восстановления — «снова отвечает»."""
    statuses = [503, 503, 200, 200]
    clock = FakeClock(limit=1000)
    hits: list[float] = []

    async def health(request: web.Request) -> web.Response:
        hits.append(clock.now)
        return web.Response(status=statuses.pop(0), text="ok")

    port = free_port()
    target = web.Application()
    target.router.add_get("/health", health)
    client = await start_local(target, port)
    ticker = bot_web.TickRunner(make_bot(), no_db)  # type: ignore[arg-type]
    loop = bot_web.BackgroundLoop(ticker, f"http://127.0.0.1:{port}/health", clock=clock.monotonic, sleep=clock.sleep)
    loop.job_first_delay = loop.job_interval = 10 * HOUR  # здесь — только самопробуждение
    try:
        with caplog.at_level(logging.DEBUG, logger=bot_web.__name__):
            await clock.run(loop)
    finally:
        await client.close()
        await ticker.bot.session.close()

    assert hits == [60, 180, 300, 900]
    assert loop.pings_failed == 2 and loop.pings_ok == 2
    records = [record for record in caplog.records if "Самопробуждение" in record.getMessage()]
    warnings = [record for record in records if record.levelno == logging.WARNING]
    assert len(warnings) == 1 and "HTTP 503" in warnings[0].getMessage()
    assert any(record.levelno == logging.INFO and "снова отвечает" in record.getMessage() for record in records)
    assert all(str(port) not in record.getMessage() and "127.0.0.1" not in record.getMessage() for record in records)


async def test_keepalive_network_errors_and_timeouts_do_not_stop_the_loop() -> None:
    """Неверный адрес и зависший /health (ответа нет дольше таймаута) — неудача, цикл живёт дальше
    и повторяет попытки (60, 180, 300 с)."""
    gate = asyncio.Event()

    async def hanging(request: web.Request) -> web.Response:
        await gate.wait()
        return web.Response(text="ok")

    port = free_port()
    target = web.Application()
    target.router.add_get("/health", hanging)
    client = await start_local(target, port)
    ticker = bot_web.TickRunner(make_bot(), no_db)  # type: ignore[arg-type]
    try:
        # ftp:// — ошибка сразу, без обращения к сети (как неверно заданный адрес).
        for url in ("ftp://127.0.0.1/health", f"http://127.0.0.1:{port}/health"):
            clock = FakeClock(limit=400)
            loop = bot_web.BackgroundLoop(ticker, url, clock=clock.monotonic, sleep=clock.sleep)
            loop.job_first_delay = loop.job_interval = 10 * HOUR
            loop.keepalive_timeout = 0.2
            started = time.monotonic()
            await clock.run(loop)
            assert time.monotonic() - started < 5
            assert loop.pings_failed == 3 and loop.pings_ok == 0  # 60, 180, 300 с
    finally:
        gate.set()
        await client.close()
        await ticker.bot.session.close()


# --- Один запуск заданий на процесс: общий с /tick -----------------------------------------------------


async def test_loop_skips_jobs_while_tick_run_is_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """/tick запустил задания, они идут долго — цикл в свой срок новых не начинает (пропуск),
    а /tick, пока идут задания цикла, отвечает «busy»."""
    slow = SlowJobs()
    monkeypatch.setattr(jobs, "run_due_jobs", slow)
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings(public_url="", render_external_url=""))  # type: ignore[arg-type]
    clock = FakeClock(limit=100)  # цикл успевает одну попытку — на 30-й секунде
    loop = fake_clock_loop(app, clock)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await (await client.get(f"/tick?key={TICK_KEY}")).json())["status"] == "started"
        await clock.run(loop)
        assert loop.job_skips == 1 and loop.job_starts == 0 and len(slow.calls) == 1
        slow.gate.set()
        await asyncio.wait_for(app[bot_web.TICKER].task, timeout=5)  # type: ignore[arg-type]

        # Наоборот: задания запустил цикл — /tick их не дублирует.
        slow.gate.clear()
        clock2 = FakeClock(limit=100)
        loop2 = bot_web.BackgroundLoop(app[bot_web.TICKER], None, clock=clock2.monotonic, sleep=clock2.sleep)
        await clock2.run(loop2)
        assert loop2.job_starts == 1 and len(slow.calls) == 2
        body = await (await client.get(f"/tick?key={TICK_KEY}")).json()
        assert body["status"] == "busy" and len(slow.calls) == 2
        slow.gate.set()
        await asyncio.wait_for(app[bot_web.TICKER].task, timeout=5)  # type: ignore[arg-type]
    finally:
        slow.gate.set()
        await client.close()
        await bot.session.close()


# --- Остановка ------------------------------------------------------------------------------------------


async def test_stop_cancels_loop_cleanly_and_is_idempotent() -> None:
    ticker = bot_web.TickRunner(make_bot(), no_db)  # type: ignore[arg-type]
    loop = bot_web.BackgroundLoop(ticker, "http://127.0.0.1:9/health")
    await loop.stop()  # не запущен — ничего не делает
    assert loop.start() is True and loop.start() is False
    await asyncio.sleep(0.01)
    started = time.monotonic()
    await loop.stop()
    await loop.stop()
    assert time.monotonic() - started < 1
    assert loop.task is not None and loop.task.cancelled() and not loop.running
    assert loop.job_starts == 0 and loop.pings_ok == loop.pings_failed == 0
    await ticker.bot.session.close()


async def test_stop_interrupts_keepalive_request_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Остановка во время запроса /health (хостинг не отвечает): запрос прерывается сразу, это не
    «неудача», HTTP-сессия цикла закрыта."""
    monkeypatch.setattr(bot_web, "KEEPALIVE_FIRST_DELAY_SEC", 0)
    sessions: list[aiohttp.ClientSession] = []
    real_session = aiohttp.ClientSession

    def session_spy(*args: Any, **kwargs: Any) -> aiohttp.ClientSession:
        session = real_session(*args, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(bot_web.aiohttp, "ClientSession", session_spy)
    arrived = asyncio.Event()

    async def hanging(request: web.Request) -> web.Response:
        arrived.set()
        await asyncio.Event().wait()
        return web.Response(text="ok")

    port = free_port()
    target = web.Application()
    target.router.add_get("/health", hanging)
    client = await start_local(target, port)
    ticker = bot_web.TickRunner(make_bot(), no_db)  # type: ignore[arg-type]
    loop = bot_web.BackgroundLoop(ticker, f"http://127.0.0.1:{port}/health")
    try:
        loop.start()
        await asyncio.wait_for(arrived.wait(), timeout=5)
        started = time.monotonic()
        await loop.stop()
        assert time.monotonic() - started < 1
    finally:
        await client.close()
        await ticker.bot.session.close()
    assert loop.task is not None and loop.task.cancelled()
    assert loop.pings_failed == 0 and loop.pings_ok == 0
    assert len(sessions) == 1 and sessions[0].closed


async def test_app_shutdown_stops_loop_and_waits_for_jobs_it_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """Остановка веб-приложения (SIGTERM) останавливает цикл сама; задания, которые цикл уже начал,
    не обрываются — остановка их дожидается (как и запущенные по /tick)."""
    monkeypatch.setattr(bot_web, "JOB_FIRST_DELAY_SEC", 0)
    slow = SlowJobs()
    monkeypatch.setattr(jobs, "run_due_jobs", slow)
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings(public_url="", render_external_url=""))  # type: ignore[arg-type]
    loop = app[bot_web.BACKGROUND]
    client = TestClient(TestServer(app))
    await client.start_server()
    loop.start()
    await wait_until(lambda: asyncio.sleep(0, result=bool(slow.calls)))
    asyncio.get_running_loop().call_later(0.2, slow.gate.set)
    await client.close()
    assert not loop.running and loop.task is not None and loop.task.cancelled()
    assert app[bot_web.TICKER].last_result == {"reminders": 1, "digest": "not_due", "backup": "done"}
    assert len(slow.calls) == 1
    await bot.session.close()


# --- main.py: режим webhook запускает цикл, polling — нет -------------------------------------------------


async def test_run_webhook_wakes_itself_and_runs_jobs_without_external_pinger(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Бот в режиме webhook без внешнего будильника: сам запускает задания и сам открывает свой
    публичный адрес /health (здесь публичный адрес — этот же сервер на 127.0.0.1); по сигналу
    остановки цикл останавливается вместе с сервером."""
    port = free_port()
    real_site = web.TCPSite

    def local_site(runner: web.BaseRunner, host: str, port: int, **kwargs: Any) -> web.TCPSite:
        return real_site(runner, "127.0.0.1", port, **kwargs)  # без запроса брандмауэра Windows

    stop = asyncio.Event()

    async def wait_for_stop() -> None:
        await stop.wait()

    keepalive_hits: list[str] = []
    real_health = bot_web._health

    async def health(request: web.Request) -> web.Response:
        if request.headers.get("User-Agent") == bot_web.KEEPALIVE_USER_AGENT:
            keepalive_hits.append(request.method)
        return await real_health(request)

    built: list[web.Application] = []
    real_build = bot_main.build_web_app

    def build(*args: Any, **kwargs: Any) -> web.Application:
        built.append(real_build(*args, **kwargs))
        return built[-1]

    fake_jobs = RecordingJobs()
    monkeypatch.setattr(jobs, "run_due_jobs", fake_jobs)
    monkeypatch.setattr(bot_web, "_health", health)
    monkeypatch.setattr(bot_web, "JOB_FIRST_DELAY_SEC", 0.05)
    monkeypatch.setattr(bot_web, "JOB_INTERVAL_SEC", 0.1)
    monkeypatch.setattr(bot_web, "KEEPALIVE_FIRST_DELAY_SEC", 0.05)
    monkeypatch.setattr(bot_web, "KEEPALIVE_INTERVAL_SEC", 0.1)
    monkeypatch.setattr(bot_main.web, "TCPSite", local_site)
    monkeypatch.setattr(bot_main, "_wait_for_stop", wait_for_stop)
    monkeypatch.setattr(bot_main, "build_web_app", build)
    api = WebhookApi()
    bot = make_bot(api)
    settings = local_settings(port)
    _release_bot_routers()
    try:
        dp = bot_main.build_dispatcher(sessionmaker)
        with caplog.at_level(logging.INFO):
            task = asyncio.create_task(bot_main._run_webhook(settings, bot, dp, sessionmaker))
            await wait_until(lambda: asyncio.sleep(0, result=len(keepalive_hits) >= 2 and len(fake_jobs.times) >= 2))
            stop.set()
            await asyncio.wait_for(task, timeout=10)
        [app] = built
        loop = app[bot_web.BACKGROUND]
        assert not loop.running and loop.task is not None and loop.task.cancelled()
        assert loop.pings_ok >= 2 and loop.pings_failed == 0
        assert set(keepalive_hits) == {"GET"}
        assert loop.job_starts >= 2
        # Проверка webhook шла вместе с заданиями: webhook на месте — setWebhook только при запуске.
        assert loop.webhook_checks >= 1 and len(api.set_calls) == 1
        assert "Самопробуждение: каждые 0.1 с; задания по расписанию: каждые 0.1 с" in caplog.text
        assert TICK_KEY not in caplog.text
        async with aiohttp.ClientSession() as http:
            with pytest.raises(aiohttp.ClientError):
                async with http.get(f"http://127.0.0.1:{port}/health"):
                    pass
    finally:
        _release_bot_routers()
        await bot.session.close()


async def test_polling_mode_does_not_start_background_loop(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Режим polling (свой ПК, VPS): ни фонового цикла, ни запросов к себе — задания у APScheduler."""

    def forbidden(self: bot_web.BackgroundLoop) -> bool:
        raise AssertionError("в режиме polling фоновый цикл webhook не запускается")

    monkeypatch.setattr(bot_web.BackgroundLoop, "start", forbidden)
    api = WebhookApi()
    bot = make_bot(api)
    _release_bot_routers()
    try:
        dp = bot_main.build_dispatcher(sessionmaker)
        task = asyncio.create_task(bot_main._run_polling(bot, dp, sessionmaker))
        await wait_until(lambda: asyncio.sleep(0, result=any(type(r).__name__ == "GetUpdates" for r in api.requests)))
        await dp.stop_polling()
        await asyncio.wait_for(task, timeout=10)
        assert not api.set_calls
    finally:
        _release_bot_routers()
        await bot.session.close()


# --- Webhook: вернуть, если его сняла другая копия бота -------------------------------------------------


async def test_reclaim_webhook_returns_webhook_taken_by_another_copy(caplog: pytest.LogCaptureFixture) -> None:
    """Копия бота в режиме polling (run.bat на компьютере, старая версия) сняла webhook или другой хостинг
    поставил свой адрес — reclaim_webhook возвращает его сюда (тот же адрес и секрет, накопившиеся апдейты не
    сбрасываются). Webhook на месте — ничего не меняется, даже если отличается только список типов апдейтов
    (при обновлении на хостинге старый и новый экземпляры не перетягивают его друг у друга)."""
    settings = make_settings()
    dp = ping_dispatcher([])
    url = f"{BASE}/tg/{HOOK_SECRET}"
    api = WebhookApi(url="")
    bot = make_bot(api)
    caplog.set_level(logging.INFO)
    try:
        assert await bot_web.reclaim_webhook(bot, dp, settings) is True
        [call] = api.set_calls
        assert call.url == url and call.secret_token == HOOK_SECRET and call.drop_pending_updates is False
        assert "Webhook у Telegram снят" in caplog.text and HOOK_SECRET not in caplog.text

        assert await bot_web.reclaim_webhook(bot, dp, settings) is False
        api.webhook_allowed = ["message", "callback_query", "my_chat_member"]
        assert await bot_web.reclaim_webhook(bot, dp, settings) is False
        assert len(api.set_calls) == 1

        api.webhook_url = "https://other-host.example.com/tg/xyz"
        assert await bot_web.reclaim_webhook(bot, dp, settings) is True
        assert api.webhook_url == url and len(api.set_calls) == 2
        assert "указывает на другой адрес" in caplog.text
    finally:
        await bot.session.close()


async def test_loop_checks_webhook_with_jobs_and_survives_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Проверка webhook — при каждом запуске заданий (30-я секунда, дальше каждые 5 мин); ошибка
    проверки (Telegram недоступен) — только предупреждение в лог, цикл работает дальше."""
    clock = FakeClock(limit=HOUR)
    fake_jobs = RecordingJobs(clock)
    monkeypatch.setattr(jobs, "run_due_jobs", fake_jobs)
    calls: list[float] = []

    async def check() -> None:
        calls.append(clock.now)
        if len(calls) == 2:
            raise RuntimeError("Telegram недоступен")

    ticker = bot_web.TickRunner(make_bot(), no_db)  # type: ignore[arg-type]
    loop = bot_web.BackgroundLoop(ticker, None, webhook_check=check, clock=clock.monotonic, sleep=clock.sleep)
    caplog.set_level(logging.WARNING)
    try:
        await clock.run(loop)
    finally:
        await ticker.bot.session.close()
    assert calls == [30 + 300 * n for n in range(12)] == fake_jobs.times
    assert loop.webhook_checks == 11 and loop.job_starts == 12
    assert "Проверка webhook не удалась (RuntimeError)" in caplog.text
    assert not loop.running and loop.webhook_task is not None and loop.webhook_task.done()


def test_build_web_app_wires_webhook_reclaim() -> None:
    settings = make_settings()
    bot, dp = make_bot(), ping_dispatcher([])
    app = bot_web.build_web_app(bot, dp, no_db, settings)  # type: ignore[arg-type]
    check = app[bot_web.BACKGROUND].webhook_check
    assert isinstance(check, functools.partial) and check.func is bot_web.reclaim_webhook
    assert check.args == (bot, dp, settings)
