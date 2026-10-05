"""Ежедневная резервная копия базы данных — файлом в Telegram каждому активному руководителю.

Бот может работать на бесплатном облачном сервере, который провайдер вправе остановить или удалить
вместе с диском. Копия базы в чатах руководителей — страховка: из неё восстанавливаются все задачи,
оценки, история и сотрудники (как — README, раздел «Резервные копии»).

Копия — всегда файл SQLite (``kpi_backup_ГГГГ-ММ-ДД.db``), какую бы базу бот ни использовал:

* **SQLite в файле** — снимок встроенным в SQLite механизмом online backup
  (``sqlite3.Connection.backup``): копия согласованная, даже если бот в этот момент пишет в базу (WAL),
  и останавливать бота не нужно. Снимок собирается в памяти (в отдельном потоке, чтобы не задерживать
  бота) и сразу отправляется.
* **PostgreSQL** (Supabase) и другие СУБД — все таблицы бота (``Base.metadata.sorted_tables``)
  переписываются во временный файл SQLite с той же схемой. Чтение — одна транзакция REPEATABLE READ
  (все таблицы на один момент времени, бот при этом работает), порциями; запись в файл — в отдельном
  потоке. Такой файл бот открывает как ``data/bot.db``, а в PostgreSQL его загружает
  ``python -m bot.tools.restore`` — копия восстанавливается без доступа к облачному хостингу.

Telegram принимает от бота файлы до 50 МБ: копию больше MAX_BACKUP_BYTES бот не отправляет, а один раз
(до удачной копии или перезапуска бота) предупреждает руководителей, что копию нужно делать вручную.

send_backup никогда не бросает исключений: сбой копии пишется в лог и не мешает работе бота.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile, Message
from sqlalchemy import Table, create_engine, select
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.config import get_settings
from bot.db.base import Base, make_engine, normalize_url
from bot.db.models import User
from bot.services import reminders, users
from bot.utils.dates import to_local, utcnow
from bot.utils.text import fmt_num

__all__ = [
    "MAX_BACKUP_BYTES",
    "BackupTooLarge",
    "backup_filename",
    "export_database",
    "make_backup_bytes",
    "send_backup",
]

log = logging.getLogger(__name__)

Sessionmaker = async_sessionmaker[AsyncSession]

MAX_BACKUP_BYTES = 45 * 1024 * 1024  # Telegram принимает от бота файлы до 50 МБ — берём с запасом
_UPLOAD_TIMEOUT_SEC = 300            # загрузка файла в десятки МБ по медленной связи
_SQLITE_TIMEOUT_SEC = 30             # подождать, если база занята записью
_MAX_RETRY_WAIT = 60                 # дольше ждать по флуд-лимиту не будем — копия уйдёт завтра
_SEND_ERRORS = (TelegramAPIError, OSError)  # OSError покрывает и TimeoutError
_EXPORT_CHUNK_ROWS = 500             # строк за раз при выгрузке из PostgreSQL (память Render — 512 МБ)

CAPTION = (
    "💾 Резервная копия базы за {date}. "
    "Храните этот файл: из него можно восстановить все задачи и оценки."
)
TOO_BIG_TEXT = (
    "⚠️ Резервная копия базы за {date} не отправлена: база занимает {size} МБ, "
    "а через Telegram бот может переслать файл не больше {limit} МБ.\n\n"
    "Сделайте копию вручную: остановите бота и скопируйте папку <code>data</code> "
    "(подробно — в README, раздел «Резервные копии»)."
)
# База не в файле (PostgreSQL): выгрузку прерывают, как только копия превысила предел.
TOO_BIG_TEXT_SERVER = (
    "⚠️ Резервная копия базы за {date} не отправлена: копия занимает больше {limit} МБ, "
    "а через Telegram бот может переслать файл не больше {limit} МБ.\n\n"
    "Сделайте копию базы вручную средствами хостинга базы данных (например, <code>pg_dump</code>) — "
    "подробно в README, раздел «Резервные копии»."
)

# Базы, о слишком большом размере которых руководители уже предупреждены. Сбрасывается удачной
# копией и перезапуском бота — чтобы не писать одно и то же каждый вечер.
_too_big_warned: set[str] = set()


class BackupTooLarge(Exception):
    """Снимок базы больше допустимого размера (он не делается или отбрасывается)."""

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"database snapshot is {size} bytes, limit {limit}")
        self.size = size
        self.limit = limit


# --- Снимок базы -------------------------------------------------------------------------------


def _sqlite_path(database_url: str | URL) -> Path | None:
    """Путь к файлу SQLite из DATABASE_URL; None — база не SQLite или не в файле (:memory:)."""
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        return None
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database or ""
    if not database or database == ":memory:" or database.startswith("file:") or url.query.get("mode") == "memory":
        return None
    return Path(database)


def _snapshot(path: Path, max_bytes: int | None) -> bytes:
    """Согласованный снимок файла SQLite через online backup API (синхронно — для asyncio.to_thread).

    Размер снимка (число страниц × размер страницы) проверяется до копирования, чтобы не читать
    в память базу, которую всё равно нельзя отправить.
    """
    # mode=rw: не создавать пустую базу, если файла нет (его удалили между проверкой и открытием).
    source_uri = f"{path.resolve().as_uri()}?mode=rw"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=_SQLITE_TIMEOUT_SEC)) as source:
        if max_bytes is not None:
            page_count = source.execute("PRAGMA page_count").fetchone()[0]
            page_size = source.execute("PRAGMA page_size").fetchone()[0]
            if page_count * page_size > max_bytes:
                raise BackupTooLarge(page_count * page_size, max_bytes)
        with closing(sqlite3.connect(":memory:")) as target:
            source.backup(target)  # все страницы за один шаг — снимок на один момент времени
            data = target.serialize()
    if max_bytes is not None and len(data) > max_bytes:  # база выросла между проверкой и копированием
        raise BackupTooLarge(len(data), max_bytes)
    return data


def _create_schema(target: Engine) -> None:
    Base.metadata.create_all(target)


def _exported_size(sink: Connection) -> int:
    """Текущий размер базы SQLite (вместе со страницами незавершённой транзакции)."""
    page_count = sink.exec_driver_sql("PRAGMA page_count").scalar_one()
    page_size = sink.exec_driver_sql("PRAGMA page_size").scalar_one()
    return int(page_count) * int(page_size)


def _write_rows(sink: Connection, table: Table, rows: Sequence[Any], max_bytes: int | None) -> None:
    """Порция строк таблицы -> файл SQLite (синхронно — для asyncio.to_thread)."""
    sink.execute(table.insert(), [dict(row._mapping) for row in rows])
    if max_bytes is not None:
        size = _exported_size(sink)
        if size > max_bytes:
            raise BackupTooLarge(size, max_bytes)


async def export_database(engine: AsyncEngine, *, max_bytes: int | None = None) -> bytes:
    """Все таблицы бота из любой базы (PostgreSQL, SQLite, ...) -> содержимое нового файла SQLite (.db).

    Схема файла — та же, что создаёт бот (``Base.metadata.create_all``), первичные ключи сохраняются:
    файл открывается ботом как data/bot.db и загружается в PostgreSQL ``python -m bot.tools.restore``.
    Чтение — одна транзакция (в PostgreSQL — REPEATABLE READ: все таблицы на один момент времени),
    порциями по _EXPORT_CHUNK_ROWS строк; запись во временный файл — в отдельном потоке, файл удаляется.
    max_bytes — копия выросла больше -> BackupTooLarge (выгрузка прерывается сразу).
    """
    fd, name = tempfile.mkstemp(prefix="kpi_backup_", suffix=".db")
    os.close(fd)
    path = Path(name)
    target = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False})
    try:
        await asyncio.to_thread(_create_schema, target)
        sink = await asyncio.to_thread(target.connect)
        try:
            async with engine.connect() as source:
                if source.dialect.name == "postgresql":
                    source = await source.execution_options(isolation_level="REPEATABLE READ")
                async with source.begin():
                    for table in Base.metadata.sorted_tables:
                        result = await source.stream(select(table))
                        async for rows in result.partitions(_EXPORT_CHUNK_ROWS):
                            await asyncio.to_thread(_write_rows, sink, table, rows, max_bytes)
            await asyncio.to_thread(sink.commit)
        finally:
            await asyncio.to_thread(sink.close)
        await asyncio.to_thread(target.dispose)
        data = await asyncio.to_thread(path.read_bytes)
    finally:
        target.dispose()
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - файл ещё открыт (Windows): удалит очистка временной папки
            log.warning("Не удалось удалить временный файл копии %s", path)
    if max_bytes is not None and len(data) > max_bytes:
        raise BackupTooLarge(len(data), max_bytes)
    return data


async def make_backup_bytes(
    database: str | URL | AsyncEngine, *, max_bytes: int | None = None
) -> bytes | None:
    """Копия базы — содержимое файла SQLite (.db) — или None, если копировать нечего.

    database — DATABASE_URL или движок бота (AsyncEngine: копия PostgreSQL читается через его пул,
    без нового подключения). SQLite в файле — снимок online backup; PostgreSQL и другие СУБД —
    выгрузка всех таблиц во временный файл SQLite (export_database).
    None — неверный адрес, SQLite в памяти (``:memory:``) или файла базы нет.
    max_bytes — предельный размер копии: больше -> BackupTooLarge.
    Ошибки базы и файловой системы пробрасываются (send_backup их ловит и логирует).
    """
    engine = database if isinstance(database, AsyncEngine) else None
    try:
        url = engine.url if engine is not None else normalize_url(database)  # type: ignore[arg-type]
    except (ArgumentError, ValueError):
        return None
    if url.get_backend_name() == "sqlite":
        path = _sqlite_path(url)
        if path is None:
            return None
        if not path.is_file():
            log.warning("Резервная копия: файл базы %s не найден", path)
            return None
        return await asyncio.to_thread(_snapshot, path, max_bytes)
    if engine is not None:
        return await export_database(engine, max_bytes=max_bytes)
    engine = make_engine(url)
    try:
        return await export_database(engine, max_bytes=max_bytes)
    finally:
        await engine.dispose()


def backup_filename(now: datetime) -> str:
    """Имя файла копии по местной дате: kpi_backup_2026-10-02.db (now — naive UTC)."""
    return f"kpi_backup_{to_local(now):%Y-%m-%d}.db"


# --- Отправка ------------------------------------------------------------------------------------


def _reason(exc: BaseException) -> str:
    """Причина для лога. Текст сетевых ошибок не пишем: в нём может оказаться URL с токеном бота."""
    if isinstance(exc, TelegramAPIError) and not isinstance(exc, TelegramNetworkError):
        return exc.message
    return type(exc).__name__


async def _call(action: Callable[[], Awaitable[Message]], chat_id: int, what: str) -> Message | None:
    """Запрос к Telegram без исключений (как notify.safe_send): при флуд-лимите — подождать и повторить
    один раз; бот заблокирован, ошибка Telegram или сети — запись в лог и None."""
    for attempt in (1, 2):
        try:
            return await action()
        except TelegramRetryAfter as exc:
            if attempt == 2 or exc.retry_after > _MAX_RETRY_WAIT:
                log.warning("Флуд-лимит Telegram: %s в чат %s не отправлено", what, chat_id)
                return None
            log.info("Флуд-лимит Telegram: ждём %s с и повторяем (%s, чат %s)", exc.retry_after, what, chat_id)
            await asyncio.sleep(exc.retry_after)
        except TelegramForbiddenError:
            log.info("Чат %s недоступен (бот заблокирован?) — %s не доставлено", chat_id, what)
            return None
        except _SEND_ERRORS as exc:
            log.warning("Не удалось отправить %s в чат %s: %s", what, chat_id, _reason(exc))
            return None
    return None


def _database(sessionmaker: Sessionmaker) -> AsyncEngine | str:
    """База, с которой работает бот: движок фабрики сессий, иначе DATABASE_URL из настроек."""
    bind = sessionmaker.kw.get("bind")
    return bind if isinstance(bind, AsyncEngine) else get_settings().database_url


def _is_file_database(database: AsyncEngine | str) -> bool:
    """База — SQLite (копию вручную делают копированием папки data)."""
    try:
        url = database.url if isinstance(database, AsyncEngine) else normalize_url(database)
    except (ArgumentError, ValueError):
        return False
    return url.get_backend_name() == "sqlite"


async def _warn_too_big(
    bot: Bot, managers: list[User], key: str, exc: BackupTooLarge, now: datetime, *, file_database: bool
) -> None:
    """Один раз сообщить руководителям, что база слишком большая для отправки через Telegram."""
    if key in _too_big_warned:
        return
    text = (TOO_BIG_TEXT if file_database else TOO_BIG_TEXT_SERVER).format(
        date=f"{to_local(now):%d.%m.%Y}",
        size=fmt_num(round(exc.size / 1024 / 1024, 1)),
        limit=fmt_num(exc.limit // (1024 * 1024)),
    )
    quiet = reminders.in_quiet_hours(now)
    delivered = 0
    for manager in managers:
        chat_id = manager.tg_id
        sent = await _call(
            lambda: bot.send_message(chat_id, text, disable_notification=quiet),
            chat_id,
            "предупреждение о размере резервной копии",
        )
        if sent is not None:
            delivered += 1
    if delivered:
        _too_big_warned.add(key)


async def _send_backup(bot: Bot, sessionmaker: Sessionmaker, now: datetime) -> int:
    async with sessionmaker() as session:
        managers = await users.list_managers(session)
    if not managers:
        log.warning("Резервная копия базы не отправлена: нет активных руководителей")
        return 0
    database = _database(sessionmaker)
    file_database = _is_file_database(database)
    # Ключ отметки «уже предупредили» — адрес базы (str(URL) — без пароля).
    key = str(database.url if isinstance(database, AsyncEngine) else database)
    try:
        data = await make_backup_bytes(database, max_bytes=MAX_BACKUP_BYTES)
    except BackupTooLarge as exc:
        log.warning(
            "Резервная копия базы не отправлена: копия (%s байт) больше предела Telegram (%s байт) — "
            "сделайте копию вручную (README, «Резервные копии»)",
            exc.size,
            exc.limit,
        )
        await _warn_too_big(bot, managers, key, exc, now, file_database=file_database)
        return 0
    if data is None:
        log.info("Резервная копия базы не делается: база в памяти или файла базы нет")
        return 0
    _too_big_warned.discard(key)

    caption = CAPTION.format(date=f"{to_local(now):%d.%m.%Y}")
    document: BufferedInputFile | str = BufferedInputFile(data, filename=backup_filename(now))
    delivered = 0
    for manager in managers:
        chat_id, file = manager.tg_id, document
        message = await _call(
            # Без звука: копия приходит каждый день, обычно в «тихие часы».
            lambda: bot.send_document(
                chat_id,
                file,
                caption=caption,
                disable_notification=True,
                request_timeout=_UPLOAD_TIMEOUT_SEC,
            ),
            chat_id,
            "резервную копию базы",
        )
        if message is None:
            continue
        delivered += 1
        if message.document is not None:
            document = message.document.file_id  # остальным — тот же файл, без повторной загрузки
    log.info(
        "Резервная копия базы (%s КБ) отправлена руководителям: %s из %s",
        len(data) // 1024,
        delivered,
        len(managers),
    )
    return delivered


async def send_backup(bot: Bot, sessionmaker: Sessionmaker, now: datetime | None = None) -> int:
    """Отправить снимок базы файлом каждому активному руководителю (без звука).

    Имя файла — kpi_backup_ГГГГ-ММ-ДД.db, дата — местная (now — naive UTC, по умолчанию «сейчас»).
    База больше MAX_BACKUP_BYTES — файл не отправляется: предупреждение в лог и один раз руководителям.
    Возвращает, скольким руководителям доставлен файл. Никогда не бросает исключений.
    """
    try:
        return await _send_backup(bot, sessionmaker, now or utcnow())
    except Exception:
        log.exception("Не удалось сделать или отправить резервную копию базы")
        return 0
