"""Приложение в Telegram (Mini App) — docs/MINIAPP_SPEC.md.

Раздаёт его сам бот (aiohttp, режим webhook на Render): страница ``/app``, ассеты ``/app/static/*``
(белый список, кэш по версии) и JSON-API ``/api/*`` (sub-app, ``bot.webapp.api``). Вход — по подписанной
Telegram строке initData (``bot.webapp.auth``), права — по записи ``User`` в базе. Каждое действие
вызывает те же сервисы и те же уведомления, что и чат.

То же приложение открывается и как сайт в обычном браузере (§15, ``bot.webapp.site``): страница ``/``,
вход ``/auth/*`` — через подтверждение в боте, дальше тот же ``/api/*`` по cookie сессии.

Публичный вход:

* ``setup_webapp(app, bot=…, sessionmaker=…, settings=…)`` — смонтировать всё в приложение aiohttp
  (сеть и БД при сборке не нужны; повторный вызов на том же app — RuntimeError);
* ``register_webapp(app, bot, dp, sessionmaker, settings)`` — то же с позиционными аргументами (dp не нужен:
  приложение не трогает диалоги чата);
* ``pending_tasks(app)`` — незавершённые фоновые задачи приложения (оценка сдачи, Excel, пересылка
  файлов) — их дожидается остановка сервера (``bot.web._drain``).

Здесь же — общий контекст приложения: реестр фоновых задач (``TaskRegistry``), «не больше одного
одновременно» на пользователя (``UserGate``), «не чаще N за окно времени» (``RateLimiter``) и статика
(``StaticBundle``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings

__all__ = [
    "CTX",
    "STATIC_DIR",
    "TASKS",
    "ApiError",
    "RateLimiter",
    "StaticBundle",
    "TaskRegistry",
    "UserGate",
    "WebappContext",
    "pending_tasks",
    "register_webapp",
    "setup_webapp",
]

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class ApiError(Exception):
    """Ответ API с ошибкой: HTTP-статус, код (§6.2) и русский текст для пользователя."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# --- Фоновые задачи и «один запрос за раз» --------------------------------------------------------------


class TaskRegistry:
    """Фоновые задачи приложения: сильные ссылки (задачу не соберёт GC), исключения — в лог."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(self._guard(coro, name), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    async def _guard(coro: Coroutine[Any, Any, Any], name: str) -> Any:
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - фоновая задача не должна молча пропасть
            log.exception("Mini App: фоновая задача %s завершилась ошибкой", name)
            return None

    def pending(self) -> set[asyncio.Task[Any]]:
        return {task for task in self._tasks if not task.done()}

    async def drain(self, timeout: float | None = None) -> None:
        """Дождаться всех фоновых задач, в том числе запущенных во время ожидания (для тестов).
        Не уложились в timeout — TimeoutError."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while pending := self.pending():
            left = None if deadline is None else deadline - loop.time()
            if left is not None and left <= 0:
                raise TimeoutError(f"Фоновые задачи Mini App не завершились: {len(pending)}")
            await asyncio.wait(pending, timeout=left)


class UserGate:
    """«Не больше одного одновременно» на пользователя и вид действия (в памяти процесса)."""

    def __init__(self) -> None:
        self._busy: set[tuple[str, int]] = set()

    def try_enter(self, kind: str, tg_id: int) -> bool:
        """Занять действие; False — уже занято (предыдущий запрос ещё идёт)."""
        key = (kind, tg_id)
        if key in self._busy:
            return False
        self._busy.add(key)
        return True

    def leave(self, kind: str, tg_id: int) -> None:
        self._busy.discard((kind, tg_id))

    def busy(self, kind: str, tg_id: int) -> bool:
        return (kind, tg_id) in self._busy

    @asynccontextmanager
    async def hold(self, kind: str, tg_id: int, busy_message: str) -> AsyncIterator[None]:
        """Занять на время блока; занято — ApiError(429, "busy", busy_message)."""
        if not self.try_enter(kind, tg_id):
            raise ApiError(429, "busy", busy_message)
        try:
            yield
        finally:
            self.leave(kind, tg_id)


