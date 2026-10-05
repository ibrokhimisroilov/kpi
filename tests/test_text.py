"""bot.utils.text: экранирование, обрезка, числа, проценты, склонения, полоса прогресса."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from bot.utils.text import bar, esc, fmt_num, fmt_pct, parse_number, parse_percent, plural, truncate


class _TagBalance(HTMLParser):
    """Проверка, что HTML-фрагмент сбалансирован (каждый открытый тег закрыт)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.ok = True

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.ok = False


def _balanced(fragment: str) -> bool:
    parser = _TagBalance()
    parser.feed(fragment)
    parser.close()
    return parser.ok and not parser.stack


# --- esc -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('<b>Отчёт & "план"</b>', "&lt;b&gt;Отчёт &amp; &quot;план&quot;&lt;/b&gt;"),
        ("обычный текст", "обычный текст"),
        (None, ""),
        (42, "42"),
        ("'", "&#x27;"),
    ],
)
def test_esc(value: object, expected: str) -> None:
    assert esc(value) == expected


# --- truncate --------------------------------------------------------------------------------------


def test_truncate_short_text_unchanged() -> None:
    assert truncate("коротко", 100) == "коротко"
    assert truncate("ровно", 5) == "ровно"


def test_truncate_long_text_gets_ellipsis() -> None:
    result = truncate("а" * 5000)
    assert len(result) <= 4000
    assert result.endswith("…")


@pytest.mark.parametrize("limit", [5, 10, 17, 25, 40, 60])
def test_truncate_keeps_html_valid(limit: int) -> None:
    text = "<b>Жирный &amp; длинный текст</b> и <i>курсив с &lt;тегом&gt;</i> " * 3
    result = truncate(text, limit)
    assert len(result) <= limit
    assert _balanced(result)
    # Обрывков сущностей («&am…») не остаётся.
    assert "&am…" not in result and "&l…" not in result


def test_truncate_closes_open_tags() -> None:
    result = truncate("<b>" + "x" * 100 + "</b>", 20)
    assert result.endswith("</b>")
    assert "…" in result
    assert _balanced(result)


# --- fmt_pct / fmt_num -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (101.5, "102 %"),  # пример из ТЗ: округление половины вверх
        (100, "100 %"),
        (99.4, "99 %"),
        (2.5, "3 %"),
        (0.5, "1 %"),
        (0, "0 %"),
        (None, "—"),
    ],
)
def test_fmt_pct(value: float | None, expected: str) -> None:
    assert fmt_pct(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(100.0, "100"), (2.5, "2,5"), (1.234, "1,23"), (0, "0"), (1200, "1200"), (None, "—")],
)
def test_fmt_num(value: float | None, expected: str) -> None:
    assert fmt_num(value) == expected


# --- plural ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0 задач"),
        (1, "1 задача"),
        (2, "2 задачи"),
        (4, "4 задачи"),
        (5, "5 задач"),
        (11, "11 задач"),
        (14, "14 задач"),
        (21, "21 задача"),
        (22, "22 задачи"),
        (111, "111 задач"),
        (101, "101 задача"),
    ],
)
def test_plural(n: int, expected: str) -> None:
    assert plural(n, "задача", "задачи", "задач") == expected


# --- parse_number / parse_percent ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("110", 110.0),
        ("110,5", 110.5),
        ("110.5", 110.5),
        ("1 200", 1200.0),
        ("110 договоров", 110.0),
        ("проверено 95 актов", 95.0),
        ("нет числа", None),
        ("", None),
    ],
)
def test_parse_number(text: str, expected: float | None) -> None:
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("95", 95.0),
        ("95%", 95.0),
        ("95 %", 95.0),
        (" 110 ", 110.0),
        ("95,5 %", 95.5),
        ("девяносто", None),
        ("", None),
        ("95 и 100", None),
    ],
)
def test_parse_percent(text: str, expected: float | None) -> None:
    assert parse_percent(text) == expected


# --- bar -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pct", "expected"),
    [
        (50, "▰▰▰▰▰▱▱▱▱▱"),
        (0, "▱" * 10),
        (None, "▱" * 10),
        (100, "▰" * 10),
        (150, "▰" * 10),  # больше 100 % — полная полоса, без выхода за ширину
        (-20, "▱" * 10),
        (float("nan"), "▱" * 10),
        (float("inf"), "▰" * 10),
        (float("-inf"), "▱" * 10),
    ],
)
def test_bar(pct: float | None, expected: str) -> None:
    assert bar(pct) == expected


def test_bar_width() -> None:
    assert len(bar(73, width=8)) == 8
    assert bar(50, width=4) == "▰▰▱▱"
