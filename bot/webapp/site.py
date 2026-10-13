"""Сайт: то же приложение в обычном браузере (docs/MINIAPP_SPEC.md §15).

Сайт раздаёт тот же сервер, что и Mini App: страница ``/`` (без скрипта Telegram и со своей политикой
встраивания), те же ``/app/static/*`` и тот же ``/api/*``. Разница одна — способ подтвердить, кто пришёл:
вместо подписанного Telegram initData — cookie сессии, выданная после подтверждения входа в боте
(``bot.services.weblogin``).

Маршруты входа (sub-app ``/auth``, без базы до проверки запроса):

* ``POST /auth/start`` — начать вход: число для сверки, ссылка на бота и её QR-код; секрет попытки —
  в cookie, недоступной скриптам;
* ``GET /auth/status`` — состояние попытки для браузера, который её начал; после подтверждения в боте —
  cookie сессии (один раз);
* ``POST /auth/cancel`` — отказ от начатой попытки («Отмена»);
* ``POST /auth/logout`` — выход: сессия удаляется.

Защита запросов с чужих сайтов: cookie — ``SameSite=Strict``; каждый запрос страницы несёт заголовок
``X-KPI-CSRF`` (для API — знак, выведенный из сессии: чужой сайт его не знает и такой заголовок поставить не
может); ``Origin`` / ``Sec-Fetch-Site`` чужого сайта — отказ. Секреты и знаки сессий в журнал не пишутся.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramAPIError
from aiohttp import web

from bot.services import weblogin
from bot.services.weblogin import csrf_for
from bot.utils.dates import utcnow
from bot.webapp import CTX, ApiError, WebappContext, qr

__all__ = [
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "LOGIN_COOKIE",
    "SESSION_COOKIE",
    "build_auth_app",
    "check_page_request",
    "csrf_for",
    "device_label",
    "index",
    "session_token",
]

log = logging.getLogger(__name__)

SESSION_COOKIE = "kpi_session"  # знак сессии (HttpOnly)
CSRF_COOKIE = "kpi_csrf"        # знак страницы: его читает приложение и возвращает в заголовке
LOGIN_COOKIE = "kpi_login"      # секрет попытки входа (HttpOnly), живёт несколько минут
SECURE_PREFIX = "__Host-"       # по https: cookie принимается только от этого адреса, с Secure и Path=/
CSRF_HEADER = "X-KPI-CSRF"

# Счётчики — на адрес посетителя, а за одним адресом может сидеть весь отдел (общий интернет офиса):
START_LIMITS = ((10, 60.0), (100, 3600.0))  # начал входа: за минуту и за час
STATUS_LIMITS = ((600, 60.0),)              # опрос состояния (каждая страница спрашивает раз в 2 с)
# На всех сразу: адрес посетителя за прокси хостинга можно подделать, поэтому поток запросов с «разных
# адресов» упирается в общий счётчик (в памяти, до базы) и в потолок ждущих попыток. Работе тех, кто уже
# вошёл, и приложению в Telegram такой поток не мешает.
START_LIMITS_ALL = ((60, 60.0),)
MAX_PENDING = 1000
# Срок сессии держит сервер (weblogin.SESSION_TTL без обращений, работа продлевает). Cookie в браузере живёт
# дольше — иначе того, кто работает каждый день, выбрасывало бы через 30 дней после входа.
COOKIE_MAX_AGE = 400 * 24 * 60 * 60
BOT_INFO_TIMEOUT_SEC = 5.0

TXT_CSRF = "Запрос отклонён. Обновите страницу и повторите."
TXT_SESSION_ENDED = "Вход на сайт завершён. Войдите снова."
TXT_TOO_OFTEN = "Слишком много попыток входа подряд. Повторите чуть позже."
TXT_TELEGRAM = "Не удалось связаться с Telegram. Попробуйте ещё раз через минуту."
TXT_NOT_FOUND = "Не найдено."
TXT_ERROR = "Произошла ошибка, попробуйте ещё раз"

SITE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
_dumps = functools.partial(json.dumps, ensure_ascii=False, separators=(",", ":"))

# --- Cookie -----------------------------------------------------------------------------------------------


def _cookie_name(ctx: WebappContext, base: str) -> str:
    return SECURE_PREFIX + base if ctx.settings.cookies_secure else base


def _read_cookie(request: web.Request, ctx: WebappContext, base: str) -> str:
    return request.cookies.get(_cookie_name(ctx, base), "")


def _set_cookie(
    response: web.StreamResponse, ctx: WebappContext, base: str, value: str, *, max_age: int, http_only: bool
) -> None:
    response.set_cookie(
        _cookie_name(ctx, base),
        value,
        max_age=max_age,
        path="/",
        secure=ctx.settings.cookies_secure,
        httponly=http_only,
        samesite="Strict",
    )


def _drop_cookie(response: web.StreamResponse, ctx: WebappContext, base: str, *, http_only: bool) -> None:
    response.del_cookie(
        _cookie_name(ctx, base), path="/", secure=ctx.settings.cookies_secure, httponly=http_only, samesite="Strict"
    )


def session_token(request: web.Request, ctx: WebappContext) -> str:
    """Знак сессии из cookie запроса ("" — cookie нет)."""
    return _read_cookie(request, ctx, SESSION_COOKIE)


# --- Проверка, что запрос пришёл со своей страницы ----------------------------------------------------------


def _same_origin(request: web.Request, ctx: WebappContext) -> bool:
    """Запрос пришёл со страницы сайта, а не с чужой. Свой адрес — публичный адрес из настроек (хостинг
    может подменить Host) и адрес, по которому пришёл запрос; по https — только https."""
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None and fetch_site.strip().lower() != "same-origin":
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        parts = urlsplit(origin)
        base = urlsplit(ctx.settings.base_url)
    except ValueError:
        return False
    if ctx.settings.cookies_secure and parts.scheme.lower() != "https":
        return False
    own_hosts = {request.host.lower()} | ({base.netloc.lower()} if base.netloc else set())
    return bool(parts.netloc) and parts.netloc.lower() in own_hosts


def check_page_request(request: web.Request, ctx: WebappContext, token: str) -> None:
    """Запрос API по cookie сессии обязан прийти со страницы сайта: свой ``Origin`` и знак страницы в
    заголовке. Иначе — 403 ``csrf`` (до обращения к базе)."""
    given = request.headers.get(CSRF_HEADER, "")
    try:
        matches = hmac.compare_digest(given.encode("ascii"), csrf_for(token).encode("ascii"))
    except UnicodeError:
        matches = False
    if not matches or not _same_origin(request, ctx):
        raise ApiError(403, "csrf", TXT_CSRF)


def _client_key(request: web.Request) -> int:
    """Адрес посетителя для счётчика попыток. Сервис стоит за прокси хостинга: настоящий адрес — в его
    заголовках; без них — адрес соединения. Адреса IPv6 считаются сетями /64 (у одного посетителя их
    миллиарды)."""
    raw = (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote
        or ""
    )[:64]
    try:
        address = ipaddress.ip_address(raw.strip())
    except ValueError:
        pass
    else:
        if address.version == 6:
            raw = str(ipaddress.ip_network((address, 64), strict=False).network_address)
        else:
            raw = str(address)
    return int.from_bytes(hashlib.sha256(raw.encode("utf-8", "replace")).digest()[:8], "big")


_BROWSERS = (
    ("Edg", "Edge"), ("OPR/", "Opera"), ("YaBrowser", "Yandex"), ("SamsungBrowser", "Samsung Internet"),
    ("Firefox", "Firefox"), ("FxiOS", "Firefox"), ("CriOS", "Chrome"), ("Chrome", "Chrome"), ("Safari", "Safari"),
)
_SYSTEMS = (
    ("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
    ("Mac OS X", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"),
)


def device_label(user_agent: str | None) -> str | None:
    """«Chrome, Windows» — что показать человеку в боте. Только слова из своего списка: сам заголовок
    браузера (его присылает посетитель) в сообщение не попадает."""
    agent = (user_agent or "")[:512]
    browser = next((label for needle, label in _BROWSERS if needle in agent), None)
    system = next((label for needle, label in _SYSTEMS if needle in agent), None)
    return ", ".join(part for part in (browser, system) if part) or None


# --- Ответы ------------------------------------------------------------------------------------------------


def _ok(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_dumps)


def _error(status: int, code: str, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": message, "code": code, **extra}, status=status, dumps=_dumps)


def _ctx(request: web.Request) -> WebappContext:
    return request.config_dict[CTX]


@web.middleware
async def _error_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        response = await handler(request)
    except ApiError as exc:
        response = _error(exc.status, exc.code, exc.message)
    except web.HTTPNotFound:
        response = _error(404, "not_found", TXT_NOT_FOUND)
    except web.HTTPMethodNotAllowed:
        response = _error(405, "method_not_allowed", "Метод не поддерживается.")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Сайт: ошибка обработки запроса входа")
        response = _error(500, "internal", TXT_ERROR)
    response.headers["Cache-Control"] = "no-store"
    return response


def _guard(request: web.Request) -> WebappContext:
    """Общая проверка маршрутов входа: сайт включён, запрос — со своей страницы (заголовок, который чужой
    сайт поставить не может, и свой Origin)."""
    ctx = _ctx(request)
    if not ctx.settings.site_active:
        raise web.HTTPNotFound()
    if not request.headers.get(CSRF_HEADER) or not _same_origin(request, ctx):
        raise ApiError(403, "csrf", TXT_CSRF)
    return ctx


# --- Ссылка на бота -----------------------------------------------------------------------------------------


async def bot_url(ctx: WebappContext) -> str | None:
    """Адрес чата с ботом (https://t.me/<имя>); None — Telegram сейчас не ответил. Имя бота запоминается."""
    if ctx.bot_url is None:
        try:
            me = await asyncio.wait_for(ctx.bot.me(), BOT_INFO_TIMEOUT_SEC)
        except (TelegramAPIError, TimeoutError, OSError) as exc:
            log.warning("Сайт: не удалось узнать имя бота (%s)", type(exc).__name__)
            return None
        if not me.username:
            return None
        ctx.bot_url = f"https://t.me/{me.username}"
    return ctx.bot_url


async def _login_link(ctx: WebappContext, token: str) -> str | None:
    payload = weblogin.START_PREFIX + token
    if ctx.login_link is not None:  # локальная проверка: вместо Telegram — страница dev-сервера
        return ctx.login_link(payload)
    base = await bot_url(ctx)
    return f"{base}?start={payload}" if base else None


# --- Маршруты входа -----------------------------------------------------------------------------------------


async def auth_start(request: web.Request) -> web.Response:
    ctx = _guard(request)
    wait = ctx.limits.hit("site_start", _client_key(request), START_LIMITS)
    if wait is None:
        wait = ctx.limits.hit("site_start_all", 0, START_LIMITS_ALL)
    if wait is not None:
        log.info("Сайт: слишком частые попытки входа")
        return _error(429, "rate_limited", TXT_TOO_OFTEN, retry_after=math.ceil(wait))
    if ctx.login_link is None and await bot_url(ctx) is None:  # до базы: это обращение к Telegram
        return _error(503, "telegram_error", TXT_TELEGRAM)
    now = utcnow()
    previous = _read_cookie(request, ctx, LOGIN_COOKIE)
    async with ctx.sessionmaker() as session:
        await weblogin.cleanup(session, now)
        if previous:  # вход начат заново: прежняя попытка этого браузера закрывается
            await weblogin.cancel(session, previous, now)
        if await weblogin.pending_count(session, now) >= MAX_PENDING:
            await session.commit()
            log.warning("Сайт: слишком много незавершённых попыток входа")
            return _error(429, "rate_limited", TXT_TOO_OFTEN, retry_after=60)
        started = await weblogin.start(session, device=device_label(request.headers.get("User-Agent")), now=now)
        await session.commit()
    link = await _login_link(ctx, started.token)
    if link is None:
        return _error(503, "telegram_error", TXT_TELEGRAM)
    ttl = int(weblogin.ATTEMPT_TTL.total_seconds())
    response = _ok({"code": started.code, "link": link, "qr": qr.encode(link), "expires_in": ttl})
    _set_cookie(response, ctx, LOGIN_COOKIE, started.browser_secret, max_age=ttl + 60, http_only=True)
    return response


async def auth_status(request: web.Request) -> web.Response:
    ctx = _guard(request)
    secret = _read_cookie(request, ctx, LOGIN_COOKIE)
    if not secret:
        return _ok({"state": "none"})
    wait = ctx.limits.hit("site_status", _client_key(request), STATUS_LIMITS)
    if wait is not None:
        return _error(429, "rate_limited", TXT_TOO_OFTEN, retry_after=math.ceil(wait))
    async with ctx.sessionmaker() as session:
        got = await weblogin.collect(session, secret, now=utcnow())
        if got.state == "ok":
            # В этом браузере уже кто-то был: прежняя сессия закрывается, а не остаётся жить в базе.
            await weblogin.end_session(session, session_token(request, ctx))
        await session.commit()
    if got.state == "waiting":
        return _ok({"state": "waiting", "opened": got.opened, "expires_in": got.expires_in, "code": got.code})
    response = _ok({"state": got.state})
    _drop_cookie(response, ctx, LOGIN_COOKIE, http_only=True)
    if got.state == "ok" and got.session_token:
        _set_cookie(response, ctx, SESSION_COOKIE, got.session_token, max_age=COOKIE_MAX_AGE, http_only=True)
        _set_cookie(response, ctx, CSRF_COOKIE, csrf_for(got.session_token), max_age=COOKIE_MAX_AGE, http_only=False)
        log.info("Сайт: выполнен вход")
    return response


async def auth_cancel(request: web.Request) -> web.Response:
    """«Отмена» на экране ожидания: попытка закрывается, её кнопки в боте больше ничего не подтверждают."""
    ctx = _guard(request)
    secret = _read_cookie(request, ctx, LOGIN_COOKIE)
    if secret:
        async with ctx.sessionmaker() as session:
            await weblogin.cancel(session, secret, utcnow())
            await session.commit()
    response = _ok({"ok": True})
    _drop_cookie(response, ctx, LOGIN_COOKIE, http_only=True)
    return response


async def auth_logout(request: web.Request) -> web.Response:
    ctx = _guard(request)
    token = session_token(request, ctx)
    if token:
        check_page_request(request, ctx, token)
        async with ctx.sessionmaker() as session:
            await weblogin.end_session(session, token)
            await session.commit()
        log.info("Сайт: выполнен выход")
    response = _ok({"ok": True})
    _drop_cookie(response, ctx, SESSION_COOKIE, http_only=True)
    _drop_cookie(response, ctx, CSRF_COOKIE, http_only=False)
    _drop_cookie(response, ctx, LOGIN_COOKIE, http_only=True)
    return response


AUTH_ROUTES: list[tuple[str, str]] = [
    ("POST", "/auth/start"),
    ("GET", "/auth/status"),
    ("POST", "/auth/cancel"),
    ("POST", "/auth/logout"),
]


def build_auth_app(ctx: WebappContext) -> web.Application:
    """Sub-app /auth: вход на сайт. Неизвестный путь и метод — JSON 404 / 405."""
    app = web.Application(middlewares=[_error_middleware])
    app[CTX] = ctx
    app.router.add_post("/start", auth_start)
    app.router.add_get("/status", auth_status)
    app.router.add_post("/cancel", auth_cancel)
    app.router.add_post("/logout", auth_logout)
    return app


# --- Страница сайта -----------------------------------------------------------------------------------------


async def index(request: web.Request) -> web.Response | None:
    """Страница сайта (``/``): тот же app.js, что у Mini App, без скрипта Telegram. None — сайт выключен
    или его файлов нет (тогда адрес отвечает как раньше)."""
    ctx = request.app.get(CTX)
    if ctx is None or not ctx.settings.site_active:
        return None
    assets = ctx.static.current()
    if assets is None or assets.site is None:
        return None
    config: dict[str, Any] = {"version": assets.version, "debug": ctx.settings.webapp_debug_active, "web": True}
    link = await bot_url(ctx) if ctx.login_link is None else None
    if link:
        config["bot"] = link
    body = assets.site.replace("__ASSET_VERSION__", assets.version).replace(
        "__KPI_CONFIG__", json.dumps(config, ensure_ascii=False).replace("<", "\\u003c")
    )
    return web.Response(
        text=body,
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": SITE_CSP,
        },
    )
