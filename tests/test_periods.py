"""bot.services.periods: границы недели/месяца/квартала/года в местном времени, сдвиги, подписи."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bot.services.errors import DomainError
from bot.services.periods import PERIOD_KINDS, Period, get_period
from bot.utils.dates import to_local, to_utc

NOW = datetime(2026, 10, 2, 7, 0)  # пятница 02.10.2026 12:00 по Ташкенту


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return to_utc(datetime(year, month, day, hour, minute))


def test_period_kinds() -> None:
    assert PERIOD_KINDS == ("week", "month", "quarter", "year")


def test_current_week_starts_on_local_monday() -> None:
    period = get_period("week", 0, NOW)
    assert isinstance(period, Period)
    assert (period.kind, period.offset, period.short) == ("week", 0, "Неделя")
    assert period.start == local(2026, 9, 28) == datetime(2026, 9, 27, 19, 0)
    assert period.end == local(2026, 10, 5)
    assert to_local(period.start).weekday() == 0
    assert period.label == "Неделя 28.09–04.10.2026"
    assert period.start <= NOW < period.end


@pytest.mark.parametrize(
    ("now_local", "monday"),
    [
        (datetime(2026, 10, 5, 0, 30), datetime(2026, 10, 5)),    # понедельник сразу после полуночи
        (datetime(2026, 10, 4, 23, 59), datetime(2026, 9, 28)),   # воскресенье 23:59 — ещё прошлая неделя
        (datetime(2026, 10, 5, 3, 0), datetime(2026, 10, 5)),     # в UTC это ещё воскресенье
    ],
)
def test_week_boundary_in_local_time(now_local: datetime, monday: datetime) -> None:
    period = get_period("week", 0, to_utc(now_local))
    assert period.start == to_utc(monday)
    assert period.end == to_utc(monday + timedelta(days=7))


@pytest.mark.parametrize(
    ("offset", "start", "label"),
    [
        (-1, datetime(2026, 9, 21), "Неделя 21.09–27.09.2026"),
        (1, datetime(2026, 10, 5), "Неделя 05.10–11.10.2026"),
        (-5, datetime(2026, 8, 24), "Неделя 24.08–30.08.2026"),
    ],
)
def test_week_offsets(offset: int, start: datetime, label: str) -> None:
    period = get_period("week", offset, NOW)
    assert period.start == to_utc(start)
    assert period.end - period.start == timedelta(days=7)
    assert period.label == label


def test_week_across_new_year_label() -> None:
    period = get_period("week", 0, local(2026, 12, 30, 12))
    assert period.start == local(2026, 12, 28)
    assert period.label == "Неделя 28.12.2026–03.01.2027"


@pytest.mark.parametrize(
    ("offset", "start", "end", "label"),
    [
        (0, (2026, 10, 1), (2026, 11, 1), "Октябрь 2026"),
        (-1, (2026, 9, 1), (2026, 10, 1), "Сентябрь 2026"),
        (-10, (2025, 12, 1), (2026, 1, 1), "Декабрь 2025"),
        (3, (2027, 1, 1), (2027, 2, 1), "Январь 2027"),
        (-8, (2026, 2, 1), (2026, 3, 1), "Февраль 2026"),
    ],
)
def test_month(offset: int, start: tuple, end: tuple, label: str) -> None:
    period = get_period("month", offset, NOW)
    assert period.start == local(*start)
    assert period.end == local(*end)
    assert period.label == label
    assert period.short == "Месяц"


@pytest.mark.parametrize(
    ("offset", "start", "end", "label"),
    [
        (0, (2026, 10, 1), (2027, 1, 1), "IV квартал 2026"),
        (-1, (2026, 7, 1), (2026, 10, 1), "III квартал 2026"),
        (-3, (2026, 1, 1), (2026, 4, 1), "I квартал 2026"),
        (-4, (2025, 10, 1), (2026, 1, 1), "IV квартал 2025"),
        (1, (2027, 1, 1), (2027, 4, 1), "I квартал 2027"),
    ],
)
def test_quarter(offset: int, start: tuple, end: tuple, label: str) -> None:
    period = get_period("quarter", offset, NOW)
    assert period.start == local(*start)
    assert period.end == local(*end)
    assert period.label == label
    assert period.short == "Квартал"


@pytest.mark.parametrize(
    ("offset", "year"),
    [(0, 2026), (-1, 2025), (1, 2027)],
)
def test_year(offset: int, year: int) -> None:
    period = get_period("year", offset, NOW)
    assert period.start == local(year, 1, 1)
    assert period.end == local(year + 1, 1, 1)
    assert period.label == f"{year} год"
    assert period.short == "Год"


def test_year_start_is_local_midnight() -> None:
    # 01.01.2026 00:00 по Ташкенту — это 31.12.2025 19:00 UTC.
    assert get_period("year", 0, NOW).start == datetime(2025, 12, 31, 19, 0)


def test_new_year_night_uses_local_date() -> None:
    # 31.12.2026 20:00 UTC = 01.01.2027 01:00 по Ташкенту.
    now = datetime(2026, 12, 31, 20, 0)
    assert get_period("year", 0, now).label == "2027 год"
    assert get_period("quarter", 0, now).label == "I квартал 2027"
    assert get_period("month", 0, now).label == "Январь 2027"


@pytest.mark.parametrize("kind", PERIOD_KINDS)
def test_periods_are_contiguous(kind: str) -> None:
    previous = get_period(kind, -1, NOW)
    current = get_period(kind, 0, NOW)
    following = get_period(kind, 1, NOW)
    assert previous.end == current.start
    assert current.end == following.start
    assert current.start <= NOW < current.end


@pytest.mark.parametrize("kind", PERIOD_KINDS)
def test_default_now_is_current_time(kind: str) -> None:
    period = get_period(kind)
    assert period.offset == 0
    assert period.start < period.end


def test_unknown_kind() -> None:
    with pytest.raises(DomainError):
        get_period("decade", 0, NOW)
