"""Подключение к базе данных (SQLAlchemy 2 async): SQLite (aiosqlite) или PostgreSQL (asyncpg).

SQLite — по умолчанию (файл data/bot.db): свой компьютер или VPS с диском.
PostgreSQL — облачный хостинг без постоянного диска (Render + бесплатный Supabase и т. п.).
DATABASE_URL вставляется в том виде, в каком его показывает Supabase / любой хостинг PostgreSQL:

    postgresql://postgres.<ref>:<пароль>@aws-0-eu-central-1.pooler.supabase.com:5432/postgres

``make_engine`` для PostgreSQL сам:

* подставляет пароль из настройки ``DATABASE_PASSWORD``, если в адресе пароля нет или вместо него
  стоит заглушка Supabase ``[YOUR-PASSWORD]``: строку подключения можно вставить ровно так, как её
  показывает Supabase, а пароль — отдельно, без экранирования спецсимволов (``apply_password``).
  Пароль, записанный в самом адресе, важнее. Пароль нигде не пишется в лог;
* меняет схему ``postgres://`` / ``postgresql://`` / ``postgresql+<другой драйвер>://`` на
  ``postgresql+asyncpg://``; пароль с неэкранированным «@» тоже понимается;
* убирает из адреса параметры libpq, которых asyncpg не знает (на ``sslmode=require`` он упал бы
  с TypeError), и переводит их в аргументы подключения asyncpg: ``sslmode`` -> ``ssl``,
  ``sslrootcert``/``sslcert``/``sslkey`` -> SSL-контекст, ``connect_timeout`` -> ``timeout``,
  ``application_name`` и ``options=-c имя=значение`` -> ``server_settings``. Параметры asyncpg
  (``statement_cache_size``, ``command_timeout``, ...) приводятся к нужному типу. Остальные
  параметры libpq (``gssencmode``, ``channel_binding``, ``keepalives``...) отбрасываются
  с предупреждением в лог (только имя параметра — значения и пароль в лог не пишутся);
* включает SSL для любого нелокального сервера, если ``sslmode`` не задан: как ``sslmode=require``
  (шифрование без проверки сертификата — так подключаются к Supabase по умолчанию; проверка
  сертификата — ``sslmode=verify-full&sslrootcert=<файл CA>``). Локальный сервер (localhost,
  127.0.0.1, ::1, частная сеть, имя без точки вроде ``db`` в docker compose, unix-сокет) —
  по умолчанию asyncpg: «prefer» (SSL, если сервер его поддерживает);
* берёт небольшой пул: ``pool_size=3, max_overflow=1`` — бесплатный пулер Supabase ограничивает
  число подключений, а при обновлении на Render ~1–2 минуты работают два экземпляра бота.
  Ещё одно соединение — отдельный пул хранилища диалогов (``make_storage_engine``): итого 5 на
  экземпляр, 2 × 5 = 10 при обновлении; ``pool_pre_ping=True`` и ``pool_recycle=300`` — пулер
  и сеть рвут простаивающие соединения, а бот на бесплатном Render засыпает: перед выдачей
  соединение проверяется, а старше 5 минут — пересоздаётся;
* порт 6543 — transaction pooler Supabase (Supavisor / PgBouncer в режиме transaction):
  серверное соединение меняется после каждой транзакции, подготовленные выражения на нём
  не живут. Поэтому кэши выражений asyncpg и SQLAlchemy выключаются
  (``statement_cache_size=0``, ``prepared_statement_cache_size=0``), а имена выражений
  делаются уникальными (uuid), чтобы не столкнуться с чужими на общем соединении. То же
  включает параметр ``?pgbouncer=true`` (для пулера на другом порту). Рекомендуемый режим —
  session pooler (порт 5432, IPv4): там всё это не нужно и работает быстрее.

Явные ``**engine_kwargs`` у ``make_engine`` перекрывают значения по умолчанию (тесты так ставят
``poolclass=StaticPool``); ``connect_args`` объединяются.

``init_db`` создаёт недостающие таблицы (существующие и данные не трогает). На PostgreSQL —
под транзакционной advisory-блокировкой (два экземпляра бота, стартующие одновременно, не
столкнутся на CREATE TABLE) и с включённой защитой строк (RLS) на таблицах бота: Supabase
открывает схему public через свой REST API (Data API), а таблица без RLS доступна любому,
у кого есть публичный anon-ключ проекта. Бот подключается владельцем таблиц, на него RLS
не действует.
"""

