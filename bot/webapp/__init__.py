"""Приложение в Telegram (Mini App) — docs/MINIAPP_SPEC.md.

Раздаёт его сам бот (aiohttp, режим webhook на Render): страница ``/app``, ассеты ``/app/static/*``
(белый список, кэш по версии) и JSON-API ``/api/*`` (sub-app, ``bot.webapp.api``). Вход — по подписанной
Telegram строке initData (``bot.webapp.auth``), права — по записи ``User`` в базе. Каждое действие
вызывает те же сервисы и те же уведомления, что и чат.

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

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[tuple[str, int], deque[float]] = {}

    def hit(self, kind: str, key: int, limits: Sequence[tuple[int, float]]) -> float | None:
        """Засчитать попытку, если ни один лимит ``(сколько, за сколько секунд)`` не превышен: -> None.
        Превышен — попытка не засчитывается: -> через сколько секунд можно снова (> 0)."""
        now = self._clock()
        longest = max((window for _, window in limits), default=0.0)
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
        digest = hashlib.sha256(files["app.js"] + files["app.css"]).hexdigest()[:12]
        return _Assets(index=index, files=files, version=digest)

    def current(self) -> _Assets | None:
        if self.reload:
            self._assets = self._load()
        return self._assets

    @property
    def version(self) -> str:
        """ASSET_VERSION: первые 12 hex-символов sha256(app.js + app.css); "" — приложение не собрано."""
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
    content_type = STATIC_FILES.get(name)
    assets = request.app[CTX].static.current() if content_type is not None else None
    if content_type is None or assets is None:
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
    """Смонтировать Mini App: GET /app, /app/, /app/static/{name} и sub-app /api.

    Сеть и БД при сборке не нужны. Повторный вызов на том же app — RuntimeError. ``pages=False`` — только
    /api (если страницу раздаёт кто-то другой).
    """
    if CTX in app:
        raise RuntimeError("Mini App уже смонтирован в это приложение")
    from bot.webapp import api  # здесь: пакет импортируется без API (bot.web проверяет find_spec)

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
