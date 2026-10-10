"""Разбор сроков, введённых текстом по-русски.

Понимает «сегодня», «завтра», «послезавтра», «через 3 дня», «через неделю», «в пятницу», «до пятницы»,
«конец недели», «конец месяца», «05.10», «05.10.2026», «2026-10-05», «5 октября», «5 окт.»,
а также любое из этого со временем: «18:00», «в 18:00», «в 18.00», «05.10 18.00», «в 6 вечера».
Результат — naive UTC; без времени берётся settings.default_deadline_time.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from bot.config import get_settings
from bot.utils.dates import deadline_from_local_date, to_utc

__all__ = ["parse_deadline", "quick_deadline_options", "iso_to_deadline", "names_far_year", "FAR_YEAR_HINT"]

FRIDAY = 4
# Срок дальше этого — почти наверняка опечатка в годе («31.12.9999», «5.10.2206»): не принимаем.
MAX_YEARS_AHEAD = 5
FAR_YEAR_HINT = f"Срок слишком далеко — проверьте год (не дальше {MAX_YEARS_AHEAD} лет вперёд)."
_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")

# --- Словари ---------------------------------------------------------------------------------

_MONTH_RES = tuple(
    re.compile(pattern)
    for pattern in (
        r"январ\w*|янв",
        r"феврал\w*|февр?",
        r"март\w*|мар",
        r"апрел\w*|апр",
        r"ма[йяе]",
        r"июн\w*",
        r"июл\w*",
        r"август\w*|авг",
        r"сентябр\w*|сент?",
        r"октябр\w*|окт",
        r"ноябр\w*|нояб?",
        r"декабр\w*|дек",
    )
)

_WEEKDAY_RES = tuple(
    re.compile(pattern)
    for pattern in (
        r"понедельник[аеу]?|пн",
        r"вторник[аеу]?|вт",
        r"сред[аеуы]|ср",
        r"четверг[аеу]?|чт",
        r"пятниц[аеуы]|пт",
        r"суббот[аеуы]|сб",
        r"воскресень[еяю]|вс",
    )
)

_RELATIVE_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}

_NUMBER_WORDS = {
    "один": 1, "одну": 1, "одна": 1, "одного": 1,
    "два": 2, "две": 2, "пару": 2, "пара": 2,
    "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10,
}

# Служебные слова, которые не влияют на дату: «до пятницы», «к 5 октября», «на завтра», «срок: …».
_FILLERS = frozenset({"до", "к", "ко", "в", "во", "на", "по", "срок", "срок:", "дедлайн", "deadline"})

# --- Время -----------------------------------------------------------------------------------

_DAYPART = r"(?:\s+(утра|дня|вечера|ночи))?"
_HOUR_WORD = r"(?:ч|час|часа|часов|часам)\b\.?"
_TIME_PATTERNS = (
    # «18:00», «в 18:00», «до 9:30 утра»
    re.compile(r"(?:\b(?:в|во|к|до)\s+)?(?<!\d)(\d{1,2}):(\d{2})(?!\d)" + _DAYPART),
    # «в 18.00» — с предлогом «в» это однозначно время
    re.compile(r"\b(?:в|во)\s+(\d{1,2})\.(\d{2})(?![\d.])" + _DAYPART),
    # «в 18 часов», «к 10 ч»
    re.compile(r"\b(?:в|во|к|до)\s+(\d{1,2})()\s*" + _HOUR_WORD + _DAYPART),
    # «в 6 вечера»
    re.compile(r"\b(?:в|во|к|до)\s+(\d{1,2})()" + r"\s+(утра|дня|вечера|ночи)\b"),
)
# «18.00» без предлога — время, только если рядом есть дата («05.10 18.00», «завтра 18.00»).
_DOTTED_RE = re.compile(r"(?<![\d.:])(\d{1,2})\.(\d{2})(?![\d.:])")

# --- Даты ------------------------------------------------------------------------------------

_HOURS_RE = re.compile(r"через\s+(?:(\d{1,3}|[а-я]+)\s+)?(?:час|часа|часов)")
_AFTER_RE = re.compile(r"через\s+(?:(\d{1,3}|[а-я]+)\s+)?(день|дня|дней|сутки|суток|недел\w+|месяц\w*)")
_WEEKDAY_EXPR_RE = re.compile(r"(?:(следующ\w*|эт\w+|ближайш\w*)\s+)?([а-я]+)")
_END_RE = re.compile(r"(?:конец|конца|концу|конце)\s+(?:эт\w+\s+)?(недели|месяца|года)")
_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_NUMERIC_RE = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?")
_TEXT_DATE_RE = re.compile(r"(\d{1,2})(?:-?(?:го|е|ое))?\s+([а-я]+)\.?(?:\s+(\d{4}|\d{2}))?")
_DAY_OF_MONTH_RE = re.compile(r"(\d{1,2})(?:\s*-?\s*(?:го|е|ое))?\s+числа|(\d{1,2})\s*-?\s*го")
_YEAR_SUFFIX_RE = re.compile(r"(\d{4})\s*г(?:ода|\.)?(?=\s|$)")


# --- Узбекский (SPEC.md §14): фраза приводится к русской, дальше — общий разбор ----------------------

_APOSTROPHES = str.maketrans({char: "'" for char in "ʻʼ‘’`´"})
# Узбекская кириллица -> латиница, только слова о сроках.
_UZ_CYRILLIC = {
    "бугун": "bugun", "эртага": "ertaga", "индинга": "indinga",
    "душанба": "dushanba", "сешанба": "seshanba", "чоршанба": "chorshanba", "пайшанба": "payshanba",
    "жума": "juma", "шанба": "shanba", "якшанба": "yakshanba",
    "кундан": "kundan", "ҳафтадан": "haftadan", "хафтадан": "haftadan", "ойдан": "oydan", "соатдан": "soatdan",
    "кейин": "keyin", "сўнг": "so'ng", "охири": "oxiri", "охиригача": "oxirigacha", "ой": "oy", "ҳафта": "hafta",
    "хафта": "hafta", "йил": "yil", "куни": "kuni", "соат": "soat", "гача": "gacha", "келаси": "kelasi",
    "кейинги": "keyingi", "бир": "bir", "икки": "ikki", "уч": "uch",
}
_UZ_HINT_RE = re.compile(
    r"bugun|erta|indin|shanba|juma|keyin|so'ng|oxir|gacha|kuni|soat|yil|ichida|"
    r"yanvar|fevral|mart|aprel|may|iyun|iyul|avgust|sent[ay]|okt[ay]|noyabr|dekabr"
)
_UZ_NUMBERS = {
    "bir": "1", "ikki": "2", "uch": "3", "to'rt": "4", "besh": "5", "olti": "6", "yetti": "7",
    "sakkiz": "8", "to'qqiz": "9", "o'n": "10",
}
_UZ_WEEKDAYS = {
    "dushanba": "понедельник", "seshanba": "вторник", "chorshanba": "среда", "payshanba": "четверг",
    "juma": "пятница", "shanba": "суббота", "yakshanba": "воскресенье",
}
_UZ_MONTHS = {
    "yanvar": "января", "fevral": "февраля", "mart": "марта", "aprel": "апреля", "may": "мая", "iyun": "июня",
    "iyul": "июля", "avgust": "августа", "sentabr": "сентября", "sentyabr": "сентября", "oktabr": "октября",
    "oktyabr": "октября", "noyabr": "ноября", "dekabr": "декабря",
}
_UZ_UNITS = {"kun": "дней", "hafta": "недель", "oy": "месяц", "soat": "часов"}
_UZ_DAYPARTS = {"ertalab": "утра", "kechqurun": "вечера", "kechki": "вечера", "kechasi": "ночи", "tushdan keyin": "дня"}
_UZ_MONTH = "|".join(sorted(_UZ_MONTHS, key=len, reverse=True))
_UZ_CASE = r"(?:gacha|ga|da|ning|ni)?"  # падежные окончания: «jumagacha», «oktabrda»
_UZ_YEAR_FIRST_RE = re.compile(rf"(\d{{4}})\s*-?\s*yil(?:i|ning|da)?\s+(\d{{1,2}})\s*-?\s*({_UZ_MONTH}){_UZ_CASE}\b")
_UZ_DATE_RE = re.compile(rf"(\d{{1,2}})\s*-?\s*({_UZ_MONTH}){_UZ_CASE}\b(?:\s+(\d{{4}})\s*-?\s*yil\w*)?")
_UZ_AFTER_RE = re.compile(
    r"(\d{1,3}|bir|ikki|uch|to'rt|besh|olti|yetti|sakkiz|to'qqiz|o'n)\s+(kun|hafta|oy|soat)(?:dan\s+(?:keyin|so'ng)|\s+ichida)"
)
_UZ_END_RE = re.compile(r"\b(oy|hafta|yil)(?:ning)?\s+oxiri" + _UZ_CASE + r"\b")
_UZ_WEEKDAY_RE = re.compile(
    r"(?:\b(kelasi|keyingi|shu)\s+)?\b(dushanba|seshanba|chorshanba|payshanba|yakshanba|shanba|juma)" + _UZ_CASE + r"\b"
)
_UZ_HOUR_RE = re.compile(r"\bsoat\s+(\d{1,2})(?::(\d{2}))?(?:\s*(?:da|gacha|ga)\b)?")
_UZ_DAYPART_RE = re.compile(r"\b(ertalab|kechqurun|kechki|kechasi|tushdan keyin)\s+(в \d{1,2}:\d{2})")
_UZ_CLOCK_SUFFIX_RE = re.compile(r"(\d{1,2}:\d{2})\s*(?:da|gacha|ga)\b")
_UZ_FILLER_RE = re.compile(r"\b(?:kuni(?:gacha|ga)?|muddati?|sanasi|gacha|kechi bilan)\b")
_UZ_GLUED_GACHA_RE = re.compile(r"(?<=[\d.])gacha\b")


def _from_uzbek(text: str) -> str:
    """«ertaga soat 15:00 da», «juma kuni», «5-oktabr», «3 kundan keyin», «oy oxirigacha» — в русские
    слова, которые понимает общий разбор. Текст без узбекских слов возвращается как есть."""
    text = text.translate(_APOSTROPHES)
    text = " ".join(_UZ_CYRILLIC.get(word, word) for word in text.split())
    if not _UZ_HINT_RE.search(text):
        return text
    text = _UZ_YEAR_FIRST_RE.sub(lambda m: f"{m.group(2)} {_UZ_MONTHS[m.group(3)]} {m.group(1)}", text)
    text = _UZ_DATE_RE.sub(lambda m: f"{m.group(1)} {_UZ_MONTHS[m.group(2)]}" + (f" {m.group(3)}" if m.group(3) else ""), text)
    text = _UZ_AFTER_RE.sub(lambda m: f"через {_UZ_NUMBERS.get(m.group(1), m.group(1))} {_UZ_UNITS[m.group(2)]}", text)
    text = _UZ_END_RE.sub(lambda m: "конец " + {"oy": "месяца", "hafta": "недели", "yil": "года"}[m.group(1)], text)
    text = re.sub(r"\bertagacha\b|\bertaga\b", "завтра", text)
    text = re.sub(r"\bbugun(?:gacha)?\b", "сегодня", text)
    text = re.sub(r"\bindin(?:ga)?\b", "послезавтра", text)
    text = _UZ_WEEKDAY_RE.sub(
        lambda m: ({"kelasi": "следующая ", "keyingi": "следующая ", "shu": "эта "}.get(m.group(1) or "", ""))
        + _UZ_WEEKDAYS[m.group(2)],
        text,
    )
    text = _UZ_HOUR_RE.sub(lambda m: f"в {m.group(1)}:{m.group(2) or '00'}", text)
    text = _UZ_DAYPART_RE.sub(lambda m: f"{m.group(2)} {_UZ_DAYPARTS[m.group(1)]}", text)
    text = _UZ_CLOCK_SUFFIX_RE.sub(r"\1", text)
    text = _UZ_GLUED_GACHA_RE.sub("", text)
    text = _UZ_FILLER_RE.sub(" ", text)
    return " ".join(text.split())


# --- Общие хелперы ---------------------------------------------------------------------------


def _local_naive(now_local: datetime | None) -> datetime:
    """Текущее местное время без tzinfo (aware переводится в settings.timezone)."""
    tz = get_settings().tz
    if now_local is None:
        return datetime.now(tz).replace(tzinfo=None)
    if now_local.tzinfo is not None:
        return now_local.astimezone(tz).replace(tzinfo=None)
    return now_local


def _default_time() -> time:
    hh, mm = get_settings().default_deadline_time.split(":")
    return time(int(hh), int(mm))


def _normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[,;«»\"!?]", " ", text)
    text = _from_uzbek(text)
    text = re.sub(r"не\s+(?:позднее|позже)", " ", text)
    text = _YEAR_SUFFIX_RE.sub(r"\1", text)
    return " ".join(text.split())


def _strip_fillers(text: str) -> str:
    words = [word for word in text.split() if word not in _FILLERS]
    return " ".join(words).strip(" .")


def _count(token: str | None) -> int | None:
    """Количество в «через N …»: нет числа -> 1, «две» -> 2, непонятное слово -> None."""
    if token is None:
        return 1
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _month_number(word: str) -> int | None:
    word = word.rstrip(".")
    for number, pattern in enumerate(_MONTH_RES, start=1):
        if pattern.fullmatch(word):
            return number
    return None


def _weekday_number(word: str) -> int | None:
    for number, pattern in enumerate(_WEEKDAY_RES):
        if pattern.fullmatch(word):
            return number
    return None


def _last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _next_month(day: date) -> tuple[int, int]:
    return (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _nearest_date(day: int, month: int, today: date) -> date | None:
    """Дата без года: в этом году, а если уже прошла — в следующем."""
    for year in (today.year, today.year + 1):
        candidate = _safe_date(year, month, day)
        if candidate is not None and candidate >= today:
            return candidate
    return None


def _full_year(raw: str) -> int:
    year = int(raw)
    return year + 2000 if year < 100 else year


# --- Время -----------------------------------------------------------------------------------


def _make_time(hour: int, minute: int, daypart: str | None) -> time:
    """Собирает время с учётом «утра/дня/вечера/ночи»; недопустимое -> ValueError."""
    if daypart in ("дня", "вечера") and hour < 12:
        hour += 12
    elif daypart in ("утра", "ночи") and hour == 12:
        hour = 0
    return time(hour, minute)


def _extract_time(text: str) -> tuple[time | None, str]:
    """Находит явное время и вырезает его из текста. Недопустимое время -> ValueError."""
    for pattern in _TIME_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        hour, minute, daypart = match.groups()
        at = _make_time(int(hour), int(minute or 0), daypart)
        rest = f"{text[: match.start()]} {text[match.end():]}"
        return at, " ".join(rest.split())
    return None, text


# --- Даты: каждый разборщик получает fullmatch и возвращает дату или None ---------------------


def _after(match: re.Match[str], today: date, at: time, now: datetime) -> date | None:
    count = _count(match.group(1))
    if count is None:
        return None
    unit = match.group(2)
    if unit.startswith("недел"):
        return today + timedelta(weeks=count)
    if unit.startswith("месяц"):
        return _add_months(today, count)
    return today + timedelta(days=count)


def _weekday(match: re.Match[str], today: date, at: time, now: datetime) -> date | None:
    modifier, word = match.groups()
    target = _weekday_number(word)
    if target is None:
        return None
    if modifier and modifier.startswith("следующ"):
        next_monday = today + timedelta(days=7 - today.weekday())
        return next_monday + timedelta(days=target)
    # Ближайший будущий такой день; если сегодня этот день недели — через неделю.
    return today + timedelta(days=(target - today.weekday()) % 7 or 7)


def _end_of(match: re.Match[str], today: date, at: time, now: datetime) -> date | None:
    unit = match.group(1)
    if unit == "недели":
        day = today + timedelta(days=(FRIDAY - today.weekday()) % 7)
        return day if datetime.combine(day, at) > now else day + timedelta(weeks=1)
    if unit == "месяца":
        day = _last_day(today.year, today.month)
        return day if datetime.combine(day, at) > now else _last_day(*_next_month(today))
    day = date(today.year, 12, 31)
    return day if datetime.combine(day, at) > now else date(today.year + 1, 12, 31)


def _iso(match: re.Match[str], today: date, at: time, now: datetime) -> date | None:
    year, month, day = (int(group) for group in match.groups())
    return _safe_date(year, month, day)


def _numeric(match: re.Match[str], today: date, at: time, now: datetime) -> date | None:
    day, month, year = match.groups()
    if year is not None:
        return _safe_date(_full_year(year), int(month), int(day))
    return _nearest_date(int(day), int(month), today)


def _text_date(match: re.Match[str], today: date, at: time, now: datetime) -> date | None:
    day, word, year = match.groups()
    month = _month_number(word)
    if month is None:
        return None
    if year is not None:
        return _safe_date(_full_year(year), month, int(day))
    return _nearest_date(int(day), month, today)


def _day_of_month(match: re.Match[str], today: date, at: time, now: datetime) -> date | None:
    day = int(next(group for group in match.groups() if group))
    for shift in range(13):
        first = _add_months(today.replace(day=1), shift)
        candidate = _safe_date(first.year, first.month, day)
        if candidate is not None and candidate >= today:
            return candidate
    return None


_DateParser = Callable[[re.Match[str], date, time, datetime], date | None]
_DATE_PARSERS: tuple[tuple[re.Pattern[str], _DateParser], ...] = (
    (_AFTER_RE, _after),
    (_END_RE, _end_of),
    (_ISO_RE, _iso),
    (_NUMERIC_RE, _numeric),
    (_TEXT_DATE_RE, _text_date),
    (_DAY_OF_MONTH_RE, _day_of_month),
    (_WEEKDAY_EXPR_RE, _weekday),
)


def _parse_day(text: str, now: datetime, at: time) -> date | None:
    today = now.date()
    if text in _RELATIVE_DAYS:
        return today + timedelta(days=_RELATIVE_DAYS[text])
    for pattern, parser in _DATE_PARSERS:
        match = pattern.fullmatch(text)
        result = parser(match, today, at, now) if match is not None else None
        if result is not None:
            return result
    return None


def _resolve(text: str, at: time | None, now: datetime) -> datetime | None:
    """Текст без явного времени + время -> местное naive datetime или None."""
    text = _strip_fillers(text)
    hours = _HOURS_RE.fullmatch(text)
    if hours is not None:
        count = _count(hours.group(1))
        if at is not None or not count:
            return None
        return (now + timedelta(hours=count)).replace(second=0, microsecond=0)
    if not text:
        # Указано только время («в 18:00») — значит сегодня.
        return datetime.combine(now.date(), at) if at is not None else None
    effective = at or _default_time()
    day = _parse_day(text, now, effective)
    return datetime.combine(day, effective) if day is not None else None


def _resolve_with_dotted_time(text: str, now: datetime) -> datetime | None:
    """«05.10 18.00», «завтра 18.00»: одно из «ЧЧ.ММ» — время, если остаток — дата."""
    for match in reversed(list(_DOTTED_RE.finditer(text))):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            continue
        remainder = f"{text[: match.start()]} {text[match.end():]}"
        if not _strip_fillers(remainder):
            continue
        result = _resolve(remainder, time(hour, minute), now)
        if result is not None:
            return result
    return None


# --- Публичные функции -----------------------------------------------------------------------


def parse_deadline(text: str, now_local: datetime | None = None) -> datetime | None:
    """Срок из текста -> naive UTC. Непонятный текст или срок в прошлом -> None.

    now_local — «сейчас» по местному времени (aware или naive), нужен для тестов.
    """
    now = _local_naive(now_local)
    normalized = _normalize(text or "")
    if not normalized:
        return None
    try:
        at, rest = _extract_time(normalized)
    except ValueError:
        return None
    result = _resolve(rest, at, now)
    if result is None and at is None:
        result = _resolve_with_dotted_time(rest, now)
    if result is None or result <= now or result.year > now.year + MAX_YEARS_AHEAD:
        return None
    return to_utc(result)


def names_far_year(text: str, now_local: datetime | None = None) -> bool:
    """В тексте есть год дальше MAX_YEARS_AHEAD («31.12.9999»).

    parse_deadline вернёт для такого текста None — пользователю лучше сказать «проверьте год»,
    а не «не понял срок».
    """
    limit = _local_naive(now_local).year + MAX_YEARS_AHEAD
    return any(int(year) > limit for year in _YEAR_RE.findall(text or ""))


def quick_deadline_options(now_local: datetime | None = None) -> list[tuple[str, str]]:
    """Варианты для кнопок: [(подпись, ISO-дата)] без повторов дат.

    Сегодня (если время по умолчанию ещё не прошло), Завтра, Пятница, Через неделю, Конец месяца.
    """
    now = _local_naive(now_local)
    today, at = now.date(), _default_time()
    end_of_month = _last_day(today.year, today.month)
    if datetime.combine(end_of_month, at) <= now:
        end_of_month = _last_day(*_next_month(today))
    candidates: list[tuple[str, date]] = []
    if datetime.combine(today, at) > now:
        candidates.append(("Сегодня", today))
    candidates += [
        ("Завтра", today + timedelta(days=1)),
        ("Пятница", today + timedelta(days=(FRIDAY - today.weekday()) % 7 or 7)),
        ("Через неделю", today + timedelta(weeks=1)),
        ("Конец месяца", end_of_month),
    ]
    options: list[tuple[str, str]] = []
    seen: set[date] = set()
    for label, day in candidates:
        if day in seen:
            continue
        seen.add(day)
        options.append((f"{label}, {day:%d.%m}", day.isoformat()))
    return options


def iso_to_deadline(iso_date: str) -> datetime:
    """«2026-10-05» -> naive UTC с временем settings.default_deadline_time (ValueError при ошибке)."""
    return deadline_from_local_date(date.fromisoformat(iso_date))
