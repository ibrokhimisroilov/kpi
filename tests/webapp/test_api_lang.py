"""Язык мини-приложения (SPEC.md §14, docs/MINIAPP_SPEC.md §8.11): /api/me отдаёт язык, POST /api/lang меняет
его для чата и приложения, подписи сервера (статус, срок, журнал, ошибки) приходят на языке пользователя,
тексты задач не переводятся, невидимые метки слов пользователя в JSON не попадают.

Метки здесь включены — как в работе: без них перевод не отличит название задачи от текста бота.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select

from bot import i18n
from bot.db.models import User
from bot.webapp import api

from .conftest import EMP, MGR, MiniApp

pytestmark = pytest.mark.asyncio

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
STRANGER = 9999
# Слова людей в этих сценариях: их перевод не трогает.
USER_WORDS = (
    "Петрова Анна Сергеевна", "Иванов Иван Иванович", "Петрова А. С.", "Иванов И. И.", "Юрист",
    "Анализ договоров", "Проверить 100 договоров и представить отчёт", "договоров", "Отчёт",
    "Сделано всё по плану", "Задачи", "Проверено 110 договоров",
)


@pytest.fixture(autouse=True)
def _marks_on(set_env: Callable[..., None]) -> None:
    set_env(I18N_MARKS="true")


def strings(value: Any) -> list[str]:
    """Все строки ответа (значения, не ключи)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in strings(item)]
    return []


def russian_left(data: Any) -> list[str]:
    """Строки ответа, где после вычёркивания слов людей остались русские буквы."""
    left = []
    for text in strings(data):
        rest = text
        for words in sorted(USER_WORDS, key=len, reverse=True):
            rest = rest.replace(words, "")
        if CYRILLIC.search(rest):
            left.append(text)
    return left


async def lang_in_db(ma: MiniApp, tg_id: int) -> str | None:
    async with ma.db() as s:
        return (await s.execute(select(User.lang).where(User.tg_id == tg_id))).scalar_one()


async def test_me_reports_language_and_switch_changes_it_everywhere(ma: MiniApp) -> None:
    await ma.seed_team(1)
    me = await ma.get("/api/me", as_=MGR)
    assert me["lang"] == "ru"
    assert me["langs"] == [{"code": "ru", "name": "Русский"}, {"code": "uz", "name": "Oʻzbekcha"}]

    resp = await ma.post("/api/lang", as_=MGR, json={"lang": "uz"})
    assert resp.status == 200 and resp.data == {"lang": "uz"}
    assert await lang_in_db(ma, MGR) == "uz"
    assert i18n.lang_of(MGR) == "uz"  # уведомления в чат — тоже по-узбекски

    me = await ma.get("/api/me", as_=MGR)
    assert me["lang"] == "uz"
    assert [option["label"] for option in me["deadline_options"]][-1].startswith("Oy oxiri")
    other = await ma.get("/api/me", as_=EMP)  # язык — у каждого свой
    assert other["lang"] == "ru"

    resp = await ma.post("/api/lang", as_=MGR, json={"lang": "ru"})
    assert resp.data == {"lang": "ru"} and await lang_in_db(ma, MGR) == "ru"


@pytest.mark.parametrize("body", [{}, {"lang": "en"}, {"lang": 5}, {"lang": "uz", "extra": 1}])
async def test_switch_rejects_unknown_language(ma: MiniApp, body: dict[str, Any]) -> None:
    await ma.seed_team(1)
    resp = await ma.post("/api/lang", as_=MGR, json=body)
    assert resp.status == 400 and resp.code == "bad_request"
    assert await lang_in_db(ma, MGR) is None


async def test_telegram_language_is_the_default_and_is_remembered(ma: MiniApp) -> None:
    """Язык не выбран, Telegram на узбекском — приложение узбекское, и выбор запоминается у пользователя."""
    await ma.seed_team(1)
    headers = ma.auth(EMP, language_code="uz")
    me = await ma.request("GET", "/api/me", headers=headers)
    assert me["lang"] == "uz"
    assert await lang_in_db(ma, EMP) == "uz"
    # Выбор человека важнее языка Telegram.
    await ma.post("/api/lang", as_=EMP, json={"lang": "ru"})
    me = await ma.request("GET", "/api/me", headers=headers)
    assert me["lang"] == "ru"


