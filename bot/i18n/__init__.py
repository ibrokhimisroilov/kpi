"""Два языка интерфейса: русский (исходный) и узбекский на латинице (SPEC.md §14).

Тексты бота написаны по-русски прямо в коде. Пользователю с языком «uz» готовый текст переводится **на выходе**:
перед отправкой в Telegram (``bot.i18n.telegram``) и в ответах приложения (``bot.webapp``) — функцией ``tr``
по каталогу ``bot/i18n/catalog_uz.py``. Диалогам и сервисам о языке знать не нужно, а уведомление другому
человеку само выходит на его языке: язык определяется по получателю, а не по тому, кто нажал кнопку.

Как переводится текст (``tr``): построчно. Из строки вынимается то, что переводить нельзя или незачем:

* **слова пользователя** — названия задач, ФИО, комментарии. ``bot.utils.text.esc`` окружает их невидимыми
  метками ``U0`` … ``U1``; в ключе каталога на их месте ``{u}``. Тексты задач не переводятся никогда;
* **слова-классы** — месяцы, дни недели, слова после числа («дня», «задач»): в ключе ``{w}``, перевод слова — из
  ``catalog_uz.WORDS``. Так «5 октября (вс)» и «6 ноября (пн)» — одна строка каталога;
* **числа** — в ключе ``{}``.

Получившийся «скелет» ищется в ``catalog_uz.LINES``; значение — узбекская строка с теми же заполнителями
(``{u}``, ``{w}``, ``{}`` по порядку или с номером: ``{u1}``, ``{1}`` — если порядок слов другой). Строки нет —
пробуем перевести её части между « · »; чего в каталоге нет, остаётся по-русски (и попадает в ``misses`` —
так находят непереведённое). Метки убираются всегда, для любого языка (``strip_marks``).

Язык человека — ``users.lang``; для отправки по ``chat_id`` он помнится в памяти процесса (``remember`` /
``lang_of``), язык текущего запроса — ``current`` (``use_lang``).
"""

from __future__ import annotations

import atexit
import html
import json
import logging
import os
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = [
    "LANGS",
    "LANG_NAMES",
    "RU",
    "UZ",
    "U0",
    "U1",
    "current",
    "from_telegram",
    "lang_of",
    "mark",
    "sentence_break",
    "menu_source",
    "misses",
    "normalize",
    "remember",
    "strip_marks",
    "tr",
    "use_lang",
    "variants",
]

log = logging.getLogger(__name__)

RU = "ru"
UZ = "uz"
LANGS = (RU, UZ)
LANG_NAMES = {RU: "Русский", UZ: "Oʻzbekcha"}

# Невидимые метки вокруг слов пользователя (INVISIBLE TIMES / INVISIBLE SEPARATOR): если метка всё же
# дойдёт до экрана, её не видно.
U0 = "\u2062"
U1 = "\u2063"
# Невидимая граница предложений в тексте самого бота (INVISIBLE PLUS): длинная строка из нескольких
# самостоятельных предложений переводится по предложениям (``sentence_break``).
SB = "\u2064"
_MARKS = {ord(U0): None, ord(U1): None, ord(SB): None}

_current: ContextVar[str] = ContextVar("i18n_lang", default=RU)
_lang_by_tg: dict[int, str] = {}

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_USER_RE = re.compile(f"{U0}([^{U0}{U1}]*){U1}")
# Число — одно целое: «95», «95,5», «1 200» (разряды через пробел). Даты и время («04.10», «18:20») — по частям.
_DIGITS_RE = re.compile(r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:,\d+)?|\d+(?:,\d+)?")
_SLOT_RE = re.compile(r"\{(u|w|)(\d*)\}")
_NL = chr(0xE001)  # перевод строки внутри слов пользователя, пока строка переводится (см. _balanced)
_EDGE_OPEN_RE = re.compile(r"(?:<(?:b|i|u|s|code)>)+")
_EDGE_CLOSE_RE = re.compile(r"(?:</(?:b|i|u|s|code)>)+$")

# Сбор непереведённого (тесты и разовые прогоны): скелеты строк, которых нет в каталоге, и сколько раз встретились.
_misses: Counter[str] = Counter()
_seen: Counter[str] = Counter()
_suspects: Counter[str] = Counter()
_COLLECT_PATH = os.environ.get("I18N_COLLECT", "").strip()


# --- Язык ------------------------------------------------------------------------------------------


def normalize(value: object) -> str:
    """Любое значение -> «ru» или «uz» (неизвестное — русский)."""
    return UZ if isinstance(value, str) and value.strip().lower().startswith(UZ) else RU