class RateLimiter:
    """«Не больше N за окно времени» на пользователя и вид действия (в памяти процесса).

    UserGate не даёт выполнять действие одновременно, а этот счётчик — слишком часто подряд: подсказка AI
    тратит общие с оценкой сдач бесплатные квоты, поручение рассылается всем начальникам.
    """

    SWEEP_EVERY = 1024  # раз во столько попыток записи, у которых всё вышло из окна, убираются из памяти

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[tuple[str, int], deque[float]] = {}
        self._windows: dict[str, float] = {}  # самое длинное окно каждого вида действия
        self._calls = 0

    def hit(self, kind: str, key: int, limits: Sequence[tuple[int, float]]) -> float | None:
        """Засчитать попытку, если ни один лимит ``(сколько, за сколько секунд)`` не превышен: -> None.
        Превышен — попытка не засчитывается: -> через сколько секунд можно снова (> 0)."""
        now = self._clock()
        longest = max((window for _, window in limits), default=0.0)
        self._windows[kind] = max(self._windows.get(kind, 0.0), longest)
        self._calls += 1
        if self._calls % self.SWEEP_EVERY == 0:
            self._sweep(now)
        hits = self._hits.get((kind, key))
        if hits is not None:
            while hits and hits[0] <= now - longest:
                hits.popleft()
        wait = 0.0
        for count, window in limits:
            recent = [at for at in (hits or ()) if at > now - window]
            if len(recent) >= count:
                # Освободится, когда самая старая из последних count попыток выйдет из окна.
                wait = max(wait, recent[-count] + window - now)
        if wait > 0:
            return wait
        if hits is None:
            hits = self._hits[(kind, key)] = deque()
        hits.append(now)
        return None

    def _sweep(self, now: float) -> None:
        """Забыть ключи, у которых все попытки вышли из окна: счётчик входа на сайт ведётся по адресам
        посетителей, а их может быть сколько угодно — память сервера они занимать не должны."""
        stale = [
            item
            for item, hits in self._hits.items()
            if not hits or hits[-1] <= now - self._windows.get(item[0], 0.0)
        ]
        for item in stale:
            del self._hits[item]

    def undo(self, kind: str, key: int) -> None:
        """Не засчитывать последнюю попытку (действие не выполнено — например, ошибка в данных)."""
        hits = self._hits.get((kind, key))
        if hits:
            hits.pop()


# --- Статика: index.html, app.js, app.css ----------------------------------------------------------------

INDEX_FILE = "index.html"
# Ассеты, которые отдаёт /app/static/{name}: всё остальное — 404 (в т.ч. «..», подпапки).
STATIC_FILES: dict[str, str] = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
# Файлы сайта (§15): страница и её стили поверх app.css. Их может не быть — тогда сайта нет, Mini App работает.
SITE_INDEX_FILE = "site.html"
SITE_FILES: dict[str, str] = {"site.css": "text/css; charset=utf-8"}
VERSION_PLACEHOLDER = "__ASSET_VERSION__"
CONFIG_PLACEHOLDER = "__KPI_CONFIG__"
NOT_BUILT_TEXT = "Приложение ещё не собрано"
CSP = (
    "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
)
_IMMUTABLE = "public, max-age=31536000, immutable"


@dataclass(frozen=True)
class _Assets:
    index: str
    files: dict[str, bytes]
    version: str
    site: str | None = None  # страница сайта; None — файлов сайта нет


class StaticBundle:
    """Файлы приложения (их пишет агент UI в bot/webapp/static). Читаются при setup_webapp; в режиме
    отладки (``settings.webapp_debug_active``) — заново на каждый запрос: правка без перезапуска."""

    def __init__(self, static_dir: Path, *, reload: bool = False) -> None:
        self.static_dir = Path(static_dir)
        self.reload = reload
        self._assets: _Assets | None = None
        self._warned = False
        self._assets = self._load()

    def _load(self) -> _Assets | None:
        names = [INDEX_FILE, *STATIC_FILES]
        missing = [name for name in names if not (self.static_dir / name).is_file()]
        if missing:
            if not self._warned:
                log.warning("Mini App: нет файлов приложения (%s) — /app отвечает 503", ", ".join(missing))
                self._warned = True
            return None
        try:
            index = (self.static_dir / INDEX_FILE).read_text(encoding="utf-8")
            files = {name: (self.static_dir / name).read_bytes() for name in STATIC_FILES}
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("Mini App: не удалось прочитать файлы приложения (%s)", type(exc).__name__)
            return None
        self._warned = False
        site = self._load_site(files)
        # Стили сайта входят в версию: правка site.css тоже сбрасывает кэш браузера.
        digest = hashlib.sha256(b"".join(files[name] for name in (*STATIC_FILES, *SITE_FILES) if name in files))
        return _Assets(index=index, files=files, version=digest.hexdigest()[:12], site=site)

    def _load_site(self, files: dict[str, bytes]) -> str | None:
        """Страница сайта и её стили — если есть все файлы (их байты добавляются в ``files``)."""
        names = [SITE_INDEX_FILE, *SITE_FILES]
        if not all((self.static_dir / name).is_file() for name in names):
            return None
        try:
            page = (self.static_dir / SITE_INDEX_FILE).read_text(encoding="utf-8")
            extra = {name: (self.static_dir / name).read_bytes() for name in SITE_FILES}
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("Сайт: не удалось прочитать файлы страницы (%s)", type(exc).__name__)
            return None
        files.update(extra)
        return page

    def current(self) -> _Assets | None:
        if self.reload:
            self._assets = self._load()
        return self._assets

    @property
    def version(self) -> str:
        """ASSET_VERSION: первые 12 hex-символов sha256(app.js + app.css [+ site.css]); "" — приложение не
        собрано."""
        assets = self._assets if not self.reload else self.current()
        return assets.version if assets is not None else ""