from __future__ import annotations

import ipaddress
import logging
import shlex
import ssl
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import QueuePool, StaticPool

__all__ = [
    "Base",
    "apply_password",
    "describe_url",
    "init_db",
    "is_password_placeholder",
    "is_postgres_url",
    "make_engine",
    "make_sessionmaker",
    "make_storage_engine",
    "normalize_url",
    "postgres_connect_args",
]

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# --- PostgreSQL: адрес и параметры подключения ----------------------------------------------------

PG_POOL_SIZE = 3
PG_MAX_OVERFLOW = 1
PG_STORAGE_POOL_SIZE = 1                # отдельный пул хранилища диалогов (make_storage_engine)
PG_POOL_RECYCLE_SEC = 300
PG_CONNECT_TIMEOUT_SEC = 15.0           # asyncpg по умолчанию ждёт 60 с — апдейт или задание столько не висят
TRANSACTION_POOLER_PORT = 6543          # Supabase: transaction pooler (Supavisor)
_INIT_LOCK_KEY = 0x6B70695F696E6974     # «kpi_init»: advisory-блокировка init_db

_PG_BACKENDS = frozenset({"postgres", "postgresql"})
_SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
_TRUE = frozenset({"1", "true", "yes", "on"})

# Параметры asyncpg.connect, которые можно передать в адресе, и их типы.
_ASYNCPG_PARAMS: dict[str, type] = {
    "timeout": float,
    "command_timeout": float,
    "statement_cache_size": int,
    "max_cached_statement_lifetime": float,
    "max_cacheable_statement_size": int,
    "prepared_statement_cache_size": int,  # параметр SQLAlchemy (кэш подготовленных выражений)
    "target_session_attrs": str,
    "krbsrvname": str,
    "gsslib": str,
    "passfile": str,
    "service": str,
    "servicefile": str,
}
# Остаются в адресе: их разбирает сам SQLAlchemy (несколько хостов, unix-сокет).
_URL_PARAMS = frozenset({"host", "port"})
_SSL_FILE_PARAMS = ("sslrootcert", "sslcert", "sslkey", "sslpassword")


def is_postgres_url(url: str | URL) -> bool:
    """Адрес указывает на PostgreSQL (любая схема postgres:// / postgresql[+драйвер]://)."""
    try:
        return normalize_url(url).get_backend_name() == "postgresql"
    except Exception:  # noqa: BLE001 — неразборчивый адрес: точно не PostgreSQL
        return False


def normalize_url(url: str | URL) -> URL:
    """Адрес базы в виде, понятном SQLAlchemy: асинхронный драйвер (aiosqlite / asyncpg).

    Параметры запроса не трогаются (их разбирает ``postgres_connect_args``).
    """
    if isinstance(url, str):
        url = make_url(_fix_unescaped_password(url.strip().strip("\"'")))
    backend = url.get_backend_name()
    if backend in _PG_BACKENDS:
        return url.set(drivername="postgresql+asyncpg")
    if backend == "sqlite" and url.get_driver_name() in ("", "pysqlite"):
        return url.set(drivername="sqlite+aiosqlite")
    return url


def _fix_unescaped_password(raw: str) -> str:
    """Пароль с «@», вставленный в адрес как есть (без %40), — экранировать.

    Иначе SQLAlchemy молча возьмёт часть пароля за имя сервера («нет такого хоста»).
    Пароль — всё между первым «:» после «://» и последним «@».
    """
    scheme, sep, rest = raw.partition("://")
    if not sep or rest.count("@") < 2:
        return raw
    userinfo, host_part = rest.rsplit("@", 1)
    user, colon, password = userinfo.partition(":")
    if not colon:
        return raw
    return f"{scheme}://{user}:{quote(password, safe='')}@{host_part}"


def describe_url(url: str | URL) -> str:
    """Адрес базы для лога — без пароля и параметров: «postgresql+asyncpg://user@host:5432/db»."""
    try:
        parsed = normalize_url(url)
    except Exception:  # noqa: BLE001
        return "<неверный DATABASE_URL>"
    return parsed.set(query={}).render_as_string(hide_password=True)


# --- Пароль отдельно от адреса (DATABASE_PASSWORD) ------------------------------------------------

