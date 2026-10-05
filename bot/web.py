"""Веб-сервер бота в режиме webhook (RUN_MODE=webhook): Render и другие бесплатные веб-хостинги.

Telegram сам присылает сообщения на адрес бота. Внешние сервисы («будильники») не нужны: бот сам
(BackgroundLoop, запускается из main.py после старта веб-сервера)

* каждые JOB_INTERVAL_SEC (5 мин) запускает задания по времени — напоминания, еженедельную сводку,
  резервную копию (bot.scheduler.jobs.run_due_jobs) — через тот же TickRunner, что и /tick;
* каждые KEEPALIVE_INTERVAL_SEC (10 мин) открывает свой ПУБЛИЧНЫЙ адрес <base_url>/health: запрос
  приходит к хостингу снаружи, и бесплатный Render не «усыпляет» бота (он усыпляет сервис после
  15 мин без входящих запросов). Нет публичного адреса — самопробуждения нет;
* вместе с заданиями проверяет, что webhook у Telegram указывает на этот сервер (reclaim_webhook):
  если его сняла копия бота, запущенная где-то ещё (run.bat на компьютере, старая версия), — возвращает.

Маршруты:

* ``POST /tg/<секрет>`` — апдейты Telegram. Заголовок ``X-Telegram-Bot-Api-Secret-Token`` сверяется
  с секретом (иначе 403). Ответ — сразу 200, апдейт обрабатывается в фоне: долгая AI-оценка не держит
  Telegram (не дождавшись ответа, он считает доставку неудачной и присылает апдейт снова). Апдейт
  с уже полученным update_id (повтор от Telegram) второй раз не обрабатывается.
* ``GET /tick?key=<ключ>`` — необязательный внешний «будильник» (резерв): запустить run_due_jobs
  в фоне и сразу ответить. Пока прошлый запуск (по /tick или по расписанию бота) не закончился,
  новый не начинается. Неверный ключ — 403.
* ``GET|HEAD /health`` — «ok» без обращения к БД (проверка хостинга).
* ``GET|HEAD /`` — «KPI bot is running».

Секреты (путь webhook, ключ /tick) в лог не пишутся; журнал HTTP-запросов aiohttp выключен (main.py).
Остановка (SIGTERM): фоновый цикл останавливается (новые задания не начинаются), сервер перестаёт
принимать запросы, апдейты и задания в работе получают до SHUTDOWN_GRACE_SEC секунд, затем
закрывается FSM-хранилище (dp.emit_shutdown). Webhook у Telegram при остановке НЕ удаляется:
при обновлении на хостинге новый экземпляр уже принимает апдейты.
"""

from __future__ import annotations

import asyncio
import functools
import hmac
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.methods import TelegramMethod
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.scheduler import jobs
from bot.utils.dates import utcnow

__all__ = [
    "BACKGROUND",
    "RECEIVER",
    "SECRET_HEADER",
    "TICKER",
    "BackgroundLoop",
    "TickRunner",
    "UpdateReceiver",
    "build_web_app",
    "ensure_webhook",
    "keepalive_url",
    "reclaim_webhook",
    "secrets_equal",
    "webhook_path",
    "webhook_url",
]

log = logging.getLogger(__name__)

Sessionmaker = async_sessionmaker[AsyncSession]

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
SHUTDOWN_GRACE_SEC = 20.0   # Render ждёт после SIGTERM 30 с — успеть закончить апдейты и задания
TICK_TIMEOUT_SEC = 600.0    # зависший запуск заданий не должен навсегда блокировать следующие «тики»
_RECENT_UPDATES = 1000      # сколько последних update_id помнить, чтобы не обработать повтор дважды

