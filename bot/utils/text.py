"""Текстовые хелперы: экранирование HTML, обрезка, числа, проценты, склонения."""

from __future__ import annotations

import html
import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from bot import i18n

__all__ = ["esc", "truncate", "fmt_pct", "fmt_num", "plural", "parse_number", "parse_percent", "bar"]

_ELLIPSIS = "…"

# Незакрытый тег или HTML-сущность в конце обрезанной строки.
_PARTIAL_TAG_RE = re.compile(r"<[^>]*$")
_PARTIAL_ENTITY_RE = re.compile(r"&[#\w]*$")
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)[^>]*>")

# Число: «110», «110,5», «1 200», «1 200,5», «-3»; разделитель тысяч — пробел (в т.ч. неразрывный).
_NUMBER_RE = re.compile(
    r"(?<![\w.,])([-−])?(\d{1,3}(?:[   ]\d{3})+(?!\d)|\d+)(?:[.,](\d+))?"
)
_PERCENT_RE = re.compile(r"\s*([-−]?\d+(?:[.,]\d+)?)\s*(?:%|проц\w*)?\s*", re.IGNORECASE)


def esc(text: object) -> str:
    """Экранирует текст для HTML-разметки Telegram. None -> пустая строка.

    Это слова пользователя (название задачи, ФИО, комментарий): они окружаются невидимыми метками
    (bot.i18n.mark), чтобы перевод интерфейса на другой язык их не трогал. Метки убираются при отправке.
    """
    if text is None:
        return ""
    return i18n.mark(html.escape(str(text)))


def own(text: object) -> str:
    """Слова пользователя там, где HTML нет (подписи кнопок): те же метки, что у esc(), без экранирования."""
    if text is None:
        return ""
    return i18n.mark(str(text))


def _closing_tags(fragment: str) -> str:
    """Закрывающие теги для тегов, оставшихся открытыми во фрагменте HTML."""
    stack: list[str] = []
    for match in _TAG_RE.finditer(fragment):
        closing, name = match.group(1), match.group(2).lower()
        if not closing:
            stack.append(name)
        elif name in stack:
            # Снимаем всё до последнего такого же открытого тега.
            del stack[len(stack) - 1 - stack[::-1].index(name):]
    return "".join(f"</{name}>" for name in reversed(stack))


def truncate(text: str, limit: int = 4000) -> str:
    """Обрезает текст до limit символов, добавляя «…».

    Безопасна для HTML: не оставляет обрывков тегов и сущностей и закрывает открытые теги,
    чтобы Telegram смог разобрать разметку.
    """
    if len(text) <= limit:
        return text
    if limit < 1:
        return ""
    cut = text[: limit - 1]
    while True:
        cut = _PARTIAL_ENTITY_RE.sub("", _PARTIAL_TAG_RE.sub("", cut)).rstrip()
        closers = _closing_tags(cut)
        overflow = len(cut) + len(_ELLIPSIS) + len(closers) - limit
        if overflow <= 0 or not cut:
            return cut + _ELLIPSIS + closers
        cut = cut[: len(cut) - overflow]


def _round_half_up(value: float) -> int:
    """Округление «половина вверх» (101.5 -> 102), без банковского округления."""
    try:
        return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return round(value)


def fmt_pct(value: float | None) -> str:
    """101.5 -> «102 %»; None -> «—»."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    return f"{_round_half_up(value)} %"


def fmt_num(value: float | None) -> str:
    """100.0 -> «100», 2.5 -> «2,5», 1.234 -> «1,23»; None -> «—»."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    rounded = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = format(rounded.normalize(), "f")
    if text in ("-0", "-0.0"):
        text = "0"
    return text.replace(".", ",")


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(5, "задача", "задачи", "задач") -> «5 задач»."""
    tail = abs(n) % 100
    if 11 <= tail <= 14:
        form = many
    elif tail % 10 == 1:
        form = one
    elif 2 <= tail % 10 <= 4:
        form = few
    else:
        form = many
    return f"{n} {form}"


def parse_number(text: str) -> float | None:
    """Первое число в тексте: «110», «110,5», «1 200», «110 договоров» -> число; нет числа -> None."""
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    sign, whole, fraction = match.groups()
    digits = re.sub(r"\D", "", whole)
    number = float(f"{digits}.{fraction}" if fraction else digits)
    return -number if sign else number


def parse_percent(text: str) -> float | None:
    """«95», «95%», «95 %», «95,5 %» -> 95.0; всё остальное -> None."""
    if not text:
        return None
    match = _PERCENT_RE.fullmatch(text)
    if match is None:
        return None
    return float(match.group(1).replace(",", ".").replace("−", "-"))


def bar(pct: float | None, width: int = 10) -> str:
    """Полоса прогресса «▰▰▰▰▰▱▱▱▱▱»; больше 100 % — полная полоса, None и NaN — пустая."""
    if width <= 0:
        return ""
    if pct is None or math.isnan(pct):
        filled = 0
    else:
        # Сначала ограничиваем долю 0..1, чтобы и бесконечность давала полную полосу, а не ошибку.
        filled = _round_half_up(min(max(pct / 100, 0.0), 1.0) * width)
    return "▰" * filled + "▱" * (width - filled)
