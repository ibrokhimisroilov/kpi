"""Права на сайте — в точности те же, что в приложении Telegram (docs/MINIAPP_SPEC.md §15.4).

Сайт своих правил не добавляет: человека называет cookie сессии, дальше запрос идёт теми же обработчиками.
Здесь это проверяется сразу по всем адресам API (``api.ROUTES``) — новый адрес попадает в проверку сам:

* сотрудник с чужими номерами задач, сдач и людей получает отказ, тот же, что в Mini App, и ничего не меняет;
* свои данные человек видит на сайте теми же, что в Mini App (один в один), — начальник и сотрудник.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text

from bot.webapp import api

from .conftest import EMP, EMP2, MGR, MiniApp, Resp
from .test_weblogin import Browser, sign_in

PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Адреса, где сотрудник — законный посетитель и без номера в пути ничего чужого не трогает.
OPEN_TO_EMPLOYEE = {
    ("GET", "/api/me"),
    ("POST", "/api/lang"),
    ("GET", "/api/tasks"),
    ("POST", "/api/ai/formulate"),
    ("POST", "/api/voice"),
    ("POST", "/api/proposals"),
    ("GET", "/api/dashboard"),
}


def fill(path: str, ids: dict[str, int]) -> str:
    """Шаблон адреса -> адрес. Незнакомая подстановка — ошибка: новый вид номера надо добавить в проверку."""
    return PLACEHOLDER.sub(lambda match: str(ids[match.group(1)]), path)


def answer(resp: Resp) -> tuple[int, str | None]:
    return resp.status, resp.code


async def snapshot(ma: MiniApp) -> dict[str, list[tuple[Any, ...]]]:
    """Всё, что могло бы измениться от чужого запроса: задачи, сдачи, журнал, люди."""
    queries = {
        "tasks": "SELECT id, status, title, deadline, weight, priority, assignee_id, accepted_at, final_score FROM tasks ORDER BY id",
        "submissions": "SELECT id, task_id, decision, final_score, review_comment FROM submissions ORDER BY id",
        "events": "SELECT count(*) FROM task_events",
        "users": "SELECT id, role, status, lang FROM users ORDER BY id",
    }
    async with ma.db() as session:
        return {name: [tuple(row) for row in await session.execute(text(sql))] for name, sql in queries.items()}


def test_every_route_placeholder_is_known() -> None:
    known = {"task_id": 1, "sub_id": 1, "user_id": 1}
    for _method, path in api.ROUTES:
        assert "{" not in fill(path, known), path
    assert {route for route in OPEN_TO_EMPLOYEE if route not in api.ROUTES} == set()


async def test_employee_with_foreign_numbers_is_refused_exactly_as_in_telegram(ma: MiniApp) -> None:
    mgr, (emp, emp2) = await ma.seed_team(2)
    await ma.seed_task(emp, mgr, title="Своя задача")
    foreign = {
        kind: await ma.seed_task(emp2, mgr, kind=kind, title=f"Чужая: {kind}")
        for kind in ("active", "overdue", "rework", "submitted", "done", "proposed", "cancelled", "rejected")
    }
    sub_ids = {
        kind: (await ma.task(foreign[kind])).submissions[0].id for kind in ("rework", "submitted", "done")
    }
    browser = await sign_in(ma, EMP)
    before = await snapshot(ma)
    sent = len(ma.api.calls)

    checked = 0
    for method, path in api.ROUTES:
        names = set(PLACEHOLDER.findall(path))
        if not names:
            continue
        # Чужая задача каждого вида (и её сдача, если есть) + чужой человек.
        for kind, task_id in foreign.items():
            if "sub_id" in names and kind not in sub_ids:
                continue
            url = fill(path, {"task_id": task_id, "sub_id": sub_ids.get(kind, 0), "user_id": emp2.id})
            body = None if method == "GET" else {}
            in_telegram = await ma.request(method, url, as_=EMP, json=body)
            on_site = await browser.request(method, url, json=body)
            assert answer(on_site) == answer(in_telegram), (method, url, on_site.data, in_telegram.data)
            assert on_site.status in (403, 404), (method, url, on_site.status, on_site.data)
            checked += 1
            if names == {"user_id"}:
                break  # вид задачи для адреса с человеком не важен

    assert checked > 60  # 7 адресов задач × 8 видов, 5 адресов сдач × 3, 2 адреса людей
    assert await snapshot(ma) == before
    assert len(ma.api.calls) == sent  # и ни одного сообщения в Telegram


async def test_routes_without_numbers_are_open_to_an_employee_only_from_the_known_list(ma: MiniApp) -> None:
    mgr, (emp, _emp2) = await ma.seed_team(2)
    await ma.seed_task(emp, mgr, title="Своя задача")
    browser = await sign_in(ma, EMP)
    before = await snapshot(ma)

    for method, path in api.ROUTES:
        if PLACEHOLDER.search(path):
            continue
        body = None if method == "GET" else {}
        in_telegram = await ma.request(method, path, as_=EMP, json=body)
        on_site = await browser.request(method, path, json=body)
        assert answer(on_site) == answer(in_telegram), (method, path, on_site.data, in_telegram.data)
        if (method, path) in OPEN_TO_EMPLOYEE:
            assert on_site.status != 403, (method, path, on_site.data)
        else:  # нового адреса нет в списке — он обязан отказывать сотруднику
            assert answer(on_site) == (403, "forbidden"), (method, path, on_site.data)

    assert await snapshot(ma) == before


async def test_manager_only_routes_refuse_an_employee_on_the_site_as_in_telegram(ma: MiniApp) -> None:
    mgr, (emp, _emp2) = await ma.seed_team(2)
    own = await ma.seed_task(emp, mgr, title="Своя задача")
    own_submitted = await ma.seed_task(emp, mgr, kind="submitted", title="Свой отчёт")
    own_proposed = await ma.seed_task(emp, mgr, kind="proposed", title="Своё поручение")
    sub_id = (await ma.task(own_submitted)).submissions[0].id
    browser = await sign_in(ma, EMP)
    before = await snapshot(ma)

    # Действия начальника над СВОИМИ задачами сотрудника: оценить себя, принять своё поручение, поменять срок.
    valid_bodies: dict[tuple[str, str], Any] = {
        ("POST", "/api/tasks"): {
            "assignee_id": emp.id, "title": "Сам себе", "expected_result": "Поставить себе задачу в обход начальника",
            "deadline": "2026-10-20T18:00", "priority": "low", "weight": 5,
        },
        ("PATCH", f"/api/tasks/{own}"): {"weight": 50},
        ("POST", f"/api/tasks/{own}/cancel"): {"reason": "не хочу"},
        ("POST", f"/api/tasks/{own_proposed}/approve"): {"weight": 50, "priority": "high"},
        ("POST", f"/api/tasks/{own_proposed}/reject"): {"reason": "нет"},
        ("POST", f"/api/submissions/{sub_id}/confirm"): {},
        ("POST", f"/api/submissions/{sub_id}/score"): {"score": 150, "comment": "сам себе"},
        ("POST", f"/api/submissions/{sub_id}/revise"): {"score": 150, "comment": "сам себе"},
        ("POST", f"/api/submissions/{sub_id}/rework"): {"comment": "сам себе"},
        ("POST", f"/api/submissions/{sub_id}/files"): {},
        ("GET", "/api/review"): None,
        ("GET", "/api/employees"): None,
        ("GET", f"/api/employees/{emp.id}/weight-load?deadline=2026-10-20T18:00"): None,
        ("POST", "/api/export"): {"period": "week"},
    }
    for (method, url), body in valid_bodies.items():
        in_telegram = await ma.request(method, url, as_=EMP, json=body)
        on_site = await browser.request(method, url, json=body)
        assert answer(on_site) == answer(in_telegram) == (403, "forbidden"), (method, url, on_site.data, in_telegram.data)

    assert await snapshot(ma) == before


async def test_person_sees_the_same_data_on_the_site_as_in_telegram(ma: MiniApp, frozen: Any) -> None:
    mgr, (emp, emp2) = await ma.seed_team(2)
    tasks = {
        kind: await ma.seed_task(emp, mgr, kind=kind, title=f"Задача: {kind}", files=1 if kind == "submitted" else 0)
        for kind in ("active", "overdue", "rework", "submitted", "done", "proposed", "cancelled")
    }
    await ma.seed_task(emp2, mgr, kind="submitted", title="Отчёт Сидорова")
    await ma.seed_task(emp2, mgr, kind="done", title="Сделано Сидоровым", final_score=90.0)
    employee_urls = [
        "/api/me", "/api/tasks", "/api/tasks?status=done", "/api/tasks?counts=1", "/api/dashboard",
        f"/api/users/{emp.id}/kpi", *(f"/api/tasks/{task_id}" for task_id in tasks.values()),
    ]
    manager_urls = [
        "/api/me", "/api/tasks", "/api/tasks?counts=1&sort=-deadline", f"/api/tasks?assignee_id={emp2.id}",
        "/api/review", "/api/proposals", "/api/dashboard", "/api/dashboard?period=month", "/api/employees",
        f"/api/users/{emp.id}/kpi", f"/api/users/{emp2.id}/kpi", f"/api/employees/{emp.id}/weight-load?deadline=2026-10-06T18:00",
        *(f"/api/tasks/{task_id}" for task_id in tasks.values()),
    ]
    for tg_id, urls in ((EMP, employee_urls), (MGR, manager_urls)):
        browser = await sign_in(ma, tg_id)
        for url in urls:
            in_telegram = await ma.get(url, as_=tg_id)
            on_site = await browser.get(url)
            assert in_telegram.status == 200, (tg_id, url, in_telegram.data)
            assert on_site.status == 200 and on_site.data == in_telegram.data, (tg_id, url)

    # Оценку AI сотрудник до решения начальника не видит — и на сайте тоже.
    worker = await sign_in(ma, EMP)
    card = await worker.get(f"/api/tasks/{tasks['submitted']}")
    sub = card["submissions"][0]
    assert card["task"]["ai_score"] is None and sub["ai"] is None and sub["ai_hidden"] is True
    assert "Расчёт по правилам" not in card.text  # обоснования оценки в ответе тоже нет


async def test_second_employee_never_appears_in_the_first_ones_lists_on_the_site(ma: MiniApp) -> None:
    mgr, (emp, emp2) = await ma.seed_team(2)
    await ma.seed_task(emp, mgr, title="Своя задача")
    for kind in ("active", "submitted", "done", "proposed"):
        await ma.seed_task(emp2, mgr, kind=kind, title=f"Секрет Сидорова: {kind}")
    browser = await sign_in(ma, EMP)

    for url in (
        "/api/tasks", "/api/tasks?status=done", "/api/tasks?q=Секрет", f"/api/tasks?assignee_id={emp2.id}",
        "/api/tasks?scope=all", "/api/tasks?counts=1", "/api/proposals", "/api/review", "/api/employees",
        "/api/dashboard", "/api/dashboard?scope=team",
        "/api/me",
    ):
        resp = await browser.get(url)
        assert "Секрет Сидорова" not in resp.text and "Сидоров" not in resp.text, (url, resp.text[:300])
