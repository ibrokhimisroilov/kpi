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
  экземпляр, 2 × 5 = 10 при обновлении;
* бережёт обмены с базой (см. «Обмены с базой» ниже): проверяет соединение только после простоя,
  пересоздаёт его раз в 30 минут, а не каждые 5, и не открывает транзакцию для одного чтения;
* порт 6543 — transaction pooler Supabase (Supavisor / PgBouncer в режиме transaction):
  серверное соединение меняется после каждой транзакции, подготовленные выражения на нём
  не живут. Поэтому кэши выражений asyncpg и SQLAlchemy выключаются
  (``statement_cache_size=0``, ``prepared_statement_cache_size=0``), а имена выражений
  делаются уникальными (uuid), чтобы не столкнуться с чужими на общем соединении. То же
  включает параметр ``?pgbouncer=true`` (для пулера на другом порту). Рекомендуемый режим —
  session pooler (порт 5432, IPv4): там всё это не нужно и работает быстрее.

Явные ``**engine_kwargs`` у ``make_engine`` перекрывают значения по умолчанию (тесты так ставят
``poolclass=StaticPool``); ``connect_args`` объединяются.

Обмены с базой (PostgreSQL). Бот на Render ходит в Supabase в другом регионе: один обмен (round
trip) — 130–190 мс, новое соединение (TCP + TLS + SCRAM) — 1–1,4 с. Скорость ответа бота определяется
числом обменов, поэтому движок PostgreSQL:

* проверяет соединение перед выдачей из пула, только если оно простояло дольше
  ``PG_PING_IDLE_SEC`` (60 с), — одним обменом (простой запрос ``SELECT 1`` с таймаутом
  ``PG_PING_TIMEOUT_SEC``). Не ответило — соединение обрывается сразу (без «вежливого» закрытия: при
  молча пропавшей связи оно ждало бы ответа минутами) и заменяется новым (``DisconnectionError``);
  апдейт ошибки не видит — только ждёт таймаут проверки и новое соединение. Стандартный
  ``pool_pre_ping`` проверял каждую выдачу тремя обменами (BEGIN; «;»; ROLLBACK), а выдач у одного
  шага диалога — до десятка;
* пересоздаёт соединения раз в ``PG_POOL_RECYCLE_SEC`` (30 мин), а не каждые 5 мин: у нового
  соединения и пустой кэш подготовленных выражений (+1 обмен на каждый новый запрос);
  ``pool_use_lifo`` — снова выдаётся последнее вернувшееся (уже проверенное, «тёплое») соединение;
* откладывает BEGIN до первой записи (``_tune_postgres_engine``): соединение выдаётся в режиме autocommit,
  и чтения идут без транзакции, а перед первым выражением, которому транзакция нужна (INSERT,
  UPDATE, DELETE, SELECT … FOR UPDATE, текстовый SQL, DDL…), открывается обычная транзакция.
  Сессия, которая только читала, обходится без BEGIN и COMMIT/ROLLBACK — минус 2 обмена; с записью
  всё как раньше: BEGIN, запись, COMMIT. На уровне изоляции READ COMMITTED (по умолчанию в
  PostgreSQL) каждое выражение и в транзакции видит свой свежий снимок данных, поэтому чтения до
  первой записи видят то же, что видели бы в транзакции, а блокирующие чтения (FOR UPDATE/SHARE)
  и функции с побочным эффектом (advisory-блокировки и т. п.) сами открывают транзакцию.
  На transaction pooler (порт 6543) не включается: там вне транзакции подготовка и выполнение
  выражения могут уйти на разные серверные соединения.

SQLite: файл на том же компьютере, обмены бесплатные; sqlite3 и так не открывает транзакцию для
SELECT. Там ничего из этого не нужно.

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
import re
import shlex
import ssl
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import event, inspect, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import QueuePool, StaticPool
from sqlalchemy.schema import CreateColumn

try:  # SQLAlchemy 2.1
    from sqlalchemy.util.concurrency import await_, in_greenlet
