"""Сортировка списка задач — необязательный параметр ``sort`` у ``GET /api/tasks`` (docs/MINIAPP_SPEC.md §15.6).

Нужна таблицам сайта на большом экране: сортирует сервер, поэтому порядок честный для всего списка, а не
только для загруженной страницы. Без параметра список приходит в прежнем порядке — приложение в Telegram
его не передаёт.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from bot.db.models import Priority, Task
from bot.utils.dates import utcnow

from .conftest import EMP, EMP2, MGR, MiniApp


async def seed(ma: MiniApp) -> dict[str, int]:
    """Пять задач двух сотрудников с разными сроком, названием, весом, приоритетом и оценкой."""
    mgr, (ivanov, sidorov) = await ma.seed_team(2)
    now = utcnow()
    spec = [
        ("b", ivanov, "Бюджет", 3, 20, Priority.LOW, None, "active"),
        ("a", sidorov, "Аудит", 1, 40, Priority.HIGH, None, "active"),
        ("v", ivanov, "Выверка", 5, 10, Priority.MEDIUM, None, "active"),
        ("g", sidorov, "График", 2, 30, Priority.HIGH, 120.0, "done"),
        ("d", ivanov, "Договоры", 4, 5, Priority.LOW, 80.0, "done"),
    ]
    ids: dict[str, int] = {}
    for key, assignee, title, days, weight, priority, score, kind in spec:
        task_id = await ma.seed_task(
            assignee, mgr, kind=kind, title=title, deadline=now + timedelta(days=days), weight=weight,
            final_score=score or 110.0,
        )
        async with ma.db() as session:
            (await session.get(Task, task_id)).priority = priority
            await session.commit()
        ids[key] = task_id
    return ids


async def order(ma: MiniApp, ids: dict[str, int], *, as_: int = MGR, **params: Any) -> str:
    params.setdefault("status", "all")
    resp = await ma.get("/api/tasks", as_=as_, params=params)
    assert resp.status == 200, resp.data
    names = {task_id: key for key, task_id in ids.items()}
    return "".join(names[item["id"]] for item in resp["items"])


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("deadline", "agbdv"),
        ("-deadline", "vdbga"),
        ("title", "abvgd"),
        ("-title", "dgvba"),
        ("weight", "dvbga"),
        ("-weight", "agbvd"),
        ("id", "bavgd"),
        ("-id", "dgvab"),
    ],
)
async def test_sort_by_a_column(ma: MiniApp, sort: str, expected: str) -> None:
    ids = await seed(ma)
    assert await order(ma, ids, sort=sort) == expected


async def test_sort_by_priority_puts_high_first_and_low_last(ma: MiniApp) -> None:
    ids = await seed(ma)
    assert await order(ma, ids, sort="priority") == "agvbd"   # высокий (по сроку), средний, низкий (по сроку)
    assert await order(ma, ids, sort="-priority") == "bdvag"


async def test_sort_by_score_keeps_unscored_tasks_at_the_end(ma: MiniApp) -> None:
    ids = await seed(ma)
    assert (await order(ma, ids, sort="score")).startswith("dg")   # 80 %, 120 %, затем без оценки
    assert (await order(ma, ids, sort="-score")).startswith("gd")
    assert set((await order(ma, ids, sort="-score"))[2:]) == set("abv")


async def test_sort_by_assignee(ma: MiniApp) -> None:
    ids = await seed(ma)
    assert await order(ma, ids, sort="assignee") == "bvdag"    # Иванов (по сроку), затем Сидоров
    assert (await order(ma, ids, sort="-assignee"))[:2] in ("ag", "ga")


async def test_without_sort_the_order_is_the_old_one(ma: MiniApp) -> None:
    ids = await seed(ma)
    plain = await order(ma, ids)
    assert plain == "abvdg"  # незавершённые по сроку, затем выполненные — недавние сверху
    assert await order(ma, ids, sort="") == plain


async def test_sort_goes_through_pages_and_search(ma: MiniApp) -> None:
    ids = await seed(ma)
    first = await order(ma, ids, sort="-weight", limit=2, page=0)
    second = await order(ma, ids, sort="-weight", limit=2, page=1)
    third = await order(ma, ids, sort="-weight", limit=2, page=2)
    assert first + second + third == "agbvd"
    # Поиск идёт по тем же строкам в том же порядке («г» есть в «График», «Договоры», «Бюджет»…).
    found = await order(ma, ids, sort="title", q="о")
    assert found == "".join(key for key in "abvgd" if key in found) and len(found) >= 2


async def test_employee_sorts_only_own_tasks(ma: MiniApp) -> None:
    ids = await seed(ma)
    assert await order(ma, ids, as_=EMP, sort="-weight") == "bvd"
    assert await order(ma, ids, as_=EMP2, sort="title") == "ag"
    resp = await ma.get("/api/tasks", as_=EMP, params={"scope": "all", "sort": "title"})
    assert resp.status == 403


@pytest.mark.parametrize("bad", ["price", "title;drop", "--title", "DEADLINE", "-", "title,weight"])
async def test_unknown_sort_is_a_bad_request(ma: MiniApp, bad: str) -> None:
    await seed(ma)
    resp = await ma.get("/api/tasks", as_=MGR, params={"sort": bad})
    assert resp.status == 400 and resp.code == "bad_request" and "sort" in (resp.error or "")
