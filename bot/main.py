"""Запуск бота: настройки, логирование, сборка Dispatcher, обработчик ошибок и один из двух режимов.

* polling (RUN_MODE=polling, по умолчанию) — свой компьютер или VPS: бот сам забирает сообщения
  у Telegram, задания по времени выполняет APScheduler внутри процесса. Если у бота включён webhook
  (он уже работает в облаке), запуск останавливается понятной ошибкой — снять webhook можно только
  явно, TAKEOVER_WEBHOOK=1.
* webhook (RUN_MODE=webhook) — Render и другие веб-хостинги: Telegram присылает сообщения на
  ``<PUBLIC_URL>/tg/<секрет>``. Внешние «будильники» не нужны: фоновый цикл бота (bot.web.BackgroundLoop)
  каждые 5 мин запускает задания по времени (run_due_jobs, повторы отсекает БД) и каждые 10 мин
  открывает свой публичный адрес /health — бесплатный Render не усыпляет сервис. ``/tick`` остаётся
  необязательным резервом. APScheduler здесь не используется.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import signal
import socket
from contextlib import suppress
from urllib.parse import urlsplit

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, ErrorEvent, Update
from aiogram.types import User as TgUser
from aiogram.utils.token import TokenValidationError, validate_token
from aiohttp import web
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot import notify
from bot.ai.provider import close_client
from bot.config import AI_KEY_ENV, AI_PROVIDER_NAMES, Settings, get_settings
from bot.db.base import (
    init_db,
    is_password_placeholder,
    make_engine,
    make_sessionmaker,
    make_storage_engine,
    normalize_url,
)
from bot.handlers import (
    dashboard,
    start,
    task_create,
    task_propose,
    task_review,
    task_submit,
    task_view,
    users_admin,
)
from bot.middlewares import DbSessionMiddleware, UserMiddleware
from bot.scheduler.jobs import setup_scheduler, tick_schedule_summary
from bot.services.errors import DomainError
from bot.utils.text import esc
from bot.web import BACKGROUND, build_web_app, ensure_webhook

log = logging.getLogger(__name__)

GENERIC_ERROR = "⚠️ Произошла ошибка, попробуйте ещё раз"

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# Сторонние библиотеки, которые на уровне INFO/DEBUG пишут слишком подробно.
_NOISY_LOGGERS = ("apscheduler", "aiosqlite", "httpx", "httpcore")

_ALERT_LIMIT = 200          # лимит Telegram на текст alert при нажатии кнопки
_CONNECT_RETRY_START = 5    # с, первая пауза при отсутствии связи с Telegram на старте
_CONNECT_RETRY_MAX = 60     # с, максимальная пауза

_NO_TOKEN = (
    "Не задан BOT_TOKEN. Создайте бота у @BotFather в Telegram, скопируйте токен "
    "и впишите его в файл .env строкой BOT_TOKEN=... (образец — .env.example, подробности — README.md)."
)
_BAD_TOKEN = (
    "BOT_TOKEN в файле .env записан неверно. Токен выглядит так: 1234567890:AAH...xyz — "
    "без пробелов и кавычек. Скопируйте его из @BotFather ещё раз."
)
_TOKEN_REJECTED = (
    "Telegram не принял BOT_TOKEN: токен неверный или был отозван. "
    "Получите актуальный токен у @BotFather (/mybots → API Token) и обновите файл .env."
)
_NO_PUBLIC_URL = (
    "Режим webhook (RUN_MODE=webhook) требует публичный адрес бота: задайте PUBLIC_URL, например "
    "PUBLIC_URL=https://kpi-bot.onrender.com. На Render адрес подставляется сам (RENDER_EXTERNAL_URL) — "
    "если вы видите это сообщение там, проверьте, что сервис создан как Web Service. "
    "Для своего компьютера или VPS используйте RUN_MODE=polling."
)
_NOT_HTTPS = (
    "PUBLIC_URL должен начинаться с https:// — Telegram присылает сообщения только на защищённые адреса. "
    "Сейчас указано: {url}"
)
_BAD_WEBHOOK_SECRET = (
    "WEBHOOK_SECRET может содержать только латинские буквы, цифры, «_» и «-» (до 256 символов). "
    "Исправьте значение или удалите строку — тогда секрет будет выведен из BOT_TOKEN."
)
_BAD_PORT = "PORT={port} — неверный номер порта (нужно число от 1 до 65535)."
_PORT_BUSY = (
    "Не удалось открыть порт {port} для веб-сервера ({reason}). "
    "Укажите другой PORT или остановите программу, которая его занимает."
)
_WEBHOOK_REJECTED = (
    "Telegram не принял адрес webhook {url}/tg/…: {reason}. Проверьте PUBLIC_URL — адрес должен "
    "открываться из интернета по https."
)
_WEBHOOK_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")
# Заглушки, которые пишет deploy/make_render_env.py («ВСТАВЬТЕ_СЮДА_…»), — владелец заменяет их в Блокноте.
_PLACEHOLDER_MARK = "ВСТАВЬТЕ_СЮДА"
_DB_URL_PLACEHOLDER = (
    "В DATABASE_URL осталась заглушка «ВСТАВЬТЕ_СЮДА_…» вместо строки подключения к базе. Вставьте строку "
    "«Session pooler» из Supabase (Connect → Session pooler) как есть: на Render — сервис → Environment → "
    "DATABASE_URL → Save Changes (docs/DEPLOY_RENDER.md, шаг 2)."
)
_DB_PASSWORD_PLACEHOLDER = (
    "В DATABASE_PASSWORD осталась заглушка «ВСТАВЬТЕ_СЮДА_…» вместо пароля базы Supabase. Впишите пароль, "
    "который Supabase показал при создании проекта: на Render — сервис → Environment → DATABASE_PASSWORD → "
    "Save Changes. Забыли пароль — Supabase → Project Settings → Database → Reset database password."
)
_DB_URL_EMPTY = (
    "DATABASE_URL пуст. Удалите эту строку (бот возьмёт файл data/bot.db) или вставьте строку подключения "
    "PostgreSQL (Supabase: Connect → Session pooler)."
)
_DB_URL_BAD = (
    "DATABASE_URL записан неверно. Ожидается строка PostgreSQL вида postgresql://пользователь:пароль@сервер:5432/база "
    "(Supabase: Connect → Session pooler, вставлять как есть) или файл SQLite sqlite+aiosqlite:///data/bot.db."
)
_DB_PASSWORD_MISSING = (
    "В DATABASE_URL вместо пароля стоит [YOUR-PASSWORD], а DATABASE_PASSWORD не задан. Впишите пароль базы "
    "Supabase в DATABASE_PASSWORD: на Render — сервис → Environment → DATABASE_PASSWORD → Save Changes. "
    "Забыли пароль — Supabase → Project Settings → Database → Reset database password."
)
_DB_DIRECT_CONNECTION = (
    "DATABASE_URL — строка «Direct connection» Supabase (db.….supabase.co): с Render она не работает. "
    "Возьмите в Supabase → Connect строку «Session pooler» (…pooler.supabase.com:5432) и вставьте её как есть: "
    "Render → сервис → Environment → DATABASE_URL → Save Changes (docs/DEPLOY_RENDER.md, шаг 2)."
)
# Ошибки подключения к базе при запуске (main → init_db): понятная причина без значений из настроек.
_DB_AUTH_FAILED = (
    "База данных не приняла пароль или имя пользователя. Проверьте DATABASE_PASSWORD (пароль базы Supabase, "
    "без пробелов по краям) и что DATABASE_URL — строка «Session pooler» вашего проекта, вставленная целиком: "
    "Render → сервис → Environment → Save Changes. Забыли пароль — Supabase → Project Settings → Database → "
    "Reset database password, затем впишите новый в DATABASE_PASSWORD."
)
_DB_TENANT_NOT_FOUND = (
    "Supabase не нашёл проект или пользователя из DATABASE_URL: строка скопирована не полностью, взята из "
    "другого проекта или проект приостановлен (письмо «paused» → Supabase → Resume project). Вставьте заново "
    "строку «Session pooler» из Supabase → Connect: Render → сервис → Environment → DATABASE_URL → Save Changes."
)
_DB_NO_DATABASE = (
    "В DATABASE_URL указано имя базы, которой нет на сервере. Вставьте строку «Session pooler» из Supabase "
    "как есть (в конце — /postgres): Render → сервис → Environment → DATABASE_URL → Save Changes."
)
_DB_UNREACHABLE = (
    "Не удалось подключиться к базе данных: {reason}. Проверьте DATABASE_URL (нужна строка «Session pooler» "
    "из Supabase → Connect, порт 5432) и что проект Supabase не приостановлен (письмо «paused» → Resume "
    "project). Исправить: Render → сервис → Environment → DATABASE_URL → Save Changes."
)
_DB_AUTH_SQLSTATES = frozenset({"28P01", "28000"})  # invalid_password, invalid_authorization_specification
_DB_NO_DATABASE_SQLSTATE = "3D000"                  # invalid_catalog_name
_WEBHOOK_ACTIVE = (
    "Бот уже работает в облаке: Telegram присылает его сообщения на {host}. Этот запуск остановлен, чтобы "
    "не перехватить сообщения у облачного бота (данные разошлись бы). Закройте это окно — бот работает и без "
    "компьютера. Если бота действительно нужно вернуть на этот компьютер: сначала остановите облачную копию "
    "(Render → сервис → Settings → Suspend Service), затем впишите в .env строку TAKEOVER_WEBHOOK=1 и "
    "запустите снова (docs/DEPLOY_RENDER.md)."
)
_HTTP_SHUTDOWN_SEC = 5.0    # с, дождаться ответа на HTTP-запросы в работе (сами ответы мгновенные)

_COMMANDS = [
    BotCommand(command="start", description="Начать работу / главное меню"),
    BotCommand(command="menu", description="Показать меню"),
    BotCommand(command="help", description="Как работает бот"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]


class ConfigError(Exception):
    """Ошибка настройки (.env) — текст по-русски для того, кто запускает бота."""


# --- Настройки и логирование -------------------------------------------------------------------


def setup_logging(level_name: str) -> None:
    """Логи в консоль: время, уровень, модуль. Ключи и токены сюда не пишутся."""
    level = logging.getLevelName(level_name.strip().upper())
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT, force=True)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))


def load_settings() -> Settings:
    """Настройки из окружения и .env; при ошибке — ConfigError с понятным текстом."""
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]).upper() for error in exc.errors() if error["loc"]})
        raise ConfigError(
            f"В файле .env неверные значения: {', '.join(fields)}. Сверьтесь с .env.example и README.md."
        ) from None
    token = settings.bot_token.strip()
    if not token:
        raise ConfigError(_NO_TOKEN)
    try:
        validate_token(token)
    except TokenValidationError:
        raise ConfigError(_BAD_TOKEN) from None
    return settings


def check_run_mode(settings: Settings) -> None:
    """Проверить настройки выбранного режима работы; ошибка — ConfigError с понятным текстом."""
    if settings.run_mode != "webhook":
        return
    if not settings.base_url:
        raise ConfigError(_NO_PUBLIC_URL)
    if not settings.base_url.lower().startswith("https://"):
        raise ConfigError(_NOT_HTTPS.format(url=settings.base_url))
    if settings.webhook_secret and not _WEBHOOK_SECRET_RE.fullmatch(settings.webhook_secret):
        raise ConfigError(_BAD_WEBHOOK_SECRET)
    if not 0 < settings.port < 65536:
        raise ConfigError(_BAD_PORT.format(port=settings.port))


def check_database(settings: Settings) -> None:
    """Проверить DATABASE_URL / DATABASE_PASSWORD до подключения: заглушки «ВСТАВЬТЕ_СЮДА_…» и неразборчивый
    адрес — ConfigError с понятным текстом (вместо ошибки разбора адреса или «password authentication failed»).
    Значения в текст ошибки не попадают."""
    url = settings.database_url.strip()
    if _PLACEHOLDER_MARK in url.upper():
        raise ConfigError(_DB_URL_PLACEHOLDER)
    if not url.strip("\"'"):
        raise ConfigError(_DB_URL_EMPTY)
    try:
        parsed = normalize_url(url)
        backend = parsed.get_backend_name()
    except Exception:  # noqa: BLE001 — любой неразборчивый адрес: текст исключения может содержать пароль
        raise ConfigError(_DB_URL_BAD) from None
    if backend not in ("sqlite", "postgresql"):
        raise ConfigError(_DB_URL_BAD)
    if backend != "postgresql":
        return
    # DATABASE_PASSWORD нужен, только если в адресе нет настоящего пароля (apply_password): пароль,
    # вписанный в DATABASE_URL, важнее — тогда заглушка в DATABASE_PASSWORD (её пишет
    # deploy/make_render_env.py, «строку можно не заполнять») ничему не мешает.
    if is_password_placeholder(parsed.password):
        password = settings.database_password
        if _PLACEHOLDER_MARK in password.upper():
            raise ConfigError(_DB_PASSWORD_PLACEHOLDER)
        if parsed.password and not password.strip():
            raise ConfigError(_DB_PASSWORD_MISSING)
    host = (parsed.host or "").lower()
    if settings.run_mode == "webhook" and host.startswith("db.") and host.endswith(".supabase.co"):
        # Direct connection Supabase — только IPv6 (без платного дополнения), а Render выходит в сеть по IPv4.
        raise ConfigError(_DB_DIRECT_CONNECTION)


def _db_startup_problem(exc: BaseException) -> str | None:
    """Понятная причина, по которой не удалось подключиться к базе при запуске; None — ошибка не из этих.

    Только классы ошибок и коды SQLSTATE — значения из настроек (адрес, пароль) в текст не попадают.
    """
    if isinstance(exc, DBAPIError):
        orig = exc.orig
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        if sqlstate in _DB_AUTH_SQLSTATES:
            return _DB_AUTH_FAILED
        if sqlstate == _DB_NO_DATABASE_SQLSTATE:
            return _DB_NO_DATABASE
        if "tenant or user not found" in str(orig).lower():  # пулер Supabase (Supavisor)
            return _DB_TENANT_NOT_FOUND
        return None
    if isinstance(exc, socket.gaierror):
        return _DB_UNREACHABLE.format(reason="адрес сервера не найден")
    if isinstance(exc, TimeoutError):
        return _DB_UNREACHABLE.format(reason="сервер не ответил вовремя")
    if isinstance(exc, ConnectionRefusedError):
        return _DB_UNREACHABLE.format(reason="сервер отказал в подключении")
    if isinstance(exc, OSError):
        return _DB_UNREACHABLE.format(reason=f"сетевая ошибка {type(exc).__name__}")
    return None


async def _prepare_database(engine: AsyncEngine) -> None:
    """init_db; не удалось подключиться (пароль, адрес, сеть) — ConfigError с понятной причиной по-русски
    вместо трейсбека на сотню строк. Прочие ошибки — как есть."""
    try:
        await init_db(engine)
    except Exception as exc:
        problem = _db_startup_problem(exc) if engine.dialect.name == "postgresql" else None
        if problem is None:
            raise
        raise ConfigError(problem) from None


def _log_startup_hints(settings: Settings) -> None:
    """Подсказки о настройках, без которых бот работает, но не полностью."""
    if not settings.admin_ids:
        log.warning(
            "ADMIN_IDS не задан: никто не станет руководителем автоматически и заявки некому подтвердить. "
            "Впишите свой Telegram ID в .env (ADMIN_IDS=...)."
        )
    _log_ai_chain(settings)
    if settings.run_mode == "webhook":
        if settings.database_url.startswith("sqlite"):
            log.warning(
                "Режим webhook, а база — файл SQLite. На Render и похожих хостингах файлы стираются при "
                "каждом перезапуске — задачи и оценки пропадут. Укажите в DATABASE_URL базу PostgreSQL "
                "(например, бесплатный Supabase)."
            )


def _log_ai_chain(settings: Settings) -> None:
    """Какие бесплатные AI-провайдеры будут спрашиваться и в каком порядке (только названия, без ключей)."""
    unknown = [name for name in settings.ai_providers if name not in AI_PROVIDER_NAMES]
    if unknown:
        log.warning(
            "AI_PROVIDERS: неизвестные провайдеры %s пропущены (известны: %s)",
            ", ".join(unknown),
            ", ".join(AI_PROVIDER_NAMES),
        )
    if bool(settings.cloudflare_api_token.strip()) != bool(settings.cloudflare_account_id.strip()):
        log.warning("Cloudflare не используется: нужны оба значения — CLOUDFLARE_API_TOKEN и CLOUDFLARE_ACCOUNT_ID")
    if settings.ai_provider == "none":
        log.info("AI выключен (AI_PROVIDER=none) — оценки и подсказки по правилам")
        return
    active = settings.active_ai_providers
    if not active:
        log.info(
            "AI выключен: не задан ни один ключ (%s) — оценки и подсказки по правилам",
            ", ".join(AI_KEY_ENV.values()),
        )
        return
    chain = " → ".join(f"{name} ({', '.join(settings.ai_models_for(name))})" for name in active)
    log.info("AI: %s → расчёт по правилам", chain)
    unused = [
        name for name in AI_PROVIDER_NAMES
        if name not in active and settings.ai_key_for(name) and name not in settings.ai_providers
    ]
    if unused:
        log.warning("AI: ключ задан, но провайдера нет в AI_PROVIDERS — не используется: %s", ", ".join(unused))
    if len(active) == 1:
        spare = AI_KEY_ENV["groq"] if active[0] != "groq" else AI_KEY_ENV["cloudflare"]
        log.info(
            "AI: запасного провайдера нет — если «%s» станет недоступен, бот перейдёт на расчёт по правилам. "
            "Бесплатный запасной AI без карты: %s (docs/DEPLOY_RENDER.md, «Запасной бесплатный AI»)",
            active[0],
            spare,
        )


# --- Dispatcher и обработчик ошибок ------------------------------------------------------------


def default_storage(sessionmaker: async_sessionmaker[AsyncSession]) -> BaseStorage:
    """Хранилище незавершённых диалогов: в БД (bot.fsm_storage.DbStorage) — переживает перезапуск
    бота; если модуля нет — в памяти. Ошибка внутри существующего модуля не скрывается."""
    if importlib.util.find_spec("bot.fsm_storage") is None:
        return MemoryStorage()
    from bot.fsm_storage import DbStorage

    return DbStorage(sessionmaker)


def build_dispatcher(
    sessionmaker: async_sessionmaker[AsyncSession], storage: BaseStorage | None = None
) -> Dispatcher:
    """Dispatcher со всеми middleware, роутерами и обработчиком ошибок. Сеть не нужна (удобно для тестов).

    storage — хранилище FSM; по умолчанию default_storage(sessionmaker).
    """
    # SimpleEventIsolation: события одного пользователя обрабатываются строго по очереди,
    # поэтому двойное нажатие кнопки не выполнит действие дважды.
    dp = Dispatcher(
        storage=storage if storage is not None else default_storage(sessionmaker),
        events_isolation=SimpleEventIsolation(),
    )
    dp.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dp.update.outer_middleware(UserMiddleware())
    dp.include_routers(
        start.router,
        users_admin.router,
        task_create.router,
        task_propose.router,
        task_submit.router,
        task_review.router,
        task_view.router,
        dashboard.router,
        start.fallback_router,  # «ловушка» для всего необработанного — строго последней
    )
    dp.errors.register(on_error)
    return dp


async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    """DomainError — показать текст пользователю; прочие ошибки — в лог и короткое сообщение."""
    exc = event.exception
    if isinstance(exc, DomainError):
        log.info("Действие отклонено (update %s): %s", event.update.update_id, exc.message)
        text = exc.message
    else:
        log.error("Ошибка при обработке update %s", event.update.update_id, exc_info=exc)
        text = GENERIC_ERROR
    await _tell_user(bot, event.update, text)
    return True


def _alert_text(text: str) -> str:
    return text if len(text) <= _ALERT_LIMIT else text[: _ALERT_LIMIT - 1] + "…"


async def _tell_user(bot: Bot, update: Update, text: str) -> None:
    """Нажатие кнопки — alert (если на callback ещё не ответили), иначе — сообщение в чат."""
    callback = update.callback_query
    if callback is not None:
        try:
            await bot.answer_callback_query(callback.id, text=_alert_text(text), show_alert=True)
            return
        except TelegramAPIError as exc:  # на callback уже ответили или запрос устарел
            log.debug("Alert не показан (%s) — отправляем сообщением", type(exc).__name__)
        chat_id = callback.message.chat.id if callback.message is not None else callback.from_user.id
    elif update.message is not None:
        chat_id = update.message.chat.id
    else:
        return
    await notify.safe_send(bot, chat_id, esc(text))


# --- Запуск --------------------------------------------------------------------------------------


async def _connect(bot: Bot) -> TgUser:
    """Проверить токен и связь с Telegram. Нет интернета — ждём и пробуем снова (ПК мог только включиться)."""
    delay = _CONNECT_RETRY_START
    while True:
        try:
            return await bot.get_me()
        except TelegramUnauthorizedError:
            raise ConfigError(_TOKEN_REJECTED) from None
        except (TelegramNetworkError, TelegramServerError) as exc:
            log.warning("Нет связи с Telegram (%s). Повтор через %s с…", type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _CONNECT_RETRY_MAX)


async def _set_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(_COMMANDS)
    except TelegramAPIError as exc:
        log.warning("Не удалось обновить список команд бота: %s", type(exc).__name__)


def _webhook_host(url: str) -> str:
    """Только имя сервера из адреса webhook: в пути — секрет, его не показываем."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        host = None
    return host or "другой адрес"


