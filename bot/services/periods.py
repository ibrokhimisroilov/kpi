"""Отчётные периоды: неделя, месяц, квартал, год.

Границы считаются в местном времени (settings.timezone), неделя — с понедельника,
затем переводятся в naive UTC для запросов к БД. Период — полуинтервал [start, end).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from bot.services.errors import DomainError
from bot.utils.dates import MONTHS_NOM, to_local, to_utc, utcnow

PERIOD_KINDS = ("week", "month", "quarter", "year")

_SHORT_LABELS = {"week": "Неделя", "month": "Месяц", "quarter": "Квартал", "year": "Год"}
_ROMAN_QUARTERS = ("I", "II", "III", "IV")


@dataclass(frozen=True)
class Period:
    kind: str
    offset: int
    start: datetime  # naive UTC, включительно
    end: datetime    # naive UTC, не включительно
    label: str       # «Неделя 28.09–04.10.2026», «Октябрь 2026», «IV квартал 2026», «2026 год»
    short: str       # «Неделя», «Месяц», «Квартал», «Год»


def get_period(kind: str, offset: int = 0, now: datetime | None = None) -> Period:
    """Период вида kind, сдвинутый на offset целых периодов от текущего (-1 — предыдущий)."""
    if kind not in PERIOD_KINDS:
        raise DomainError(f"Неизвестный период: {kind}")
    today = to_local(now or utcnow()).date()
    builders = {"week": _week, "month": _month, "quarter": _quarter, "year": _year}
    try:
        start, end, label = builders[kind](today, offset)
        start_utc = to_utc(datetime.combine(start, time.min))
        end_utc = to_utc(datetime.combine(end, time.min))
    except (OverflowError, ValueError) as exc:  # сдвиг за пределы календаря (подделанная кнопка)
        raise DomainError("Такого периода нет — выберите период заново") from exc
    return Period(
        kind=kind,
        offset=offset,
        start=start_utc,
        end=end_utc,
        label=label,
        short=_SHORT_LABELS[kind],
    )


def _add_months(year: int, month: int, delta: int) -> date:
    """Первое число месяца, отстоящего от (year, month) на delta месяцев."""
    index = year * 12 + (month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def _week(today: date, offset: int) -> tuple[date, date, str]:
    start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    end = start + timedelta(days=7)
    last = end - timedelta(days=1)
    first_fmt = "%d.%m" if start.year == last.year else "%d.%m.%Y"
    return start, end, f"Неделя {start.strftime(first_fmt)}–{last:%d.%m.%Y}"


def _month(today: date, offset: int) -> tuple[date, date, str]:
    start = _add_months(today.year, today.month, offset)
    end = _add_months(start.year, start.month, 1)
    return start, end, f"{MONTHS_NOM[start.month - 1]} {start.year}"


def _quarter(today: date, offset: int) -> tuple[date, date, str]:
    first_month = (today.month - 1) // 3 * 3 + 1
    start = _add_months(today.year, first_month, offset * 3)
    end = _add_months(start.year, start.month, 3)
    return start, end, f"{_ROMAN_QUARTERS[(start.month - 1) // 3]} квартал {start.year}"


def _year(today: date, offset: int) -> tuple[date, date, str]:
    start = date(today.year + offset, 1, 1)
    return start, date(start.year + 1, 1, 1), f"{start.year} год"