# Заглушки вместо пароля в строке подключения: Supabase показывает «[YOUR-PASSWORD]».
_PASSWORD_PLACEHOLDERS = frozenset({"your-password", "yourpassword"})
_PLACEHOLDER_WARNING = (
    "DATABASE_URL: вместо пароля стоит заглушка [YOUR-PASSWORD], а DATABASE_PASSWORD не задан — "
    "подключиться к базе не получится. Впишите пароль базы в настройку DATABASE_PASSWORD."
)


def is_password_placeholder(password: str | None) -> bool:
    """В адресе нет настоящего пароля: пусто или заглушка «[YOUR-PASSWORD]» (скобки и регистр — любые)."""
    if not password:
        return True
    core = password.strip().strip("[]<>{}\"'").strip().lower().replace("_", "-").replace(" ", "-")
    return core in _PASSWORD_PLACEHOLDERS


def apply_password(url: str | URL, password: str | None) -> URL:
    """``normalize_url`` + пароль ``password`` (DATABASE_PASSWORD) для адреса PostgreSQL без пароля.

    Пароль подставляется, если в адресе его нет или вместо него заглушка ``[YOUR-PASSWORD]``; пароль
    в самом адресе важнее. URL хранит пароль как есть — экранировать «@ : / # ? %» не нужно
    (SQLAlchemy экранирует при выводе строки, asyncpg получает пароль без изменений). Перевод строки
    в конце (вставка из Блокнота) отбрасывается. SQLite и прочие адреса — без изменений.
    """
    parsed = normalize_url(url)
    if parsed.get_backend_name() != "postgresql" or not is_password_placeholder(parsed.password):
        return parsed
    secret = (password or "").strip("\r\n")
    if secret:
        return parsed.set(password=secret)
    if parsed.password:  # заглушка, а пароля нет — понятное предупреждение вместо «auth failed»
        log.warning(_PLACEHOLDER_WARNING)
    return parsed


def _configured_password() -> str:
    """DATABASE_PASSWORD из настроек бота; настройки не читаются — пусто (адрес как есть)."""
    try:
        from bot.config import get_settings

        return get_settings().database_password
    except Exception:  # noqa: BLE001 — ошибку настроек покажет запуск бота, а не подключение к базе
        return ""


def _is_local_host(host: str | None) -> bool:
    """Сервер в той же машине / частной сети: SSL по умолчанию не требуем (asyncpg «prefer»)."""
    if not host:
        return True  # unix-сокет по умолчанию
    host = host.strip("[]").lower()
    if host.startswith("/") or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host  # «db», «postgres» — имя сервиса в docker compose / локальной сети
    return address.is_loopback or address.is_private or address.is_link_local


def _hosts(url: URL) -> list[str | None]:
    hosts = url.query.get("host")
    if hosts is None:
        return [url.host]
    values = hosts if isinstance(hosts, tuple) else (hosts,)
    return [h for value in values for h in value.split(",")] or [url.host]


def _query_value(value: str | tuple[str, ...]) -> str:
    """Значение параметра адреса (повторённый параметр — берётся последнее значение, как в libpq)."""
    return value[-1] if isinstance(value, tuple) else value


def _coerce(name: str, value: str, kind: type) -> Any:
    try:
        return kind(value)
    except ValueError:
        raise ValueError(f"DATABASE_URL: параметр {name} должен быть числом") from None


def _parse_options(options: str) -> dict[str, str]:
    """libpq ``options="-c a=b -c c=d"`` / ``--a=b`` -> server_settings asyncpg."""
    settings: dict[str, str] = {}
    try:
        tokens = shlex.split(options)
    except ValueError:
        tokens = options.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        pair: str | None = None
        if token == "-c" and index + 1 < len(tokens):
            pair = tokens[index + 1]
            index += 1
        elif token.startswith("-c") and len(token) > 2:
            pair = token[2:]
        elif token.startswith("--"):
            pair = token[2:]
        index += 1
        if pair and "=" in pair:
            key, value = pair.split("=", 1)
            settings[key.strip().replace("-", "_")] = value
        else:
            log.warning("DATABASE_URL: часть параметра options не распознана и пропущена")
    return settings