async def _drop_webhook(bot: Bot, *, takeover: bool = False) -> None:
    """Polling: проверить, не работает ли бот уже в режиме webhook (Render и т. п.).

    Webhook включён — значит, сообщения бота получает облачная копия. Молча снять его нельзя:
    случайный запуск run.bat на компьютере увёл бы бота из облака на старую локальную базу.
    Поэтому — ConfigError с объяснением. Снять webhook и перейти на polling можно только явно:
    TAKEOVER_WEBHOOK=1 (takeover=True). Накопившиеся сообщения при этом не теряются.
    """
    try:
        info = await bot.get_webhook_info()
    except TelegramAPIError as exc:
        log.warning("Не удалось проверить webhook: %s", type(exc).__name__)
        return
    if not info.url:
        return
    host = _webhook_host(info.url)
    if not takeover:
        raise ConfigError(_WEBHOOK_ACTIVE.format(host=host))
    log.warning(
        "TAKEOVER_WEBHOOK=1: у бота был включён webhook (%s) — отключаю его и перехожу на polling. "
        "Облачная копия бота должна быть остановлена: работающая копия вернёт webhook себе в течение 5 минут.",
        host,
    )
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except TelegramAPIError as exc:
        log.warning("Не удалось отключить webhook: %s", type(exc).__name__)


