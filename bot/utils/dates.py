"""Работа со временем. В БД — naive UTC, пользователю — местное время (settings.timezone)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from bot.config import get_settings
from bot.db.models import utcnow

__all__ = [
    "utcnow",
    "local_now",
    "to_local",
    "to_utc",
    "local_end_of_day_utc",
    "deadline_from_local_date",
    "fmt_date",
    "fmt_datetime",
    "fmt_deadline",
    "days_between",
]

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
MONTHS_NOM = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_YEAR_HINT_DAYS = 300  # fmt_deadline: дальше этого от сегодня — срок показывается с годом


def local_now() -> datetime:
    """Текущее местное время (aware)."""
    return datetime.now(get_settings().tz)


def to_local(dt_utc: datetime) -> datetime:
    """naive UTC из БД -> aware местное время."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=UTC)
    return dt_utc.astimezone(get_settings().tz)


def to_utc(dt_local: datetime) -> datetime:
    """Местное время (naive трактуется как местное, или aware) -> naive UTC для БД."""
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=get_settings().tz)
    return dt_local.astimezone(UTC).replace(tzinfo=None)


def _default_deadline_time() -> time:
    hh, mm = get_settings().default_deadline_time.split(":")
    return time(int(hh), int(mm))


def deadline_from_local_date(day: date, at: time | None = None) -> datetime:
    """Дата (+ необязательное время) по местному времени -> naive UTC.

    Если время не указано, берётся settings.default_deadline_time (по умолчанию 18:00).
    """
    return to_utc(datetime.combine(day, at or _default_deadline_time()))


def local_end_of_day_utc(day: date) -> datetime:
    return to_utc(datetime.combine(day, time(23, 59)))


def fmt_date(dt_utc: datetime) -> str:
    """«05.10.2026»."""
    return to_local(dt_utc).strftime("%d.%m.%Y")


def fmt_datetime(dt_utc: datetime) -> str:
    """«05.10.2026 18:00»."""
    return to_local(dt_utc).strftime("%d.%m.%Y %H:%M")


def fmt_deadline(dt_utc: datetime) -> str:
    """«5 октября (вс), 18:00» — для карточек задач.

    Срок дальше ~10 месяцев от сегодняшнего дня (в обе стороны) — с годом: «5 октября 2030 (сб), 18:00»,
    иначе опечатку в годе («05.10.2030» вместо «05.10.2026») не заметить.
    """
    loc = to_local(dt_utc)
    year = ""
    if abs((loc.date() - to_local(utcnow()).date()).days) > _YEAR_HINT_DAYS:
        year = f" {loc.year}"
    return f"{loc.day} {MONTHS_GEN[loc.month - 1]}{year} ({WEEKDAYS_SHORT[loc.weekday()]}), {loc:%H:%M}"


def days_between(later_utc: datetime, earlier_utc: datetime) -> float:
    """Разница в днях (дробная), может быть отрицательной."""
    return (later_utc - earlier_utc) / timedelta(days=1)