def from_telegram(language_code: str | None) -> str:
    """Язык по умолчанию — по языку Telegram пользователя («uz», «uz-Latn» -> узбекский)."""
    return normalize(language_code)


def current() -> str:
    """Язык текущего запроса (апдейта чата или запроса приложения)."""
    return _current.get()


@contextmanager
def use_lang(lang: str | None) -> Iterator[None]:
    token = _current.set(normalize(lang))
    try:
        yield
    finally:
        _current.reset(token)


def set_current(lang: str | None) -> None:
    """Язык до конца обработки текущего апдейта (у каждого апдейта свой контекст)."""
    _current.set(normalize(lang))


def remember(tg_id: int | None, lang: str | None) -> None:
    """Запомнить язык человека: по нему переводятся сообщения, которые бот шлёт в его чат."""
    if tg_id is not None:
        _lang_by_tg[int(tg_id)] = normalize(lang)


def lang_of(tg_id: int | None) -> str:
    """Язык чата: запомненный язык человека; незнакомому — русский."""
    return _lang_by_tg.get(int(tg_id), RU) if tg_id is not None else RU


def forget_all() -> None:
    """Забыть языки (тесты)."""
    _lang_by_tg.clear()


# --- Метки слов пользователя ---------------------------------------------------------------------------


def marks_enabled() -> bool:
    from bot.config import get_settings

    return get_settings().i18n_marks


def mark(text: str) -> str:
    """Окружить слова пользователя метками (если метки включены): перевод их не тронет."""
    if not text or not marks_enabled():
        return text
    return f"{U0}{text}{U1}"


def strip_marks(text: str) -> str:
    return text.translate(_MARKS) if U0 in text or U1 in text or SB in text else text


def sentence_break() -> str:
    """Граница предложений для перевода (пусто, если метки выключены): ставится между предложениями
    текста самого бота, которые составляются в одну строку из независимых частей."""
    return SB if marks_enabled() else ""


# --- Перевод ------------------------------------------------------------------------------------------


def _catalog() -> tuple[dict[str, str], dict[str, str], re.Pattern[str] | None]:
    global _loaded
    if _loaded is None:
        from bot.i18n import catalog_uz

        words = dict(catalog_uz.WORDS)
        phrases = [(re.compile(source), target) for source, target in getattr(catalog_uz, "PHRASES", ())]
        # Сначала выражения (подпись периода целиком), потом отдельные слова: «Октябрь 2026» — одно целое.
        alternatives = [f"(?:{source.pattern})" for source, _target in phrases]
        if words:
            listed = "|".join(sorted((re.escape(word) for word in words), key=len, reverse=True))
            alternatives.append(f"(?:{listed})")
        pattern = None
        if alternatives:
            pattern = re.compile(rf"(?<![А-Яа-яЁё])(?:{'|'.join(alternatives)})(?![А-Яа-яЁё])")
        _phrases[:] = phrases
        _loaded = (dict(catalog_uz.LINES), words, pattern)
    return _loaded


def _word(found: str, words: dict[str, str]) -> str:
    """Перевод слова-класса или выражения (``catalog_uz.PHRASES``), найденного в строке."""
    hit = words.get(found)
    if hit is not None:
        return hit
    for source, target in _phrases:
        match = source.fullmatch(found)
        if match is not None:
            return match.expand(target)
    return found


_phrases: list[tuple[re.Pattern[str], str]] = []


_loaded: tuple[dict[str, str], dict[str, str], re.Pattern[str] | None] | None = None


def reload_catalog() -> None:
    """Перечитать каталог (тесты, которые его подменяют)."""
    global _loaded, _prefix_list, _menu_reverse
    _loaded = None
    _prefix_list = None
    _menu_reverse = None


def tr(text: str | None, lang: str | None = None) -> str:
    """Готовый текст -> на язык ``lang`` (по умолчанию — язык текущего запроса); метки убираются всегда."""
    if not text:
        return text or ""
    target = normalize(lang) if lang is not None else current()
    if target != UZ:
        if _COLLECT_PATH and _CYRILLIC_RE.search(text):
            for line in _balanced(text).split("\n"):
                _translate_line(line, observe=True)
        return strip_marks(text)
    if not _CYRILLIC_RE.search(text):
        return strip_marks(text)
    return "\n".join(_translate_line(line) for line in _balanced(text).split("\n")).replace(_NL, "\n")


def _balanced(text: str) -> str:
    """Слова пользователя в несколько строк (комментарий с переводами строки) остаются одной «строкой»:
    переводы строки внутри меток на время перевода заменяются служебным символом — фраза вокруг них
    («Комментарий: {u}») ищется в каталоге целиком. Незакрытая метка (текст обрезан) не объединяется."""
    if U0 not in text or "\n" not in text:
        return text
    return _USER_RE.sub(lambda match: match.group(0).replace("\n", _NL), text)