async def _setup_webhook(bot: Bot, dp: Dispatcher, settings: Settings) -> None:
    """Webhook у Telegram (ensure_webhook): нет связи — ждём и повторяем; адрес отвергнут — ConfigError."""
    delay = _CONNECT_RETRY_START
    while True:
        try:
            await ensure_webhook(bot, dp, settings)
            return
        except TelegramRetryAfter as exc:
            log.warning("Флуд-лимит Telegram при настройке webhook — повтор через %s с", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
        except TelegramBadRequest as exc:
            reason = exc.message.replace(settings.webhook_secret_value, "…")
            raise ConfigError(_WEBHOOK_REJECTED.format(url=settings.base_url, reason=reason)) from None
        except (TelegramNetworkError, TelegramServerError) as exc:
            log.warning("Не удалось настроить webhook (%s). Повтор через %s с…", type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _CONNECT_RETRY_MAX)


async def _wait_for_stop() -> None:
    """Ждать SIGTERM (остановка на хостинге) или SIGINT (Ctrl+C).

    На Windows обработчики сигналов asyncio недоступны — там Ctrl+C прерывает asyncio.run сам
    (задача main отменяется, и все finally выполняются).
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
    try:
        await stop.wait()
        log.info("Получен сигнал остановки — завершаю работу")
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


async def _run_polling(
    bot: Bot,
    dp: Dispatcher,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    takeover_webhook: bool = False,
) -> None:
    log.info("Режим работы: polling — бот сам забирает сообщения у Telegram, задания по времени — внутри бота")
    me = await _connect(bot)
    log.info("Бот @%s подключён к Telegram", me.username)
    await _drop_webhook(bot, takeover=takeover_webhook)
    await _set_commands(bot)
    scheduler = setup_scheduler(bot, sessionmaker)
    scheduler.start()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


async def _run_webhook(
    settings: Settings, bot: Bot, dp: Dispatcher, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Веб-сервер на 0.0.0.0:PORT, webhook у Telegram, фоновый цикл (самопробуждение и задания по
    времени), работа до SIGTERM. При остановке цикл останавливается первым; webhook не удаляется."""
    log.info(
        "Режим работы: webhook — Telegram присылает сообщения на %s/tg/…, задания по времени бот "
        "запускает сам",
        settings.base_url,
    )
    app = build_web_app(bot, dp, sessionmaker, settings)
    background = app[BACKGROUND]
    # access_log=None: в журнале запросов оказались бы секретный путь webhook и ключ /tick.
    runner = web.AppRunner(app, access_log=None, handle_signals=False, shutdown_timeout=_HTTP_SHUTDOWN_SEC)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host="0.0.0.0", port=settings.port)  # noqa: S104 - хостингу нужен внешний адрес
        try:
            await site.start()
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise ConfigError(_PORT_BUSY.format(port=settings.port, reason=reason)) from None
        log.info("Веб-сервер слушает 0.0.0.0:%s", settings.port)
        me = await _connect(bot)
        log.info("Бот @%s подключён к Telegram", me.username)
        await _setup_webhook(bot, dp, settings)
        await _set_commands(bot)
        # Цикл — когда бот уже на связи с Telegram: задания сразу могут отправлять сообщения.
        background.start()
        log.info("%s", background.describe())
        log.info("Задания по времени: %s", tick_schedule_summary(background.job_interval))
        log.info(
            "Внешний будильник не нужен. Необязательный резерв: GET %s/tick?key=<TICK_SECRET>",
            settings.base_url,
        )
        await _wait_for_stop()
    finally:
        await background.stop()
        await runner.cleanup()