# Фоновый цикл бота (BackgroundLoop). Значения читаются при создании цикла — тесты их подменяют.
JOB_INTERVAL_SEC = 5 * 60           # задания по времени (run_due_jobs) — каждые 5 минут
JOB_FIRST_DELAY_SEC = 30.0          # первый запуск — вскоре после старта (как в режиме polling)
KEEPALIVE_INTERVAL_SEC = 10 * 60    # самопробуждение; должно быть меньше 15 мин (после них Render усыпляет)
KEEPALIVE_FIRST_DELAY_SEC = 60.0    # первый запрос к себе — через минуту после старта
KEEPALIVE_RETRY_SEC = 2 * 60        # не ответил — следующая попытка раньше, чтобы не пропустить 15 мин
KEEPALIVE_TIMEOUT_SEC = 20.0        # ждать ответа /health не дольше: зависший запрос не держит цикл
KEEPALIVE_USER_AGENT = "kpi-bot-keepalive"
WEBHOOK_CHECK_TIMEOUT_SEC = 60.0    # проверка webhook (getWebhookInfo / setWebhook) не дольше


def secrets_equal(given: str, expected: str) -> bool:
    """Сравнение секретов за постоянное время (не выдаёт по времени ответа, сколько символов совпало).

    Никогда не бросает: aiohttp декодирует байты заголовка как utf-8 с «surrogateescape», и заголовок
    с недопустимыми байтами (``\\xff``) даёт строку с суррогатами — такую строку нельзя закодировать
    обратно, а секретом она быть не может (иначе вместо ответа 403 — ошибка 500 и трейсбек в логе).
    """
    if not expected:
        return False
    try:
        given_bytes = given.encode("utf-8")
    except UnicodeError:
        return False
    return hmac.compare_digest(given_bytes, expected.encode("utf-8"))


def webhook_path(settings: Settings) -> str:
    """Путь webhook: /tg/<секрет> — случайный адрес, который знают только Telegram и бот."""
    return f"/tg/{settings.webhook_secret_value}"


def webhook_url(settings: Settings) -> str:
    return settings.base_url + webhook_path(settings)


def keepalive_url(settings: Settings) -> str | None:
    """Адрес самопробуждения — /health через ПУБЛИЧНЫЙ адрес бота; None — адреса нет (не будить)."""
    return f"{settings.base_url}/health" if settings.base_url else None


# --- Апдейты Telegram ----------------------------------------------------------------------------


class UpdateReceiver:
    """Приём апдейтов: проверка секрета, ответ Telegram сразу, обработка Dispatcher'ом в фоне.

    Порядок событий одного пользователя сохраняет SimpleEventIsolation Dispatcher'а
    (как в polling с обработкой апдейтов задачами).
    """

    def __init__(self, dp: Dispatcher, bot: Bot, secret: str) -> None:
        self.dp = dp
        self.bot = bot
        self.secret = secret
        self.tasks: set[asyncio.Task[None]] = set()
        self._recent: OrderedDict[int, None] = OrderedDict()

    async def handle(self, request: web.Request) -> web.Response:
        if not secrets_equal(request.headers.get(SECRET_HEADER, ""), self.secret):
            log.warning("Webhook: запрос без верного секретного заголовка отклонён (403)")
            return web.Response(status=403, text="forbidden")
        try:
            payload = await request.json(loads=self.bot.session.json_loads)
        except ValueError:
            return web.Response(status=400, text="bad request")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="bad request")
        update_id = payload.get("update_id")
        if isinstance(update_id, int):
            if update_id in self._recent:
                log.info("Webhook: апдейт %s пришёл повторно — уже принят, пропускаю", update_id)
                return web.json_response({})
            self._remember(update_id)
        task = asyncio.create_task(self._process(payload, update_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return web.json_response({})

    def _remember(self, update_id: int) -> None:
        self._recent[update_id] = None
        while len(self._recent) > _RECENT_UPDATES:
            self._recent.popitem(last=False)

    async def _process(self, payload: dict[str, Any], update_id: object) -> None:
        try:
            result = await self.dp.feed_raw_update(self.bot, payload)
            if isinstance(result, TelegramMethod):
                await self.dp.silent_call_request(self.bot, result)
        except Exception:
            log.exception("Webhook: не удалось обработать апдейт %s", update_id)

    def pending(self) -> set[asyncio.Task[None]]:
        return {task for task in self.tasks if not task.done()}


# --- Задания по времени: /tick и фоновый цикл --------------------------------------------------------


class TickRunner:
    """Запуск run_due_jobs в фоне: в этом процессе — не больше одного запуска одновременно.

    Им пользуются и /tick, и фоновый цикл бота (BackgroundLoop), поэтому их запуски не накладываются.
    Между экземплярами бота (перезапуск на хостинге) повторы отсекает сама run_due_jobs записями в БД.
    """

    def __init__(self, bot: Bot, sessionmaker: Sessionmaker, *, timeout: float = TICK_TIMEOUT_SEC) -> None:
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.timeout = timeout
        self.task: asyncio.Task[None] | None = None
        self.runs = 0
        self.last_result: dict[str, object] | None = None
        self.last_finished: str | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> bool:
        """Запустить задания в фоне. False — прошлый запуск ещё идёт (новый не начинается)."""
        if self.running:
            return False
        self.task = asyncio.create_task(self._run())
        return True

    async def _run(self) -> None:
        try:
            self.last_result = await asyncio.wait_for(
                jobs.run_due_jobs(self.bot, self.sessionmaker), timeout=self.timeout
            )
        except TimeoutError:
            log.error("Задания по времени не уложились в %s с и прерваны", int(self.timeout))
            self.last_result = {"error": "timeout"}
        except Exception:
            log.exception("Задания по времени завершились ошибкой")
            self.last_result = {"error": "exception"}
        finally:
            self.runs += 1
            self.last_finished = f"{utcnow():%Y-%m-%dT%H:%M:%S}Z"

    def pending(self) -> set[asyncio.Task[None]]:
        return {self.task} if self.running and self.task is not None else set()


def _span(seconds: float) -> str:
    """«10 мин» / «0.5 с» — для лога."""
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)} мин"
    return f"{seconds:g} с"