def _mask(line: str) -> tuple[str, list[str], list[str], list[str]]:
    """Строка -> (скелет, слова пользователя, переведённые слова-классы, числа)."""
    _lines, words, pattern = _catalog()
    users: list[str] = []

    def take_user(match: re.Match[str]) -> str:
        users.append(match.group(1))
        if _COLLECT_PATH:
            _suspect(match.group(1))
        return "{u}"

    masked = _USER_RE.sub(take_user, line)
    if U0 in masked:  # обрезанный текст: метка открыта, а закрывающей нет — до конца строки слова пользователя
        head, _, tail = masked.partition(U0)
        users.append(tail.replace(U1, ""))
        masked = head + "{u}"
    masked = masked.replace(U1, "")
    found_words: list[str] = []
    if pattern is not None:

        def take_word(match: re.Match[str]) -> str:
            found_words.append(_word(match.group(0), words))
            return "{w}"

        masked = pattern.sub(take_word, masked)
    numbers: list[str] = []

    def take_number(match: re.Match[str]) -> str:
        numbers.append(match.group(0))
        return "{}"

    # Числа внутри «{u}» / «{w}» не трогаем: заполнителей с цифрами здесь ещё нет.
    masked = _DIGITS_RE.sub(take_number, masked)
    return masked, users, found_words, numbers


def _fill(template: str, users: list[str], words: list[str], numbers: list[str]) -> str:
    counters = {"u": 0, "w": 0, "": 0}
    pools = {"u": users, "w": words, "": numbers}

    def put(match: re.Match[str]) -> str:
        kind, index = match.group(1), match.group(2)
        pool = pools[kind]
        if index:
            position = int(index)
        else:
            position = counters[kind]
            counters[kind] += 1
        return pool[position] if position < len(pool) else ""

    return _SLOT_RE.sub(put, template)


def _translate_line(line: str, *, observe: bool = False) -> str:
    plain = strip_marks(line)
    if "{" in plain or "}" in plain or not _CYRILLIC_RE.search(_USER_RE.sub("", line).replace(U0, "")):
        return plain  # скобки заполнителей в самом тексте или переводить нечего (только слова пользователя)
    if SB in line:  # несколько самостоятельных предложений в одной строке — каждое переводится отдельно
        return "".join(_translate_line(piece, observe=observe) for piece in line.split(SB))
    stripped = line.strip()
    lead = line[: len(line) - len(line.lstrip())]
    trail = line[len(line.rstrip()):]
    key, users, words, numbers = _mask(stripped)
    if _COLLECT_PATH:
        _seen[key] += 1
    template = _template_for(key)
    if observe:
        return plain
    return lead + _fill(template, users, words, numbers) + trail


def _template_for(key: str) -> str:
    """Узбекский шаблон для скелета ``key``. Порядок поиска: строка целиком -> известное начало строки
    (``catalog_uz.PREFIXES``) + остаток -> части между разделителями. Чего в каталоге нет, остаётся по-русски
    и записывается в ``misses``. Заполнители результата — с явными номерам (части можно склеивать)."""
    lines, _words, _pattern = _catalog()
    hit = lines.get(key)
    if hit is not None:
        return _absolute(hit, (0, 0, 0))
    for prefix, uz_prefix in _prefixes():
        if key.startswith(prefix) and len(key) > len(prefix):
            return _absolute(uz_prefix, (0, 0, 0)) + _shifted(key[len(prefix):], _slot_counts(prefix))
    for separator in _SEPARATORS:
        if separator in key:
            parts = key.split(separator)
            if any(lines.get(part) is not None or any(part.startswith(p) for p, _ in _prefixes()) for part in parts):
                offsets = (0, 0, 0)
                out: list[str] = []
                for part in parts:
                    out.append(_shifted(part, offsets))
                    counts = _slot_counts(part)
                    offsets = (offsets[0] + counts[0], offsets[1] + counts[1], offsets[2] + counts[2])
                return separator.join(out)
    # Строка в тегах («<i>текст</i>», кусок «текст.</i>»): в каталоге ищется сам текст, теги возвращаются.
    head = _EDGE_OPEN_RE.match(key)
    tail = _EDGE_CLOSE_RE.search(key)
    if head is not None or tail is not None:
        start = head.end() if head is not None else 0
        end = tail.start() if tail is not None else len(key)
        if start < end:
            return key[:start] + _template_for(key[start:end]) + key[end:]
    _miss(key)
    return _absolute(key, (0, 0, 0))


