"""Восстановление базы бота из файла резервной копии: ``python -m bot.tools.restore файл.db [--force]``.

Файл копии — база SQLite: ``kpi_backup_ГГГГ-ММ-ДД.db``, который бот каждый вечер присылает руководителям,
или ``data/bot.db`` бота, работавшего на компьютере. Команда копирует из него все строки в базу из
``DATABASE_URL`` (файл ``.env`` в папке бота или переменная окружения) — PostgreSQL (Supabase) или SQLite.
Так восстанавливают облачную базу после сбоя и переносят данные с компьютера в облако.

* Таблицы создаются, если их ещё нет (как при запуске бота — ``init_db``).
* В базе уже есть сотрудники — отказ, чтобы случайно не затереть рабочую базу. ``--force`` — сначала
  удалить все данные бота из базы, затем загрузить копию.
* Первичные ключи (id) сохраняются — связи задач, сдач, файлов и журнала не рвутся. В PostgreSQL
  счётчики id (последовательности) сдвигаются на max(id), чтобы новые записи не получили занятые номера.
* PostgreSQL строже SQLite: слишком длинные строки обрезаются по длине колонки, NUL-символы удаляются,
  целые вне INTEGER и NaN в JSON заменяются — как это делает бот при записи (``bot.services.dbsafe``).
* Всё — одна транзакция: при ошибке база остаётся как была.

Бот на время восстановления должен быть остановлен (иначе он может записать что-то посередине).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Enum,
    Integer,
    String,
    Table,
    create_engine,
    delete,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from bot.db import models  # noqa: F401 — все таблицы бота в Base.metadata
from bot.db.base import Base, describe_url, init_db, make_engine, normalize_url
from bot.services.dbsafe import INT32_MAX, INT32_MIN, clamp_int, clip, clip_file_name

__all__ = ["RestoreError", "RestoreResult", "TABLE_LABELS", "main", "restore"]

SQLITE_HEADER = b"SQLite format 3\x00"
_CHUNK_ROWS = 500

# Таблицы бота по-русски — для итогового отчёта.
TABLE_LABELS: dict[str, str] = {
    "users": "сотрудники и руководители",
    "tasks": "задачи",
    "submissions": "сдачи результатов",
    "attachments": "файлы-подтверждения",
    "task_events": "журнал изменений задач",
    "reminder_log": "отправленные напоминания",
    "digest_log": "еженедельные сводки",
    "job_log": "задания по расписанию",
    "fsm_state": "незавершённые диалоги",
}


class RestoreError(Exception):
    """Восстановление невозможно; текст — по-русски, для того, кто запускает команду."""


@dataclass
class RestoreResult:
    """Что сделано: строк по таблицам, каких таблиц не было в копии, сколько значений поправлено."""

    database: str                                   # адрес базы без пароля
    counts: dict[str, int] = field(default_factory=dict)
    missing_tables: list[str] = field(default_factory=list)
    cleared_rows: int = 0                           # удалено из базы перед загрузкой (--force)
    fixed_values: int = 0                           # значений поправлено под ограничения PostgreSQL
    sequences_reset: bool = False

    @property
    def total(self) -> int:
        return sum(self.counts.values())


# --- Файл копии -------------------------------------------------------------------------------


def _open_backup(path: Path) -> Engine:
    """Движок SQLite для файла копии; не файл SQLite или нет таблиц бота -> RestoreError."""
    if not path.is_file():
        raise RestoreError(f"Файл копии не найден: {path}")
    with path.open("rb") as file:
        header = file.read(len(SQLITE_HEADER))
    if header != SQLITE_HEADER:
        raise RestoreError(
            f"Файл {path.name} — не резервная копия бота (это не база SQLite). "
            "Нужен файл kpi_backup_ГГГГ-ММ-ДД.db из Telegram или data/bot.db."
        )
    engine = create_engine(f"sqlite:///{path.resolve().as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
    except SQLAlchemyError as exc:
        engine.dispose()
        raise RestoreError(f"Файл {path.name} не открывается как база SQLite: {_short(exc)}") from exc
    if not {"users", "tasks"} <= tables:
        engine.dispose()
        raise RestoreError(f"В файле {path.name} нет таблиц бота (users, tasks) — это не резервная копия бота.")
    return engine


def _describe(url: str | URL) -> str:
    """Куда восстанавливаем — для сообщений: путь к файлу SQLite или адрес сервера без пароля."""
    try:
        parsed = normalize_url(url)
    except (ArgumentError, ValueError):
        return "<неверный DATABASE_URL>"
    if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
        return f"SQLite {parsed.database}"
    return describe_url(parsed)


def _same_file(backup_path: Path, url: URL) -> bool:
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return False
    try:
        return Path(url.database).resolve() == backup_path.resolve()
    except OSError:
        return False


# --- Значения под ограничения PostgreSQL ----------------------------------------------------------------


def _finite_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_json(item) for item in value]
    return value


def _fit_value(table: Table, name: str, value: Any) -> Any:
    """Значение, которое примет PostgreSQL (см. bot.services.dbsafe)."""
    if value is None:
        return None
    column = table.c[name]
    if isinstance(value, str) and isinstance(column.type, String) and not isinstance(column.type, Enum):
        limit = getattr(column.type, "length", None)
        if name == "file_name":
            return clip_file_name(value, limit)
        return clip(value, limit)
    if isinstance(column.type, JSON):
        return _finite_json(value)
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and type(column.type) is Integer  # BigInteger (Telegram ID) — 64 бита, ему можно
        and not column.primary_key
        and not column.foreign_keys
        and not INT32_MIN <= value <= INT32_MAX
    ):
        return clamp_int(value)
    return value


def _fit_rows(table: Table, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    fixed = 0
    result: list[dict[str, Any]] = []
    for row in rows:
        new_row: dict[str, Any] = {}
        for name, value in row.items():
            new_value = _fit_value(table, name, value)
            if new_value != value:
                fixed += 1
            new_row[name] = new_value
        result.append(new_row)
    return result, fixed


# --- Восстановление ---------------------------------------------------------------------------------


async def _count_users(conn: AsyncConnection) -> int:
    users = Base.metadata.tables["users"]
    return int(await conn.scalar(select(func.count()).select_from(users)) or 0)


async def _clear(conn: AsyncConnection) -> int:
    """Удалить все строки таблиц бота (дочерние таблицы — первыми)."""
    removed = 0
    for table in reversed(Base.metadata.sorted_tables):
        result = await conn.execute(delete(table))
        removed += max(result.rowcount or 0, 0)
    return removed


async def _copy_table(
    conn: AsyncConnection, backup: Engine, table: Table, columns: list[str], *, fit: bool
) -> tuple[int, int]:
    """Строки таблицы из копии -> база (порциями). Возвращает (строк, поправленных значений)."""
    copied = fixed = 0
    order = [table.c[column.name] for column in table.primary_key.columns if column.name in columns]
    query = select(*(table.c[name] for name in columns)).order_by(*order)
    with backup.connect() as source:
        result = source.execution_options(yield_per=_CHUNK_ROWS).execute(query)
        for chunk in result.partitions():
            rows = [dict(row._mapping) for row in chunk]
            if fit:
                rows, chunk_fixed = _fit_rows(table, rows)
                fixed += chunk_fixed
            await conn.execute(table.insert(), rows)
            copied += len(rows)
    return copied, fixed


async def _reset_sequences(conn: AsyncConnection) -> None:
    """PostgreSQL: следующий id каждой таблицы = max(id) + 1 (строки вставлены с явными id)."""
    quote = conn.dialect.identifier_preparer.quote
    for table in Base.metadata.sorted_tables:
        column = table.autoincrement_column
        if column is None:
            continue
        await conn.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence(:table, :column), "
                f"COALESCE(MAX({quote(column.name)}), 0) + 1, false) FROM {quote(table.name)}"
            ),
            {"table": quote(table.name), "column": column.name},
        )


async def restore(backup_path: Path, database_url: str | URL, *, force: bool = False) -> RestoreResult:
    """Загрузить все строки из файла копии ``backup_path`` в базу ``database_url``.

    В базе уже есть сотрудники и не ``force`` -> RestoreError (база не меняется). Иначе — таблицы бота
    очищаются (без ``force`` в них могут быть только служебные записи) и заполняются из копии; всё —
    одной транзакцией. Ошибки подключения и записи — исключения SQLAlchemy (база не меняется).
    """
    try:
        target_url = normalize_url(database_url)
    except (ArgumentError, ValueError) as exc:
        raise RestoreError("DATABASE_URL записан неверно — сверьтесь с README («Настройки»).") from exc
    if _same_file(backup_path, target_url):
        raise RestoreError("Файл копии и база из DATABASE_URL — один и тот же файл: восстанавливать нечего.")

    result = RestoreResult(database=_describe(target_url))
    backup = _open_backup(backup_path)
    try:
        inspector = inspect(backup)
        backup_tables = set(inspector.get_table_names())
        backup_columns = {name: {col["name"] for col in inspector.get_columns(name)} for name in backup_tables}

        engine = make_engine(target_url)
        try:
            await init_db(engine)
            async with engine.begin() as conn:
                users = await _count_users(conn)
                if users and not force:
                    raise RestoreError(
                        f"В базе {result.database} уже есть данные: сотрудников и руководителей — {users}. "
                        "Чтобы заменить их содержимым копии, запустите команду ещё раз с --force "
                        "(все текущие данные этой базы будут удалены)."
                    )
                result.cleared_rows = await _clear(conn)
                fit = conn.dialect.name != "sqlite"
                for table in Base.metadata.sorted_tables:
                    if table.name not in backup_tables:
                        result.missing_tables.append(table.name)
                        result.counts[table.name] = 0
                        continue
                    columns = [column.name for column in table.columns if column.name in backup_columns[table.name]]
                    copied, fixed = await _copy_table(conn, backup, table, columns, fit=fit)
                    result.counts[table.name] = copied
                    result.fixed_values += fixed
                if conn.dialect.name == "postgresql":
                    await _reset_sequences(conn)
                    result.sequences_reset = True
        finally:
            await engine.dispose()
    finally:
        backup.dispose()
    return result


# --- Командная строка ------------------------------------------------------------------------------


def _short(exc: BaseException) -> str:
    """Причина ошибки одной строкой — без SQL-запроса и значений строк (там данные сотрудников)."""
    orig = getattr(exc, "orig", None) or exc
    first_line = str(orig).strip().splitlines()[0] if str(orig).strip() else ""
    reason = f"{type(orig).__name__}: {first_line}" if first_line else type(orig).__name__
    return reason[:300]


def format_summary(backup_path: Path, result: RestoreResult) -> str:
    lines = [f"Готово: копия {backup_path.name} загружена в базу {result.database}."]
    if result.cleared_rows:
        lines.append(f"Перед загрузкой из базы удалено строк: {result.cleared_rows}.")
    lines.append("Перенесено строк:")
    names = [*TABLE_LABELS, *(name for name in result.counts if name not in TABLE_LABELS)]
    for name in names:
        if name not in result.counts or name in result.missing_tables:
            continue
        lines.append(f"  {TABLE_LABELS.get(name, name)} ({name}): {result.counts[name]}")
    lines.append(f"Всего: {result.total}.")
    if result.missing_tables:
        lines.append(
            "В копии не было таблиц (копия от старой версии бота, они останутся пустыми): "
            + ", ".join(result.missing_tables)
            + "."
        )
    if result.fixed_values:
        lines.append(
            f"Поправлено значений под ограничения PostgreSQL (обрезаны длинные строки и т. п.): {result.fixed_values}."
        )
    if result.sequences_reset:
        lines.append("Счётчики номеров (id) PostgreSQL сдвинуты — новые записи получат свободные номера.")
    lines.append("Можно запускать бота.")
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m bot.tools.restore",
        description=(
            "Восстановить базу бота из файла резервной копии (kpi_backup_ГГГГ-ММ-ДД.db или data/bot.db) "
            "в базу из DATABASE_URL (.env). Бот на это время должен быть остановлен."
        ),
    )
    parser.add_argument("backup", type=Path, help="путь к файлу копии, например kpi_backup_2026-10-02.db")
    parser.add_argument(
        "--force",
        action="store_true",
        help="удалить текущие данные базы и заменить их копией (без этого в непустую базу не пишем)",
    )
    return parser.parse_args(argv)


def _safe_console() -> None:
    """Русский текст не должен ронять команду в консоли с другой кодировкой."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover - поток уже закрыт / не настраивается
                pass


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа: 0 — восстановлено, 1 — отказ или ошибка, 2 — неверные аргументы."""
    _safe_console()
    args = _parse_args(argv)
    from bot.config import get_settings

    try:
        database_url = get_settings().database_url
    except Exception as exc:  # noqa: BLE001 — неверный .env: сообщить по-русски, без трассировки
        print(f"Ошибка: не удалось прочитать настройки (.env): {type(exc).__name__}.", file=sys.stderr)
        return 1
    print(f"Восстанавливаю {args.backup.name} в базу {_describe(database_url)} ...", flush=True)
    try:
        result = asyncio.run(restore(args.backup, database_url, force=args.force))
    except RestoreError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except (SQLAlchemyError, OSError, ImportError, LookupError, ValueError) as exc:
        print(
            "Ошибка: не удалось подключиться к базе или записать данные — база не изменена.\n"
            f"Причина: {_short(exc)}\n"
            "Проверьте DATABASE_URL в .env (для Supabase — строка Session pooler, порт 5432) и интернет.",
            file=sys.stderr,
        )
        return 1
    print(format_summary(args.backup, result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