def _next_time(previous: float, interval: float, now: float) -> float:
    """Следующий срок по сетке previous + interval; если отстали (долгий запрос) — от «сейчас»."""
    due = previous + interval
    return due if due > now else now + interval


class BackgroundLoop:
    """Фоновый цикл режима webhook: бот сам не засыпает и сам выполняет задания по времени.

    * каждые JOB_INTERVAL_SEC (первый раз — через JOB_FIRST_DELAY_SEC) — run_due_jobs через TickRunner
      (общий с /tick): в процессе задания никогда не идут дважды одновременно; прошлый запуск ещё
      идёт — этот пропускается. Между экземплярами бота повторы отсекает сама run_due_jobs (БД);
    * каждые KEEPALIVE_INTERVAL_SEC (первый раз — через KEEPALIVE_FIRST_DELAY_SEC) — GET keepalive_url
      (``<публичный адрес>/health``). Запрос уходит в интернет и приходит к хостингу снаружи — для
      Render это входящий трафик, и бесплатный сервис не засыпает. Не ответил — повтор через
      KEEPALIVE_RETRY_SEC, в лог — тип ошибки (без адреса и секретов). keepalive_url=None — не будить;
    * вместе с заданиями — webhook_check() (в build_web_app — reclaim_webhook), отдельной задачей,
      чтобы медленный Telegram не задерживал цикл; прошлая проверка ещё идёт — новая не начинается.

    start() — после старта веб-сервера, stop() — при остановке (идемпотентна; задания, уже начатые
    TickRunner'ом, не прерываются — их дожидается остановка сервера). clock/sleep — для тестов.
    """

    def __init__(
        self,
        ticker: TickRunner,
        keepalive_url: str | None,
        *,
        webhook_check: Callable[[], Awaitable[object]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self.ticker = ticker
        self.keepalive_url = keepalive_url or None
        self.webhook_check = webhook_check
        self.webhook_task: asyncio.Task[None] | None = None
        self.webhook_checks = 0     # сколько проверок webhook завершилось без ошибки
        self.job_interval = float(JOB_INTERVAL_SEC)
        self.job_first_delay = float(JOB_FIRST_DELAY_SEC)
        self.keepalive_interval = float(KEEPALIVE_INTERVAL_SEC)
        self.keepalive_first_delay = float(KEEPALIVE_FIRST_DELAY_SEC)
        self.keepalive_retry = float(KEEPALIVE_RETRY_SEC)
        self.keepalive_timeout = float(KEEPALIVE_TIMEOUT_SEC)
        self._clock = clock
        self._sleep = sleep
        self.task: asyncio.Task[None] | None = None
        self.job_starts = 0     # сколько раз цикл запустил задания
        self.job_skips = 0      # сколько раз пропустил: прошлый запуск (например, по /tick) ещё шёл
        self.pings_ok = 0
        self.pings_failed = 0
        self._failures_in_row = 0

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def describe(self) -> str:
        """Строка для лога при запуске: «Самопробуждение: каждые 10 мин; задания по расписанию: каждые 5 мин»."""
        wake = "выключено (нет публичного адреса)"
        if self.keepalive_url:
            wake = f"каждые {_span(self.keepalive_interval)}"
        return f"Самопробуждение: {wake}; задания по расписанию: каждые {_span(self.job_interval)}"

    def start(self) -> bool:
        """Запустить цикл. False — уже запущен."""
        if self.running:
            return False
        self.task = asyncio.create_task(self._run(), name="kpi-background-loop")
        return True

    async def stop(self) -> None:
        """Остановить цикл и дождаться его завершения (запрос /health и проверка webhook в пути прерываются)."""
        running = {task for task in (self.task, self.webhook_task) if task is not None and not task.done()}
        if not running:
            return
        for task in running:
            task.cancel()
        # asyncio.wait не пробрасывает CancelledError цикла, но пробросит отмену самого stop().
        await asyncio.wait(running)

    async def _run(self) -> None:
        http: aiohttp.ClientSession | None = None
        try:
            started = self._clock()
            next_job = started + self.job_first_delay
            next_ping = started + self.keepalive_first_delay if self.keepalive_url else math.inf
            while True:
                await self._sleep(max(0.0, min(next_job, next_ping) - self._clock()))
                now = self._clock()
                if now >= next_job:
                    self._start_jobs()
                    self._start_webhook_check()
                    next_job = _next_time(next_job, self.job_interval, now)
                if now >= next_ping:
                    if http is None:
                        http = aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(total=self.keepalive_timeout),
                            headers={"User-Agent": KEEPALIVE_USER_AGENT},
                        )
                    ok = await self._ping(http)
                    after = self._clock()
                    if ok:
                        next_ping = _next_time(next_ping, self.keepalive_interval, after)
                    else:
                        next_ping = after + min(self.keepalive_retry, self.keepalive_interval)
        except Exception:  # ошибка в самом цикле (не в задании и не в сети) — не молчать
            log.exception("Фоновый цикл (самопробуждение и задания по расписанию) остановился из-за ошибки")
        finally:
            if http is not None:
                await http.close()

    def _start_jobs(self) -> None:
        try:
            started = self.ticker.start()
        except Exception:
            log.exception("Не удалось запустить задания по расписанию")
            return
        if started:
            self.job_starts += 1
            log.debug("Задания по расписанию запущены")
        else:
            self.job_skips += 1
            log.info("Задания по расписанию: прошлый запуск ещё идёт — этот пропускаю")

    def _start_webhook_check(self) -> None:
        if self.webhook_check is None or (self.webhook_task is not None and not self.webhook_task.done()):
            return
        self.webhook_task = asyncio.create_task(self._check_webhook(), name="kpi-webhook-check")

    async def _check_webhook(self) -> None:
        """webhook_check() с ограничением по времени; ошибки — только в лог (тип ошибки, без адреса)."""
        assert self.webhook_check is not None
        try:
            await asyncio.wait_for(self.webhook_check(), timeout=WEBHOOK_CHECK_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 — сеть/Telegram не должны останавливать цикл
            log.warning("Проверка webhook не удалась (%s) — повторю при следующем запуске заданий", type(exc).__name__)
        else:
            self.webhook_checks += 1

    async def _ping(self, http: aiohttp.ClientSession) -> bool:
        """GET keepalive_url. True — ответ 200. Ошибки только в лог: тип ошибки, без адреса и секретов."""
        assert self.keepalive_url is not None
        try:
            async with http.get(self.keepalive_url) as response:
                status = response.status
        except Exception as exc:  # noqa: BLE001 — сеть не должна останавливать цикл (отмена — не Exception)
            problem = type(exc).__name__
        else:
            if status == 200:
                self.pings_ok += 1
                if self._failures_in_row:
                    log.info("Самопробуждение: публичный адрес бота снова отвечает")
                else:
                    log.debug("Самопробуждение: публичный адрес бота отвечает")
                self._failures_in_row = 0
                return True
            problem = f"HTTP {status}"
        self.pings_failed += 1
        self._failures_in_row += 1
        # Первая неудача и каждая десятая подряд — предупреждение, остальные — debug (не засорять лог).
        level = logging.WARNING if self._failures_in_row == 1 or self._failures_in_row % 10 == 0 else logging.DEBUG
        log.log(
            level,
            "Самопробуждение: публичный адрес бота не ответил (%s), неудач подряд: %s — повторю через %s. "
            "Если так будет постоянно, хостинг может усыплять бота: проверьте PUBLIC_URL.",
            problem,
            self._failures_in_row,
            _span(min(self.keepalive_retry, self.keepalive_interval)),
        )
        return False


# --- Приложение aiohttp ----------------------------------------------------------------------------

RECEIVER: web.AppKey[UpdateReceiver] = web.AppKey("kpi_update_receiver", UpdateReceiver)
TICKER: web.AppKey[TickRunner] = web.AppKey("kpi_tick_runner", TickRunner)
BACKGROUND: web.AppKey[BackgroundLoop] = web.AppKey("kpi_background_loop", BackgroundLoop)
_TICK_SECRET: web.AppKey[str] = web.AppKey("kpi_tick_secret", str)
_DISPATCHER: web.AppKey[Dispatcher] = web.AppKey("kpi_dispatcher", Dispatcher)
_BOT: web.AppKey[Bot] = web.AppKey("kpi_bot", Bot)


async def _index(request: web.Request) -> web.Response:
    return web.Response(text="KPI bot is running")


async def _health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _tick(request: web.Request) -> web.Response:
    if not secrets_equal(request.query.get("key", ""), request.app[_TICK_SECRET]):
        log.warning("/tick: неверный ключ — запрос отклонён (403)")
        return web.Response(status=403, text="forbidden")
    ticker = request.app[TICKER]
    started = ticker.start()
    if not started:
        log.info("/tick: прошлый запуск заданий ещё идёт — новый не начинаю")
    return web.json_response(
        {
            "ok": True,
            "status": "started" if started else "busy",
            "last_finished": ticker.last_finished,
            "last_result": ticker.last_result,
        }
    )


def _workflow_data(app: web.Application) -> dict[str, Any]:
    dp = app[_DISPATCHER]
    return {"dispatcher": dp, "bots": [app[_BOT]], "app": app, **dp.workflow_data, "bot": app[_BOT]}


async def _on_startup(app: web.Application) -> None:
    await app[_DISPATCHER].emit_startup(**_workflow_data(app))


async def _stop_background(app: web.Application) -> None:
    """Остановка: сначала фоновый цикл — новые задания и запросы к себе больше не начинаются."""
    await app[BACKGROUND].stop()


async def _drain(app: web.Application) -> None:
    """Остановка: дать апдейтам и заданиям в работе закончиться (не дольше SHUTDOWN_GRACE_SEC)."""
    pending = app[RECEIVER].pending() | app[TICKER].pending()
    if not pending:
        return
    log.info("Остановка: жду завершения %s задач (до %s с)…", len(pending), int(SHUTDOWN_GRACE_SEC))
    _, still_running = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SEC)
    if still_running:
        log.warning("Остановка: %s задач не успели завершиться и прерваны", len(still_running))
        for task in still_running:
            task.cancel()
        await asyncio.gather(*still_running, return_exceptions=True)


async def _on_shutdown(app: web.Application) -> None:
    # После _drain: хранилище FSM закрывается (и дописывает состояния), когда апдейты уже обработаны.
    await app[_DISPATCHER].emit_shutdown(**_workflow_data(app))


def build_web_app(bot: Bot, dp: Dispatcher, sessionmaker: Sessionmaker, settings: Settings) -> web.Application:
    """Приложение aiohttp режима webhook (маршруты — в описании модуля). Сеть и БД при сборке не нужны.

    app[BACKGROUND] — фоновый цикл (самопробуждение по keepalive_url(settings) и задания по времени);
    он НЕ запускается сам: main.py вызывает start() после старта веб-сервера. Останавливается при
    остановке приложения (первым делом, до ожидания апдейтов и заданий в работе).
    """
    app = web.Application()
    app[_BOT] = bot
    app[_DISPATCHER] = dp
    app[_TICK_SECRET] = settings.tick_secret_value
    app[RECEIVER] = UpdateReceiver(dp, bot, settings.webhook_secret_value)
    app[TICKER] = TickRunner(bot, sessionmaker)
    app[BACKGROUND] = BackgroundLoop(
        app[TICKER], keepalive_url(settings), webhook_check=functools.partial(reclaim_webhook, bot, dp, settings)
    )

    app.router.add_post(webhook_path(settings), app[RECEIVER].handle)
    app.router.add_get("/tick", _tick)
    app.router.add_get("/health", _health)
    app.router.add_get("/", _index)

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_stop_background)
    app.on_shutdown.append(_drain)
    app.on_shutdown.append(_on_shutdown)
    return app