def _shifted(key: str, offsets: tuple[int, int, int]) -> str:
    """Шаблон для части строки, заполнители которой начинаются с ``offsets`` (сколько их было левее)."""
    # Часть без русских букв, но с датой («{} {w} {w}, {}:{}») тоже может стоять в каталоге: у даты свой порядок.
    template = _template_for(key) if _CYRILLIC_RE.search(key) or "{w}" in key else _absolute(key, (0, 0, 0))
    if offsets == (0, 0, 0):
        return template
    base = dict(zip(_KINDS, offsets, strict=True))
    return _SLOT_RE.sub(lambda m: "{" + m.group(1) + str(base[m.group(1)] + int(m.group(2))) + "}", template)


_KINDS = ("u", "w", "")


def _slot_counts(key: str) -> tuple[int, int, int]:
    counts = {"u": 0, "w": 0, "": 0}
    for kind, _index in _SLOT_RE.findall(key):
        counts[kind] += 1
    return counts["u"], counts["w"], counts[""]


def _absolute(template: str, offsets: tuple[int, int, int]) -> str:
    """Заполнители по порядку («{u}», «{}») -> с явными номерами со сдвигом: «{u2}», «{5}»."""
    base = dict(zip(_KINDS, offsets, strict=True))
    counters = {"u": 0, "w": 0, "": 0}

    def number(match: re.Match[str]) -> str:
        kind, index = match.group(1), match.group(2)
        if index:
            position = int(index)
        else:
            position = counters[kind]
            counters[kind] += 1
        return "{" + kind + str(base[kind] + position) + "}"

    return _SLOT_RE.sub(number, template)


def _prefixes() -> list[tuple[str, str]]:
    global _prefix_list
    if _prefix_list is None:
        from bot.i18n import catalog_uz

        _prefix_list = sorted(getattr(catalog_uz, "PREFIXES", {}).items(), key=lambda item: -len(item[0]))
    return _prefix_list


_prefix_list: list[tuple[str, str]] | None = None
_SEPARATORS = (" · ", "; ", " → ")


def _suspect(span: str) -> None:
    """Сбор: «слова пользователя», которые на деле текст бота (есть в каталоге) — их пометили через esc() зря,
    и перевод их не тронет. Такие места видны в отчёте сбора (ключ «suspect»)."""
    lines, _words, _pattern = _catalog()
    key = _DIGITS_RE.sub("{}", html.unescape(span).strip())
    if _CYRILLIC_RE.search(key) and key in lines:
        _suspects[span] += 1


def _miss(key: str) -> None:
    if _CYRILLIC_RE.search(key):
        _misses[key] += 1


def misses() -> dict[str, int]:
    """Скелеты строк, которых нет в каталоге, и сколько раз они встретились с запуска процесса."""
    return dict(_misses)


def clear_misses() -> None:
    _misses.clear()
    _seen.clear()
    _suspects.clear()


# --- Тексты, по которым бот узнаёт свои сообщения и кнопки --------------------------------------------


def variants(text: str) -> tuple[str, ...]:
    """Текст на всех языках интерфейса: (русский, узбекский). Для фильтров кнопок меню и сравнения с текстом
    собственных сообщений бота (сообщение могло уйти человеку на любом языке)."""
    translated = tr(text, UZ)
    return (text,) if translated == text else (text, translated)


def menu_source(text: str | None) -> str | None:
    """Надпись кнопки главного меню на узбекском -> та же надпись по-русски (хендлеры ждут русскую); иначе None."""
    if not text:
        return None
    global _menu_reverse
    if _menu_reverse is None:
        from bot.ui.texts import MENU_BUTTONS

        _menu_reverse = {tr(button, UZ): button for button in MENU_BUTTONS}
    source = _menu_reverse.get(text.strip())
    return source if source is not None and source != text else None


_menu_reverse: dict[str, str] | None = None


def _dump_collected() -> None:
    if not _COLLECT_PATH:
        return
    try:
        previous: dict[str, dict[str, int]] = {"seen": {}, "missing": {}}
        if os.path.exists(_COLLECT_PATH):
            with open(_COLLECT_PATH, encoding="utf-8") as fh:
                previous = json.load(fh)
        for name, counter in (("seen", _seen), ("missing", _misses), ("suspect", _suspects)):
            merged = Counter(previous.get(name, {}))
            merged.update(counter)
            previous[name] = dict(merged)
        with open(_COLLECT_PATH, "w", encoding="utf-8") as fh:
            json.dump(previous, fh, ensure_ascii=False, indent=0, sort_keys=True)
    except Exception:  # noqa: BLE001 - сбор статистики не должен мешать завершению
        log.debug("Не удалось записать собранные строки", exc_info=True)


atexit.register(_dump_collected)
