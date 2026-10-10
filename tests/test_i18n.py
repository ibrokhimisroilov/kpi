"""Перевод интерфейса (bot.i18n, SPEC.md §14): переводчик «на выходе», каталог, словарь мини-приложения.

Тексты бота пишутся по-русски; на узбекский их переводит ``i18n.tr`` по «скелету» строки: слова пользователя
(помечены ``esc()`` / ``own()``) -> ``{u}``, числа -> ``{}``, слова-классы -> ``{w}``. Здесь проверяется сам
механизм, целостность каталога ``catalog_uz`` и словаря ``UZ`` в app.js (заполнители, отсутствие русских букв
в переводах, перевод каждой отдельной надписи приложения).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from bot import i18n
from bot.i18n import catalog_uz
from bot.ui.texts import BTN_LANG, MENU_BUTTONS, TXT_CHOOSE_LANG
from bot.utils.text import esc, own

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
SLOT = re.compile(r"\{(u|w|)(\d*)\}")
APP_JS = Path(__file__).resolve().parents[1] / "bot" / "webapp" / "static" / "app.js"
# Переводы, в которых русские слова оставлены нарочно: двуязычные надписи выбора языка.
BILINGUAL = {BTN_LANG, TXT_CHOOSE_LANG, "Русский", "✅ Язык: Русский."}


@pytest.fixture(autouse=True)
def _marks_on(set_env: Callable[..., None]) -> Iterator[None]:
    set_env(I18N_MARKS="true")
    i18n.clear_misses()
    yield
    i18n.clear_misses()


def uz(text: str) -> str:
    return i18n.tr(text, i18n.UZ)


# --- Механизм ---------------------------------------------------------------------------------------


def test_russian_is_returned_as_is_without_marks() -> None:
    text = f"📌 Задача #5: {esc('Анализ договоров')}"
    assert i18n.U0 in text  # метка есть до отправки…
    assert i18n.tr(text, i18n.RU) == "📌 Задача #5: Анализ договоров"  # …и её нет после


def test_user_words_are_never_translated() -> None:
    """Название задачи совпадает с надписью бота («Задачи») — переводится только надпись."""
    assert uz("Задачи") == "Vazifalar"
    assert uz(esc("Задачи")) == "Задачи"
    assert uz(own("Задачи")) == "Задачи"
    line = uz(f"📌 Задача #5: {esc('Задачи')}")
    assert line.endswith(": Задачи") and "Vazifa" in line
    assert not i18n.misses()


def test_numbers_dates_and_word_classes_are_carried_over() -> None:
    assert uz("⚖️ Вес: 20 %") == "⚖️ Vazn: 20 %"
    assert uz("⏰ <b>Срок:</b> 5 октября 2026 (пн), 18:00") == "⏰ <b>Muddat:</b> 2026-yil 5-oktabr (du), 18:00"
    assert uz("12 октября (пн), 12:54 · осталось 1 дн.") == "12-oktabr (du), 12:54 · 1 kun qoldi"
    assert uz("📊 Неделя: <b>95,5 %</b> · Месяц: <b>—</b>") == "📊 Hafta: <b>95,5 %</b> · Oy: <b>—</b>"
    assert not i18n.misses()


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Неделя 05.10–11.10.2026", "Hafta 05.10–11.10.2026"),
        ("Неделя 28.12.2026–03.01.2027", "Hafta 28.12.2026–03.01.2027"),
        ("Октябрь 2026", "2026-yil oktabr"),
        ("IV квартал 2026", "2026-yil IV chorak"),
        ("I квартал 2027", "2027-yil I chorak"),
        ("2026 год", "2026-yil"),
    ],
)
def test_period_labels(label: str, expected: str) -> None:
    assert uz(label) == expected
    assert uz(f"📊 <b>Команда · {label}</b>") == f"📊 <b>Jamoa · {expected}</b>"
    assert uz(f"📊 Отчёт: {label}") == f"📊 Hisobot: {expected}"
    assert not i18n.misses()


def test_multiline_user_text_and_several_lines() -> None:
    comment = esc("Первая строка\nВторая строка")
    text = uz(f"💬 Комментарий начальника: <i>{comment}</i>\n\n⚖️ Вес: 10 %")
    assert "Первая строка\nВторая строка" in text
    assert text.endswith("⚖️ Vazn: 10 %")
    assert not CYRILLIC.search(text.replace("Первая строка", "").replace("Вторая строка", ""))


def test_tags_around_a_phrase_and_sentences_in_one_line() -> None:
    assert uz("<i>Сдано в срок.</i>") == "<i>Muddatida topshirilgan.</i>"
    from bot.ui import render

    rules = render.rules_sentences("План: 100, факт: 95 — выполнение 95 %. Сдано в срок. Итог: 95 %.")
    assert uz(rules) == "Reja: 100, fakt: 95 — bajarilishi 95 %. Muddatida topshirilgan. Yakun: 95 %."
    assert not i18n.misses()


def test_unknown_text_stays_russian_and_is_reported() -> None:
    text = "Такой строки в каталоге точно нет 42"
    assert uz(text) == text
    assert i18n.misses() == {"Такой строки в каталоге точно нет {}": 1}


def test_text_without_russian_letters_is_untouched() -> None:
    for text in ("✅ 5 · ⏰ 0", "KPI 95 %", "{}", "12.10.2026 18:00"):
        assert uz(text) == text
    assert not i18n.misses()


def test_language_helpers() -> None:
    assert i18n.normalize("UZ") == "uz" and i18n.normalize(None) == "ru" and i18n.normalize("en") == "ru"
    assert i18n.from_telegram("uz") == "uz" and i18n.from_telegram("uz-Latn") == "uz"
    assert i18n.from_telegram("ru") == "ru" and i18n.from_telegram("en") == "ru" and i18n.from_telegram(None) == "ru"
    i18n.remember(5, "uz")
    assert i18n.lang_of(5) == "uz" and i18n.lang_of(6) == "ru"
    with i18n.use_lang("uz"):
        assert i18n.current() == "uz" and i18n.tr("Задачи") == "Vazifalar"
    assert i18n.tr("Задачи", "ru") == "Задачи"


# --- Кнопки меню ---------------------------------------------------------------------------------


def test_menu_buttons_are_translated_and_recognised_back() -> None:
    """Каждая кнопка главного меню переведена, а узбекская надпись возвращается к русской, которую
    ждут хендлеры. Кнопка языка — одна на обоих языках."""
    translated = {button: uz(button) for button in MENU_BUTTONS}
    assert len(set(translated.values())) == len(MENU_BUTTONS)  # переводы не сливаются
    for button, label in translated.items():
        if button == BTN_LANG:
            assert label == BTN_LANG and i18n.menu_source(label) is None
            continue
        assert not CYRILLIC.search(label), button
        assert i18n.menu_source(label) == button
        assert i18n.menu_source(button) is None  # русская надпись — как есть
        assert i18n.variants(button) == (button, label)
    assert i18n.menu_source("Привет") is None


# --- Каталог ------------------------------------------------------------------------------------


def _slots(text: str) -> dict[str, int]:
    need = {"u": 0, "w": 0, "": 0}
    seq = {"u": 0, "w": 0, "": 0}
    for kind, index in SLOT.findall(text):
        if index:
            need[kind] = max(need[kind], int(index) + 1)
        else:
            seq[kind] += 1
            need[kind] = max(need[kind], seq[kind])
    return need


def test_catalog_placeholders_fit_their_sources() -> None:
    """Перевод не просит больше слов пользователя, чисел и слов-классов, чем есть в русской строке."""
    bad = []
    for source in (catalog_uz.LINES, catalog_uz.PREFIXES):
        for key, value in source.items():
            have, want = _slots(key), _slots(value)
            if any(want[kind] > have[kind] for kind in have):
                bad.append(key)
    assert not bad, bad[:10]


def test_catalog_translations_have_no_russian_letters() -> None:
    bad = [
        key
        for source in (catalog_uz.LINES, catalog_uz.PREFIXES, catalog_uz.WORDS)
        for key, value in source.items()
        if CYRILLIC.search(value) and key not in BILINGUAL and "Язык" not in key and "язык" not in key
    ]
    assert not bad, bad[:10]


def test_catalog_phrases_compile() -> None:
    for pattern, replacement in catalog_uz.PHRASES:
        assert re.compile(pattern).groups >= replacement.count("\\")


# --- Словарь мини-приложения (app.js) ------------------------------------------------------------------


def _js_dictionary() -> dict[str, str]:
    source = APP_JS.read_text(encoding="utf-8")
    block = source.split("/*UZ-BEGIN*/", 1)[1].split("/*UZ-END*/", 1)[0]
    return json.loads("{" + block.strip().rstrip(",") + "}")


def _js_code() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    head, rest = source.split("/*UZ-BEGIN*/", 1)
    return head + rest.split("/*UZ-END*/", 1)[1]


def test_app_dictionary_is_consistent() -> None:
    dictionary = _js_dictionary()
    assert len(dictionary) > 300
    for key, value in dictionary.items():
        have, want = _slots(key), _slots(value)
        assert want["u"] <= have["u"] and want[""] <= have[""], key
        if key != "Перейти на русский язык":
            assert not CYRILLIC.search(value), key


# Куски составных надписей и служебные слова: целиком они на экран не выходят (или переводятся иначе).
_JS_FRAGMENTS = {
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
    "декабря", "вс", "пн", "вт", "ср", "чт", "пт", "сб", "Б", "КБ", "МБ", "Эффективность", "Русский",
    "сотрудник", "сотрудника", "сотрудников", "задача", "задачи", "задач", "неделя", "недели", "недель",
    "файл", "файла", "файлов", "— сохранены в чате с ботом", "· ⏰ просрочена",
}


def test_every_standalone_app_label_is_in_the_dictionary() -> None:
    """Каждая русская надпись app.js, которая выводится целиком (не склеивается с другими), есть в словаре:
    новая непереведённая надпись в приложении роняет этот тест."""
    dictionary = _js_dictionary()
    number = re.compile(r"\d{1,3}(?:[   ]\d{3})+(?:,\d+)?|\d+(?:,\d+)?")
    missing = set()
    for line in _js_code().split("\n"):
        if line.lstrip().startswith(("//", "/*", "*")):
            continue  # комментарий
        code = line
        for match in re.finditer(r"'((?:[^'\\\n]|\\.)*)'", code):
            text = match.group(1).replace("\\'", "'")
            if not CYRILLIC.search(text):
                continue
            before, after = code[: match.start()].rstrip(), code[match.end():].lstrip()
            if before.endswith("+") or after.startswith("+"):
                continue  # склейка: итоговая строка проверяется в браузере (kpiI18n.misses)
            if before.endswith(("/", "test(", "RegExp(")) or "new Error(" in before[-12:]:
                continue
            key = number.sub("{}", text.strip())
            if key and key not in dictionary and key not in _JS_FRAGMENTS:
                missing.add(key)
    assert not missing, sorted(missing)[:15]