def _ssl_context(mode: str, params: dict[str, str]) -> ssl.SSLContext:
    """SSL-контекст из sslrootcert / sslcert / sslkey (как в libpq)."""
    rootcert = params.get("sslrootcert")
    if rootcert:
        if rootcert == "system":  # libpq 16+: системные корневые сертификаты
            context = ssl.create_default_context()
        else:
            context = ssl.create_default_context(cafile=rootcert)
        # С указанным корневым сертификатом «require» проверяет цепочку, как verify-ca (так делает libpq).
        context.check_hostname = mode == "verify-full"
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if params.get("sslcert"):
        password = params.get("sslpassword")
        context.load_cert_chain(params["sslcert"], keyfile=params.get("sslkey") or None, password=password or None)
    return context


def postgres_connect_args(url: str | URL) -> tuple[URL, dict[str, Any]]:
    """(адрес без параметров libpq, connect_args для asyncpg) — см. описание модуля."""
    parsed = normalize_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("postgres_connect_args: адрес не PostgreSQL")
    query = {key: _query_value(value) for key, value in parsed.query.items()}
    kept = {key: value for key, value in parsed.query.items() if key in _URL_PARAMS}
    connect_args: dict[str, Any] = {}
    server_settings: dict[str, str] = {}
    dropped: list[str] = []
    ssl_files: dict[str, str] = {}
    sslmode: str | None = None
    transaction_pooler = parsed.port == TRANSACTION_POOLER_PORT

    for key, value in query.items():
        if key in _URL_PARAMS:
            continue
        if key in _ASYNCPG_PARAMS:
            kind = _ASYNCPG_PARAMS[key]
            connect_args[key] = _coerce(key, value, kind) if kind is not str else value
        elif key == "sslmode":
            sslmode = value.strip().lower()
            if sslmode not in _SSL_MODES:
                raise ValueError(f"DATABASE_URL: sslmode должен быть одним из: {', '.join(_SSL_MODES)}")
        elif key == "ssl":  # asyncpg-стиль: ?ssl=require / ?ssl=true
            lowered = value.strip().lower()
            sslmode = "require" if lowered in _TRUE else "disable" if lowered in ("0", "false", "no", "off") else lowered
            if sslmode not in _SSL_MODES:
                raise ValueError(f"DATABASE_URL: ssl должен быть одним из: {', '.join(_SSL_MODES)}")
        elif key in _SSL_FILE_PARAMS:
            ssl_files[key] = value
        elif key == "sslnegotiation":
            if value.strip().lower() == "direct":
                connect_args["direct_tls"] = True
        elif key == "direct_tls":
            connect_args["direct_tls"] = value.strip().lower() in _TRUE
        elif key == "connect_timeout":
            seconds = _coerce(key, value, float)
            if seconds > 0:  # 0 в libpq — «ждать бесконечно»: оставляем значение по умолчанию
                connect_args["timeout"] = seconds
        elif key == "application_name":
            server_settings["application_name"] = value
        elif key == "options":
            server_settings.update(_parse_options(value))
        elif key == "pgbouncer":
            transaction_pooler = transaction_pooler or value.strip().lower() in _TRUE
        else:
            dropped.append(key)
    if dropped:
        log.warning("DATABASE_URL: параметры %s не поддерживаются asyncpg и пропущены", ", ".join(sorted(dropped)))

    if sslmode is None and not all(_is_local_host(host) for host in _hosts(parsed)):
        sslmode = "require"
    if sslmode is not None:
        use_context = sslmode not in ("disable", "allow", "prefer") and (
            ssl_files.get("sslrootcert") or ssl_files.get("sslcert")
        )
        connect_args["ssl"] = _ssl_context(sslmode, ssl_files) if use_context else sslmode

    connect_args.setdefault("timeout", PG_CONNECT_TIMEOUT_SEC)
    if server_settings:
        connect_args["server_settings"] = server_settings
    if transaction_pooler:
        connect_args.setdefault("statement_cache_size", 0)
        connect_args.setdefault("prepared_statement_cache_size", 0)
        connect_args.setdefault("prepared_statement_name_func", _unique_statement_name)
    return parsed.set(query=kept), connect_args


def _unique_statement_name() -> str:
    """Имя подготовленного выражения, которое не встретится у другого клиента пулера."""
    return f"__asyncpg_{uuid.uuid4().hex}__"


# --- Движок и сессии ------------------------------------------------------------------------------

# Пулы без размера: pool_size / max_overflow / pool_timeout им передавать нельзя.
_SIZED_POOL_ARGS = ("pool_size", "max_overflow", "pool_timeout")