async def test_unregistered_user_sees_messages_in_telegram_language(ma: MiniApp) -> None:
    headers = ma.auth(STRANGER, language_code="uz")
    me = await ma.request("GET", "/api/me", headers=headers)
    assert me["access"] == "unregistered" and me["lang"] == "uz"
    assert me["message"] and not CYRILLIC.search(me["message"])
    resp = await ma.request("GET", "/api/tasks", headers=headers)
    assert resp.status == 403 and resp.code == "not_registered"
    assert not CYRILLIC.search(resp.error or "") and resp["ru"] == api.NOT_REGISTERED
    # Тот же человек с русским Telegram — по-русски и без поля «ru».
    resp = await ma.request("GET", "/api/tasks", as_=STRANGER)
    assert resp.error == api.NOT_REGISTERED and "ru" not in resp.data


async def test_server_labels_come_in_uzbek_and_task_texts_stay(ma: MiniApp) -> None:
    """Карточка, списки, очередь проверки, KPI и дашборд узбекского начальника: русскими остаются только
    слова людей. Задача названа «Задачи» — как надпись бота; название всё равно не переводится."""
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr)
    submitted = await ma.seed_task(emp, mgr, kind="submitted", title="Отчёт")
    tricky = await ma.seed_task(emp, mgr, title="Задачи")
    await ma.seed_task(emp, mgr, kind="proposed", title="Отчёт")
    await ma.post("/api/lang", as_=MGR, json={"lang": "uz"})

    card = await ma.get(f"/api/tasks/{task_id}", as_=MGR)
    assert card.status == 200
    task = card["task"]
    assert task["title"] == "Анализ договоров" and task["expected_result"] == "Проверить 100 договоров и представить отчёт"
    assert task["status_label"].endswith("Bajarilmoqda") and task["priority_label"].endswith("Oʻrta")
    assert "qoldi" in task["deadline_label"]

    paths = [
        f"/api/tasks/{task_id}", f"/api/tasks/{submitted}", f"/api/tasks/{tricky}", "/api/tasks?scope=all&status=all",
        "/api/review", "/api/proposals", "/api/dashboard", f"/api/users/{emp.id}/kpi", "/api/me",
        f"/api/employees/{emp.id}/weight-load?deadline=2099-01-05",
    ]
    for path in paths:
        resp = await ma.get(path, as_=MGR)
        assert resp.status == 200, path
        assert not russian_left({key: value for key, value in resp.data.items() if key != "langs"}), path
        assert i18n.U0 not in resp.text and i18n.U1 not in resp.text and i18n.SB not in resp.text, path
    assert (await ma.get(f"/api/tasks/{tricky}", as_=MGR))["task"]["title"] == "Задачи"

    # Сотрудник язык не менял — у него всё по-русски.
    mine = await ma.get(f"/api/tasks/{task_id}", as_=EMP)
    assert mine["task"]["status_label"].endswith("В работе")
    assert i18n.U0 not in mine.text


async def test_errors_are_translated_and_keep_the_russian_source(ma: MiniApp) -> None:
    """Текст ошибки — на языке пользователя; рядом «ru» — тот же текст по-русски: по нему приложение
    узнаёт вид ошибки («срок», «уже обработан»)."""
    mgr, (emp,) = await ma.seed_team(1)
    await ma.seed_task(emp, mgr)
    await ma.post("/api/lang", as_=EMP, json={"lang": "uz"})
    resp = await ma.post("/api/tasks", as_=EMP, json={})
    assert resp.status == 403 and resp.code == "forbidden"
    assert resp.error == i18n.tr(api.MANAGER_ONLY, "uz") != api.MANAGER_ONLY
    assert resp["ru"] == api.MANAGER_ONLY
    resp = await ma.get("/api/tasks/999999", as_=EMP)
    assert resp.status == 404 and resp.error == "Vazifa topilmadi." and resp["ru"] == api.TASK_NOT_FOUND
    # Русскому пользователю поле «ru» не нужно — текст и так русский.
    resp = await ma.get("/api/tasks/999999", as_=MGR)
    assert resp.error == api.TASK_NOT_FOUND and "ru" not in json.loads(resp.text)


async def test_language_of_one_request_does_not_leak_into_the_next(ma: MiniApp) -> None:
    """Соединение общее, пользователи разные: после узбекского запроса русский ответ остаётся русским."""
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr)
    await ma.post("/api/lang", as_=MGR, json={"lang": "uz"})
    for _ in range(2):
        uz_card = await ma.get(f"/api/tasks/{task_id}", as_=MGR)
        ru_card = await ma.get(f"/api/tasks/{task_id}", as_=EMP)
        assert uz_card["task"]["status_label"].endswith("Bajarilmoqda")
        assert ru_card["task"]["status_label"].endswith("В работе")
    resp = await ma.request("GET", "/api/me")  # без входа — по-русски, а не на языке прошлого запроса
    assert resp.status == 401 and CYRILLIC.search(resp.error or "")
