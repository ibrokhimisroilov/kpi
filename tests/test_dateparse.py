"""bot.utils.dateparse: разбор сроков по-русски, быстрые варианты срока, ISO -> срок.

«Сейчас» — пятница, 02.10.2026 15:00 по местному времени (Asia/Tashkent, UTC+5).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from bot.config import get_settings
from bot.utils.dateparse import iso_to_deadline, names_far_year, parse_deadline, quick_deadline_options
from bot.utils.dates import to_utc

NOW_LOCAL = datetime(2026, 10, 2, 15, 0)  # пятница


def local(year: int, month: int, day: int, hour: int = 18, minute: int = 0) -> datetime:
    """Местное время -> naive UTC (как возвращает parse_deadline)."""
    return to_utc(datetime(year, month, day, hour, minute))


PARSED_CASES: list[tuple[str, datetime]] = [
    # относительные дни
    ("сегодня", local(2026, 10, 2)),
    ("завтра", local(2026, 10, 3)),
    ("Завтра", local(2026, 10, 3)),
    ("послезавтра", local(2026, 10, 4)),
    ("завтра в 10:00", local(2026, 10, 3, 10)),
    ("завтра 9:30", local(2026, 10, 3, 9, 30)),
    ("завтра в 18.00", local(2026, 10, 3, 18)),
    ("сегодня в 17:00", local(2026, 10, 2, 17)),
    # «через …»
    ("через 3 дня", local(2026, 10, 5)),
    ("через день", local(2026, 10, 3)),
    ("через два дня", local(2026, 10, 4)),
    ("через неделю", local(2026, 10, 9)),
    ("через 2 недели", local(2026, 10, 16)),
    ("через 3 дня в 12:00", local(2026, 10, 5, 12)),
    # дни недели: сегодня пятница -> «пятница» значит следующая
    ("в пятницу", local(2026, 10, 9)),
    ("пятница", local(2026, 10, 9)),
    ("до пятницы", local(2026, 10, 9)),
    ("в понедельник", local(2026, 10, 5)),
    ("среда", local(2026, 10, 7)),
    ("в субботу", local(2026, 10, 3)),
    ("во вторник в 11:00", local(2026, 10, 6, 11)),
    # конец недели (пятница; сегодня пятница и 18:00 ещё не наступило) и месяца
    ("конец недели", local(2026, 10, 2)),
    ("конец месяца", local(2026, 10, 31)),
    # числовые даты
    ("05.10", local(2026, 10, 5)),
    ("5.10", local(2026, 10, 5)),
    ("05.10.2026", local(2026, 10, 5)),
    ("05.10.26", local(2026, 10, 5)),
    ("2026-10-05", local(2026, 10, 5)),
    ("05.10 18:00", local(2026, 10, 5)),
    ("05.10 9.30", local(2026, 10, 5, 9, 30)),
    ("05.10.2026 в 12:00", local(2026, 10, 5, 12)),
    ("02.10", local(2026, 10, 2)),  # сегодня, 18:00 ещё впереди
    # даты словами
    ("5 октября", local(2026, 10, 5)),
    ("5 окт", local(2026, 10, 5)),
    ("5 октября 2026", local(2026, 10, 5)),
    ("5 октября 18:00", local(2026, 10, 5)),
    ("5 октября в 10:30", local(2026, 10, 5, 10, 30)),
    ("к 5 октября", local(2026, 10, 5)),
    ("Срок: 07.10", local(2026, 10, 7)),
    # без года и дата уже прошла -> следующий год
    ("15.01", local(2027, 1, 15)),
    ("1 января", local(2027, 1, 1)),
    ("1 октября", local(2027, 10, 1)),
    ("1.10", local(2027, 10, 1)),
]


@pytest.mark.parametrize(("text", "expected"), PARSED_CASES)
def test_parse_deadline(text: str, expected: datetime) -> None:
    assert parse_deadline(text, NOW_LOCAL) == expected


@pytest.mark.parametrize(
    "text",
    [
        # в прошлом
        "01.10.2026",
        "05.10.2020",
        "сегодня в 10:00",
        "02.10 в 14:00",
        "2026-09-30",
        # мусор и невозможные даты
        "",
        "   ",
        "абракадабра",
        "вчера",
        "проверить 100 договоров",
        "31.02.2027",
        "32 октября",
        "13.13",
        "завтра в 25:00",
        "5 абвгд",
    ],
)
def test_parse_deadline_rejects(text: str) -> None:
    assert parse_deadline(text, NOW_LOCAL) is None


def test_parse_deadline_returns_naive_utc() -> None:
    result = parse_deadline("05.10", NOW_LOCAL)
    assert result is not None and result.tzinfo is None
    assert result == datetime(2026, 10, 5, 13, 0)  # 18:00 Ташкента = 13:00 UTC


def test_parse_deadline_accepts_aware_now() -> None:
    aware_now = NOW_LOCAL.replace(tzinfo=get_settings().tz)
    assert parse_deadline("завтра", aware_now) == parse_deadline("завтра", NOW_LOCAL)


def test_parse_deadline_without_now_uses_current_time() -> None:
    assert parse_deadline("через неделю") is not None


def test_parse_deadline_respects_default_time(set_env) -> None:
    set_env(DEFAULT_DEADLINE_TIME="17:30")
    assert parse_deadline("05.10", NOW_LOCAL) == local(2026, 10, 5, 17, 30)


# --- quick_deadline_options ------------------------------------------------------------------------


def _dates(options: list[tuple[str, str]]) -> list[date]:
    return [date.fromisoformat(iso) for _, iso in options]


def test_quick_options_friday_afternoon() -> None:
    options = quick_deadline_options(NOW_LOCAL)
    days = _dates(options)
    assert len(days) == len(set(days)), "даты не должны повторяться"
    assert all(day >= NOW_LOCAL.date() for day in days)
    assert options[0][0].startswith("Сегодня") and days[0] == date(2026, 10, 2)
    assert options[1][0].startswith("Завтра") and days[1] == date(2026, 10, 3)
    labels = [label for label, _ in options]
    assert any(label.startswith("Пятница") for label in labels)
    assert any(label.startswith("Конец месяца") for label in labels)
    assert date(2026, 10, 9) in days  # ближайшая будущая пятница (= через неделю, без дубля)
    assert date(2026, 10, 31) in days


def test_quick_options_no_today_after_default_time() -> None:
    options = quick_deadline_options(datetime(2026, 10, 2, 19, 0))
    assert not options[0][0].startswith("Сегодня")
    assert _dates(options)[0] == date(2026, 10, 3)


def test_quick_options_end_of_month_rolls_over() -> None:
    options = quick_deadline_options(datetime(2026, 10, 31, 19, 0))
    days = _dates(options)
    assert date(2026, 11, 30) in days
    assert all(day > date(2026, 10, 31) for day in days)


def test_quick_options_labels_are_short() -> None:
    for label, iso in quick_deadline_options(NOW_LOCAL):
        assert label and len(label) <= 32
        assert len(iso) == 10


# --- iso_to_deadline -------------------------------------------------------------------------------


def test_iso_to_deadline_uses_default_time() -> None:
    assert iso_to_deadline("2026-10-05") == datetime(2026, 10, 5, 13, 0)


def test_iso_to_deadline_invalid() -> None:
    with pytest.raises(ValueError):
        iso_to_deadline("2026-13-40")


# --- Опечатка в годе ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "far"),
    [
        ("31.12.9999", True),
        ("5 октября 2099", True),
        ("05.10.2032 18:00", True),   # 2026 + 5 = 2031 — дальше уже опечатка
        ("05.10.2031", False),
        ("05.10.2026", False),
        ("завтра в 18:00", False),
        ("", False),
    ],
)
def test_names_far_year(text: str, far: bool) -> None:
    """parse_deadline не принимает срок дальше 5 лет; names_far_year подсказывает, что дело в годе."""
    assert names_far_year(text, NOW_LOCAL) is far
    if far:
        assert parse_deadline(text, NOW_LOCAL) is None


# --- Узбекский (SPEC.md §14) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ertaga", local(2026, 10, 3)),
        ("Ertaga soat 15:00 da", local(2026, 10, 3, 15)),
        ("bugun 17:00 gacha", local(2026, 10, 2, 17)),
        ("indinga", local(2026, 10, 4)),
        ("ertaga ertalab soat 9 da", local(2026, 10, 3, 9)),
        ("ertaga kechqurun soat 6 da", local(2026, 10, 3, 18)),
        # дни недели: сегодня пятница -> «juma» значит следующая
        ("juma kuni", local(2026, 10, 9)),
        ("jumagacha", local(2026, 10, 9)),
        ("dushanba", local(2026, 10, 5)),
        ("chorshanba kuni soat 11:00 da", local(2026, 10, 7, 11)),
        ("kelasi seshanba", local(2026, 10, 6)),
        ("shanba", local(2026, 10, 3)),
        # «через …»
        ("3 kundan keyin", local(2026, 10, 5)),
        ("bir haftadan keyin", local(2026, 10, 9)),
        ("ikki haftadan soʻng", local(2026, 10, 16)),
        ("2 oydan keyin", local(2026, 12, 2)),
        ("3 kun ichida", local(2026, 10, 5)),
        # конец месяца и года
        ("oy oxirigacha", local(2026, 10, 31)),
        ("oy oxiri", local(2026, 10, 31)),
        ("yil oxirigacha", local(2026, 12, 31)),
        # даты: «5-oktabr», с годом и временем
        ("5-oktabr", local(2026, 10, 5)),
        ("5 oktabr", local(2026, 10, 5)),
        ("5-oktabrgacha", local(2026, 10, 5)),
        ("25-oktyabr soat 14:30", local(2026, 10, 25, 14, 30)),
        ("2026-yil 15-dekabr", local(2026, 12, 15)),
        ("15-dekabr 2026-yil", local(2026, 12, 15)),
        ("05.10 gacha", local(2026, 10, 5)),
        ("05.10.2026 15:00 gacha", local(2026, 10, 5, 15)),
        # узбекская кириллица и разные апострофы
        ("эртага", local(2026, 10, 3)),
        ("жума куни", local(2026, 10, 9)),
        ("3 кундан кейин", local(2026, 10, 5)),
        ("ikki haftadan so'ng", local(2026, 10, 16)),
    ],
)
def test_uzbek_deadlines(text: str, expected: datetime) -> None:
    """Срок по-узбекски понимается так же, как по-русски: «ertaga», «juma kuni», «5-oktabr», «3 kundan keyin»."""
    assert parse_deadline(text, NOW_LOCAL) == expected


@pytest.mark.parametrize("text", ["qachondir", "keyinroq", "soat", "40-oktabr", "bugun 10:00 gacha"])
def test_uzbek_unclear_or_past_deadline_is_none(text: str) -> None:
    assert parse_deadline(text, NOW_LOCAL) is None