def _kpi_config(version: str, debug: bool) -> str:
    """JSON-конфиг страницы для <script type="application/json">: «<» экранируется (не закроет тег)."""
    return json.dumps({"version": version, "debug": debug}, ensure_ascii=False).replace("<", "\\u003c")


async def _index(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    assets = ctx.static.current()
    if assets is None:
        return web.Response(
            status=503, text=NOT_BUILT_TEXT, content_type="text/plain", headers={"Cache-Control": "no-store"}
        )
    body = assets.index.replace(VERSION_PLACEHOLDER, assets.version).replace(
        CONFIG_PLACEHOLDER, _kpi_config(assets.version, ctx.settings.webapp_debug_active)
    )
    return web.Response(
        text=body,
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": CSP,
        },
    )


async def _static(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "")
    content_type = STATIC_FILES.get(name) or SITE_FILES.get(name)
    assets = request.app[CTX].static.current() if content_type is not None else None
    if content_type is None or assets is None or name not in assets.files:
        raise web.HTTPNotFound()
    cache = _IMMUTABLE if request.query.get("v") == assets.version else "no-cache"
    return web.Response(
        body=assets.files[name],
        headers={"Content-Type": content_type, "Cache-Control": cache, "X-Content-Type-Options": "nosniff"},
    )


# --- Контекст и монтирование ----------------------------------------------------------------------------


@dataclass
class WebappContext:
    bot: Bot
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings
    tasks: TaskRegistry
    gate: UserGate
    static: StaticBundle
    limits: RateLimiter = field(default_factory=RateLimiter)
    # Сайт (§15): адрес чата с ботом (узнаётся у Telegram один раз) и подмена ссылки входа для локальной
    # проверки (dev-сервер ведёт не в Telegram, а на свою страницу).
    bot_url: str | None = None
    login_link: Callable[[str], str] | None = None


CTX: web.AppKey[WebappContext] = web.AppKey("kpi_webapp_ctx", WebappContext)
TASKS: web.AppKey[TaskRegistry] = web.AppKey("kpi_webapp_tasks", TaskRegistry)


def setup_webapp(
    app: web.Application,
    *,
    bot: Bot,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    static_dir: Path = STATIC_DIR,
    pages: bool = True,
) -> None:
    """Смонтировать Mini App: GET /app, /app/, /app/static/{name}, sub-app /api и вход на сайт — sub-app
    /auth (§15; страницу сайта ``/`` отдаёт ``bot.web`` через ``site.index``).

    Сеть и БД при сборке не нужны. Повторный вызов на том же app — RuntimeError. ``pages=False`` — только
    /api и /auth (если страницу раздаёт кто-то другой).
    """
    if CTX in app:
        raise RuntimeError("Mini App уже смонтирован в это приложение")
    from bot.webapp import api, site  # здесь: пакет импортируется без API (bot.web проверяет find_spec)

    ctx = WebappContext(
        bot=bot,
        sessionmaker=sessionmaker,
        settings=settings,
        tasks=TaskRegistry(),
        gate=UserGate(),
        static=StaticBundle(static_dir, reload=settings.webapp_debug_active),
    )
    app[CTX] = ctx
    app[TASKS] = ctx.tasks
    if pages:
        app.router.add_get("/app", _index)
        app.router.add_get("/app/", _index)
        app.router.add_get("/app/static/{name}", _static)
    app.add_subapp("/api", api.build_api_app(ctx))
    app.add_subapp("/auth", site.build_auth_app(ctx))


def register_webapp(
    app: web.Application,
    bot: Bot,
    dp: Any = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
    **kwargs: Any,
) -> None:
    """``setup_webapp`` с позиционными аргументами (bot/web.py). dp не используется."""
    if sessionmaker is None or settings is None:
        raise TypeError("register_webapp: нужны sessionmaker и settings")
    setup_webapp(app, bot=bot, sessionmaker=sessionmaker, settings=settings, **kwargs)


def pending_tasks(app: web.Application) -> set[asyncio.Task[Any]]:
    """Незавершённые фоновые задачи приложения (для web._drain); пусто, если не смонтировано."""
    registry = app.get(TASKS)
    return registry.pending() if registry is not None else set()