# --- Настройка webhook у Telegram -----------------------------------------------------------------


async def ensure_webhook(bot: Bot, dp: Dispatcher, settings: Settings) -> bool:
    """Направить апдейты Telegram на этот сервер. True — webhook установлен заново, False — уже был такой.

    setWebhook вызывается, только если у Telegram другой адрес (сменился адрес бота или секрет —
    он часть пути) или другой список типов апдейтов. Накопившиеся апдейты не сбрасываются.
    Ошибки Telegram (TelegramAPIError) пробрасываются — main.py решает, повторять или остановиться.
    """
    url = webhook_url(settings)
    allowed = sorted(dp.resolve_used_update_types())
    info = await bot.get_webhook_info()
    if info.last_error_message:
        log.info("Telegram: последняя ошибка доставки на webhook — %s", info.last_error_message)
    if info.url == url and sorted(info.allowed_updates or []) == allowed:
        log.info(
            "Webhook уже настроен на %s/tg/… (ждут доставки: %s) — не меняю",
            settings.base_url,
            info.pending_update_count,
        )
        return False
    await bot.set_webhook(
        url,
        secret_token=settings.webhook_secret_value,
        allowed_updates=allowed,
        drop_pending_updates=False,
    )
    log.info("Webhook установлен: %s/tg/… (типы апдейтов: %s)", settings.base_url, ", ".join(allowed))
    return True