except ImportError:  # pragma: no cover - SQLAlchemy 2.0
    from sqlalchemy.util import await_only as await_  # type: ignore[no-redef]
    from sqlalchemy.util.concurrency import in_greenlet  # type: ignore[no-redef]

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
    "warm_up",
]

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# --- PostgreSQL: адрес и параметры подключения ----------------------------------------------------

PG_POOL_SIZE = 3
PG_MAX_OVERFLOW = 1
PG_STORAGE_POOL_SIZE = 1                # отдельный пул хранилища диалогов (make_storage_engine)
PG_POOL_RECYCLE_SEC = 1800              # пересоздавать соединение раз в 30 мин (новое — 1–1,4 с до Supabase)
PG_PING_IDLE_SEC = 60.0                 # проверять соединение перед выдачей, если оно простояло дольше
PG_PING_TIMEOUT_SEC = 5.0               # проверка SELECT 1 не ответила за это время — соединение заменяется
_PING_SQL = "SELECT 1"  # простой протокол, без подготовки выражения; пустой «;» asyncpg.execute не принимает
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


def _is_transaction_pooler(url: URL, connect_args: dict[str, Any]) -> bool:
    """Подключение через transaction pooler (порт 6543 Supabase, ``?pgbouncer=true``) или без кэша
    выражений asyncpg: серверное соединение может смениться между транзакциями."""
    if url.port == TRANSACTION_POOLER_PORT or connect_args.get("statement_cache_size") == 0:
        return True
    flag = url.query.get("pgbouncer")
    return flag is not None and _query_value(flag).strip().lower() in _TRUE


# --- PostgreSQL: меньше обменов с базой (см. «Обмены с базой» в описании модуля) --------------------

_LAST_USED = "kpi_last_used"  # connection_record.info: когда соединение последний раз вернули в пул (monotonic)

# Выражение, которому нужна транзакция, хотя это SELECT: блокировка строк или функция с побочным эффектом.
_NEEDS_TRANSACTION_RE = re.compile(
    r"\bFOR\s+(?:NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)\b"
    r"|\b(?:pg_try_advisory|pg_advisory|nextval|setval|set_config|pg_notify|txid_current|pg_current_xact_id)\w*\s*\(",
    re.IGNORECASE,
)


def _is_plain_read(statement: str, context: Any) -> bool:
    """Обычное чтение, которому транзакция не нужна: собранный SQLAlchemy SELECT без FOR UPDATE/SHARE и
    без функций с побочным эффектом. Всё остальное (DML, DDL, текстовый SQL, SAVEPOINT, CTE…) — нет."""
    if getattr(context, "compiled", None) is None or context.is_text:
        return False
    if context.isinsert or context.isupdate or context.isdelete or context.isddl:
        return False
    if statement.lstrip()[:6].upper() != "SELECT":
        return False
    return _NEEDS_TRANSACTION_RE.search(statement) is None


def _driver_connection_of(conn: Any) -> Any:
    """DBAPI-адаптер asyncpg соединения SQLAlchemy (None — соединение закрыто или сброшено)."""
    if conn.closed or conn.invalidated:
        return None
    return conn.connection.dbapi_connection


def _ping(record: Any) -> None:
    """Проверить простоявшее соединение одним обменом: простой запрос ``SELECT 1`` (asyncpg.execute без
    параметров — простой протокол, без подготовки выражения; годится и для transaction pooler). Не ответило —
    DisconnectionError: пул закроет его и выдаст новое. Пустой запрос «;» не годится: на нём asyncpg.execute
    падает (у ответа нет статуса), и каждое простоявшее соединение заменялось бы новым (1–1,4 с).

    Не ответившее соединение сначала обрывается (``terminate``), и только потом — DisconnectionError. Иначе
    пул закрывал бы его «вежливо»: asyncpg перед закрытием ждёт ответа на отмену запроса, не ответившего по
    таймауту, — без ограничения по времени и по тому же соединению. Если связь пропала молча (NAT или
    балансировщик забыл соединение, сеть разорвана — без RST), ответа нет, и выдача соединения висела бы,
    пока ОС не признает связь мёртвой (на Linux ~15 мин), держа апдейт, блокировку пользователя и место
    в пуле. Оборванное сразу соединение пул закрывает мгновенно и открывает новое."""
    if not in_greenlet():  # pragma: no cover - выдача соединения в async SQLAlchemy всегда в greenlet
        return
    try:
        await_(record.driver_connection.execute(_PING_SQL, timeout=PG_PING_TIMEOUT_SEC))
    except Exception as exc:  # noqa: BLE001 — любая ошибка: соединение использовать нельзя
        log.info("Соединение с базой простояло и не отвечает (%s) — открываю новое", type(exc).__name__)
        _abort_connection(record)
        raise sa_exc.DisconnectionError(f"проверка соединения не прошла: {type(exc).__name__}") from exc