def make_engine(url: str | URL, *, password: str | None = None, **engine_kwargs: Any) -> AsyncEngine:
    """Асинхронный движок для SQLite или PostgreSQL (см. описание модуля).

    ``password`` — пароль PostgreSQL для адреса без пароля (или с заглушкой ``[YOUR-PASSWORD]``);
    None — из настройки DATABASE_PASSWORD.
    """
    parsed = apply_password(url, _configured_password() if password is None else password)
    kwargs: dict[str, Any] = {}
    backend = parsed.get_backend_name()

    in_memory = False
    if backend == "sqlite":
        database = parsed.database or ""
        in_memory = not database or ":memory:" in database or parsed.query.get("mode") == "memory"
        if in_memory:
            # Одна общая in-memory база для всех соединений (нужно для тестов).
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        elif not database.startswith("file:"):
            Path(database).parent.mkdir(parents=True, exist_ok=True)
    elif backend == "postgresql":
        parsed, connect_args = postgres_connect_args(parsed)
        kwargs.update(
            pool_pre_ping=True,
            pool_size=PG_POOL_SIZE,
            max_overflow=PG_MAX_OVERFLOW,
            pool_recycle=PG_POOL_RECYCLE_SEC,
            connect_args=connect_args,
        )

    extra_connect_args = engine_kwargs.pop("connect_args", None)
    kwargs.update(engine_kwargs)
    if extra_connect_args:
        merged = {**kwargs.get("connect_args", {}), **extra_connect_args}
        if isinstance(extra_connect_args.get("server_settings"), dict):
            base_settings = kwargs.get("connect_args", {}).get("server_settings") or {}
            merged["server_settings"] = {**base_settings, **extra_connect_args["server_settings"]}
        kwargs["connect_args"] = merged
    poolclass = kwargs.get("poolclass")
    if poolclass is not None and not issubclass(poolclass, QueuePool):
        for name in _SIZED_POOL_ARGS:
            kwargs.pop(name, None)

    engine = create_async_engine(parsed, **kwargs)

    if backend == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - инфраструктура
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            if not in_memory:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def make_storage_engine(url: str | URL, *, password: str | None = None, **engine_kwargs: Any) -> AsyncEngine | None:
    """Отдельный движок хранилища диалогов (``bot.fsm_storage.DbStorage``) для PostgreSQL; иначе None.

    На PostgreSQL хранилище читает и пишет состояние диалога отдельной короткой транзакцией, пока
    сессия хендлера (``DbSessionMiddleware``) держит своё соединение. Из общего пула пять апдейтов
    разных людей заняли бы все соединения и ждали бы шестое для записи состояния — бот «вставал» бы
    на ``pool_timeout`` (30 с). Свой пул из одного соединения (``PG_STORAGE_POOL_SIZE``) этого не
    допускает: его операции занимают соединение на миллисекунды и сами ничего не ждут.
    SQLite (запись вдогонку, у базы в памяти — память) — None: хранилищу хватает основного движка.
    """
    if not is_postgres_url(url):
        return None
    kwargs: dict[str, Any] = {"pool_size": PG_STORAGE_POOL_SIZE, "max_overflow": 0, **engine_kwargs}
    return make_engine(url, password=password, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Создать недостающие таблицы (существующие и данные не трогаются)."""
    from bot.db import models  # noqa: F401 - регистрирует таблицы в metadata

    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # Два экземпляра бота (обновление на Render) могут стартовать одновременно:
            # второй дождётся, пока первый создаст таблицы, и увидит их готовыми.
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _INIT_LOCK_KEY})
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name == "postgresql":
            await _enable_row_level_security(conn)


async def _enable_row_level_security(conn: Any) -> None:
    """Включить RLS на таблицах бота, где он выключен и текущий пользователь — их владелец.

    Политик нет — значит, через REST API Supabase (роли anon/authenticated) таблицы недоступны.
    Владелец таблиц (бот) RLS не подчиняется, поэтому на работу бота это не влияет.
    """
    names = list(Base.metadata.tables)
    rows = await conn.execute(
        text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relkind = 'r' AND c.relname = ANY(:names) "
            "AND NOT c.relrowsecurity AND pg_has_role(c.relowner, 'USAGE')"
        ),
        {"names": names},
    )
    quote_name = conn.dialect.identifier_preparer.quote
    for (name,) in rows.all():
        await conn.execute(text(f"ALTER TABLE {quote_name(name)} ENABLE ROW LEVEL SECURITY"))