async def reclaim_webhook(bot: Bot, dp: Dispatcher, settings: Settings) -> bool:
    """Фоновая проверка (BackgroundLoop, вместе с заданиями): webhook у Telegram указывает на этот сервер.

    Если адрес другой или webhook снят — его сняла копия бота, запущенная где-то ещё (run.bat на
    компьютере, старая версия бота): вернуть webhook сюда (как ensure_webhook) и предупредить в логе.
    True — webhook возвращён. Отличие только в списке типов апдейтов НЕ исправляется: при обновлении на
    хостинге старый и новый экземпляры (одинаковый адрес) иначе перетягивали бы этот список друг у друга.
    """
    url = webhook_url(settings)
    info = await bot.get_webhook_info()
    if info.url == url:
        log.debug("Webhook на месте")
        return False
    log.warning(
        "Webhook у Telegram %s — возвращаю его на этот сервер (%s). Похоже, бот запущен ещё где-то (run.bat "
        "на компьютере?): остановите ту копию, две копии мешают друг другу.",
        "снят" if not info.url else "указывает на другой адрес",
        settings.base_url,
    )
    await bot.set_webhook(
        url,
        secret_token=settings.webhook_secret_value,
        allowed_updates=sorted(dp.resolve_used_update_types()),
        drop_pending_updates=False,
    )
    return True