def _abort_connection(record: Any) -> None:
    """Оборвать соединение сразу, без обмена с сервером (asyncpg ``terminate``: закрыть сокет и отменить
    ожидание ответа на отмену запроса). Не вышло (соединения уже нет) — неважно: его всё равно заменят."""
    try:
        driver_connection = record.driver_connection
        if driver_connection is not None:
            driver_connection.terminate()
    except Exception:  # noqa: BLE001 — соединение и так выбрасывается
        log.debug("Не удалось оборвать соединение с базой", exc_info=True)


def _tune_postgres_engine(engine: AsyncEngine, *, deferred_begin: bool) -> None:
    """Проверка соединения после простоя и отложенный BEGIN (см. «Обмены с базой» в описании модуля)."""
    sync_engine = engine.sync_engine

    def on_connect(dbapi_connection: Any, record: Any) -> None:
        record.info[_LAST_USED] = time.monotonic()  # только что открыто — проверять не нужно
        if deferred_begin:
            dbapi_connection.autocommit = True

    def on_checkout(dbapi_connection: Any, record: Any, _proxy: Any) -> None:
        last_used = record.info.get(_LAST_USED)
        if last_used is None or time.monotonic() - last_used > PG_PING_IDLE_SEC:
            _ping(record)
        if deferred_begin:
            dbapi_connection.autocommit = True

    def on_checkin(_dbapi_connection: Any, record: Any) -> None:
        record.info[_LAST_USED] = time.monotonic()

    event.listen(sync_engine, "connect", on_connect)
    event.listen(sync_engine, "checkout", on_checkout)
    event.listen(sync_engine, "checkin", on_checkin)
    if not deferred_begin:
        return

    def begin_before_write(
        conn: Any, _cursor: Any, statement: str, _parameters: Any, context: Any, _executemany: bool
    ) -> None:
        # Транзакции ещё нет (autocommit): выражению, которому она нужна, asyncpg сначала отправит BEGIN.
        dbapi_connection = _driver_connection_of(conn)
        if dbapi_connection is not None and dbapi_connection.autocommit and not _is_plain_read(statement, context):
            dbapi_connection.autocommit = False

    def defer_next_begin(conn: Any) -> None:
        # COMMIT/ROLLBACK открытой транзакции asyncpg выполнит и так; следующая — снова с первой записи.
        dbapi_connection = _driver_connection_of(conn)
        if dbapi_connection is not None:
            dbapi_connection.autocommit = True

    event.listen(sync_engine, "before_cursor_execute", begin_before_write)
    event.listen(sync_engine, "commit", defer_next_begin)
    event.listen(sync_engine, "rollback", defer_next_begin)


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
    source_url = parsed  # с параметрами: по ним видно transaction pooler
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
        # Без pool_pre_ping: он проверял каждую выдачу соединения тремя обменами с базой.
        # Простоявшее соединение проверяет _tune_postgres_engine — одним обменом.
        kwargs.update(
            pool_size=PG_POOL_SIZE,
            max_overflow=PG_MAX_OVERFLOW,
            pool_recycle=PG_POOL_RECYCLE_SEC,
            pool_use_lifo=True,
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
    queue_pool = poolclass is None or issubclass(poolclass, QueuePool)
    if not queue_pool:
        for name in _SIZED_POOL_ARGS + ("pool_use_lifo",):
            kwargs.pop(name, None)

    engine = create_async_engine(parsed, **kwargs)

    if backend == "postgresql":
        # Отложенный BEGIN — только у своего пула соединений: на transaction pooler нельзя (см. описание
        # модуля), а с одним общим соединением на все сессии (StaticPool в тестах) сессии и так делят
        # одну транзакцию.
        pooler = _is_transaction_pooler(source_url, kwargs.get("connect_args") or {})
        _tune_postgres_engine(engine, deferred_begin=queue_pool and not pooler)

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

    Хранилище держит диалоги в памяти и ходит в базу только при первом обращении к ключу (чтение) и
    фоновой записью изменений (``bot.fsm_storage``), пока сессия хендлера (``DbSessionMiddleware``)
    держит своё соединение. Из общего пула пять апдейтов разных людей заняли бы все соединения, и
    чтение диалога ждало бы шестое — бот «вставал» бы на ``pool_timeout`` (30 с). Свой пул из одного
    соединения (``PG_STORAGE_POOL_SIZE``) этого не допускает: его операции занимают соединение на
    миллисекунды и сами ничего не ждут.
    SQLite (запись вдогонку, у базы в памяти — память) — None: хранилищу хватает основного движка.
    """
    if not is_postgres_url(url):
        return None
    kwargs: dict[str, Any] = {"pool_size": PG_STORAGE_POOL_SIZE, "max_overflow": 0, **engine_kwargs}
    return make_engine(url, password=password, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def warm_up(*engines: AsyncEngine | None) -> None:
    """Заранее открыть по соединению в пулах (при запуске бота), чтобы первый апдейт не ждал
    подключения к облачной базе (TCP + TLS + пароль — 1–1,4 с). Ошибка — только в лог: подключение
    повторится при первом обращении."""
    for engine in engines:
        if engine is None:
            continue
        try:
            async with engine.connect():
                pass
        except Exception as exc:  # noqa: BLE001 — прогрев необязателен
            log.warning("Не удалось заранее подключиться к базе: %s", type(exc).__name__)


async def init_db(engine: AsyncEngine) -> None:
    """Создать недостающие таблицы и добавить в существующие новые колонки моделей (данные не трогаются)."""
    from bot.db import models  # noqa: F401 - регистрирует таблицы в metadata

    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # Два экземпляра бота (обновление на Render) могут стартовать одновременно:
            # второй дождётся, пока первый создаст таблицы, и увидит их готовыми.
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _INIT_LOCK_KEY})
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        if conn.dialect.name == "postgresql":
            await _enable_row_level_security(conn)


def _add_missing_columns(conn: Any) -> None:
    """Добавить в существующие таблицы колонки, которые появились в моделях (``create_all`` создаёт
    только недостающие таблицы целиком).

    Так добавляются лишь колонки, которые можно дописать в таблицу с данными: допускающие NULL или
    со значением по умолчанию на стороне базы (``server_default``). Прочие — ошибка запуска: такой
    колонке нужна отдельная миграция. На PostgreSQL — ``ADD COLUMN IF NOT EXISTS`` (и вызывающий код
    держит advisory-блокировку): два экземпляра бота, стартующие одновременно, не столкнутся.
    """
    inspector = inspect(conn)
    preparer = conn.dialect.identifier_preparer
    guard = "IF NOT EXISTS " if conn.dialect.name == "postgresql" else ""
    for table in Base.metadata.sorted_tables:
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable and column.server_default is None:
                raise RuntimeError(
                    f"Колонку {table.name}.{column.name} нельзя добавить в существующую таблицу автоматически: "
                    "она обязательная и без server_default"
                )
            definition = CreateColumn(column).compile(dialect=conn.dialect)
            conn.execute(text(f"ALTER TABLE {preparer.format_table(table)} ADD COLUMN {guard}{definition}"))
            log.info("База: в таблицу %s добавлена колонка %s", table.name, column.name)


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