async def main(settings: Settings | None = None) -> None:
    """Запустить бота: БД, затем polling или webhook (RUN_MODE). Корректно останавливается по Ctrl+C / SIGTERM."""
    settings = settings or load_settings()
    check_run_mode(settings)
    check_database(settings)
    _log_startup_hints(settings)
    engine = make_engine(settings.database_url)
    # PostgreSQL: у хранилища диалогов свой пул из одного соединения — апдейты, занявшие все соединения
    # основного пула, не ждут друг друга при записи состояния (make_storage_engine). SQLite — None.
    storage_engine = make_storage_engine(settings.database_url)
    bot = Bot(settings.bot_token.strip(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await _prepare_database(engine)
        sessionmaker = make_sessionmaker(engine)
        storage = default_storage(make_sessionmaker(storage_engine)) if storage_engine is not None else None
        dp = build_dispatcher(sessionmaker, storage)
        if settings.run_mode == "webhook":
            await _run_webhook(settings, bot, dp, sessionmaker)
        else:
            await _run_polling(bot, dp, sessionmaker, takeover_webhook=settings.takeover_webhook)
    finally:
        await close_client()
        await bot.session.close()
        await engine.dispose()
        if storage_engine is not None:
            await storage_engine.dispose()
        log.info("Бот остановлен")


def run() -> None:
    """Синхронная точка входа (python -m bot): понятные сообщения об ошибках настройки, тихий Ctrl+C."""
    setup_logging("INFO")
    try:
        settings = load_settings()
        setup_logging(settings.log_level)
        asyncio.run(main(settings))
    except ConfigError as exc:
        log.critical("%s", exc)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        log.info("Остановлено пользователем (Ctrl+C)")
