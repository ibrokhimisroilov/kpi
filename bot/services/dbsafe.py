"""Данные в пределах, которые соблюдает база данных (PostgreSQL проверяет их строго).

SQLite сохраняет в VARCHAR(n) строку любой длины, текст с NUL-символом и целое до 2^63 в INTEGER.
PostgreSQL на то же отвечает ошибкой — и обработка апдейта падает с «Произошла ошибка»:

* «value too long for type character varying(n)» — строка длиннее колонки;
* «invalid byte sequence for encoding "UTF8": 0x00» — NUL-символ в тексте;
* «value out of int32 range» — целое больше 2^31-1 в колонке INTEGER (id, размер файла);
* «OFFSET/LIMIT must not be negative» — отрицательная страница списка;
* naive-колонка DateTime не принимает дату с часовым поясом (asyncpg: DataError).

Сервисы пропускают пользовательские данные через эти функции до записи в базу и до запросов.
Длины колонок берутся из моделей (bot/db/models.py), чтобы не расходиться с ними.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "INT32_MAX",
    "INT32_MIN",
    "INT64_MAX",
    "INT64_MIN",
    "clamp_int",
    "clean_text",
    "clip",
    "clip_file_name",
    "finite_or_none",
    "column_length",
    "is_db_id",
    "naive_utc",
    "non_negative",
    "sql_limit",
]

INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1   # INTEGER (первичные ключи, размер файла)
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1   # BIGINT (Telegram ID), LIMIT / OFFSET

_ELLIPSIS = "…"
_MAX_EXTENSION = 16  # длиннее — это не расширение файла, а часть имени


def column_length(attribute: Any) -> int | None:
    """Длина строковой колонки модели: ``column_length(User.username)`` -> 64 (None — без ограничения)."""
    return getattr(attribute.type, "length", None)


def clean_text(value: str) -> str:
    """Убрать то, что PostgreSQL не примет в тексте: NUL-символы и одиночные суррогаты UTF-16.

    Одиночный суррогат (обрывок эмодзи) не кодируется в UTF-8 ни для какой базы — заменяется на «�».
    """
    if "\x00" in value:
        value = value.replace("\x00", "")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        value = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return value


def clip(value: str | None, limit: int | None) -> str | None:
    """Очищенная строка не длиннее limit символов (None остаётся None)."""
    if value is None:
        return None
    value = clean_text(str(value))
    if limit is not None and len(value) > limit:
        return value[:limit]
    return value


def clip_file_name(value: str | None, limit: int | None) -> str | None:
    """Имя файла не длиннее limit, расширение сохраняется: «Очень_длинное…имя.pdf»."""
    value = clip(value, None)
    if value is None or limit is None or len(value) <= limit:
        return value
    stem, dot, extension = value.rpartition(".")
    if dot and stem and 0 < len(extension) <= _MAX_EXTENSION and limit > len(extension) + 2:
        return stem[: limit - len(extension) - 2] + _ELLIPSIS + "." + extension
    return value[: limit - 1] + _ELLIPSIS if limit > 1 else value[:limit]


def is_db_id(value: Any, *, big: bool = False) -> bool:
    """Целое, которое поместится в INTEGER (big=True — в BIGINT): иначе PostgreSQL отвергнет запрос.

    Id из кнопок ограничены 64 битами (bot.ui.callbacks.DbInt), а первичные ключи — INTEGER:
    подделанная кнопка с id = 2^40 должна давать «не найдено», а не ошибку базы.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    low, high = (INT64_MIN, INT64_MAX) if big else (INT32_MIN, INT32_MAX)
    return low <= value <= high


def clamp_int(value: int | None, low: int = INT32_MIN, high: int = INT32_MAX) -> int | None:
    """Целое в пределах колонки; None и не-целое -> None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return min(max(number, low), high)


def non_negative(value: int | None, high: int = INT64_MAX) -> int | None:
    """OFFSET: PostgreSQL не принимает отрицательный (SQLite считает его нулём) — как в SQLite, 0."""
    if value is None:
        return None
    return min(max(int(value), 0), high)


def sql_limit(value: int | None) -> int | None:
    """LIMIT: отрицательный в SQLite — «без ограничения», PostgreSQL его не принимает — None (без LIMIT)."""
    if value is None or int(value) < 0:
        return None
    return min(int(value), INT64_MAX)


def naive_utc(value: datetime) -> datetime:
    """Aware-дату перевести в naive UTC; naive считается уже UTC (как в БД)."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def finite_or_none(value: float) -> float | None:
    """JSON-колонка PostgreSQL не принимает NaN / Infinity (json.dumps пишет их без кавычек)."""
    return value if math.isfinite(value) else None
