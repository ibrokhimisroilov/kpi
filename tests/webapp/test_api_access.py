"""Матрица доступа по всем маршрутам API (docs/MINIAPP_SPEC.md §6.2, §8.1, §12.2 test_access)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from bot.webapp import api

from .conftest import EMP, EMP2, MGR, MiniApp

# Кто может вызывать маршрут: all — любой с валидным initData; ME — активный начальник и сотрудник;
# M — начальник; E — сотрудник; A — исполнитель задачи.
ROLES: dict[tuple[str, str], str] = {
    ("GET", "/api/me"): "all",
    ("GET", "/api/tasks"): "ME",
    ("POST", "/api/tasks"): "M",
    ("GET", "/api/tasks/{task_id}"): "ME",
    ("PATCH", "/api/tasks/{task_id}"): "M",
    ("POST", "/api/tasks/{task_id}/accept"): "A",
    ("POST", "/api/tasks/{task_id}/cancel"): "M",
    ("POST", "/api/tasks/{task_id}/submit"): "A",
    ("POST", "/api/tasks/{task_id}/approve"): "M",
    ("POST", "/api/tasks/{task_id}/reject"): "M",
    ("POST", "/api/ai/formulate"): "ME",
    ("GET", "/api/review"): "M",
    ("POST", "/api/submissions/{sub_id}/confirm"): "M",
    ("POST", "/api/submissions/{sub_id}/score"): "M",
    ("POST", "/api/submissions/{sub_id}/revise"): "M",
    ("POST", "/api/submissions/{sub_id}/rework"): "M",
    ("POST", "/api/submissions/{sub_id}/files"): "M",
    ("GET", "/api/proposals"): "M",
    ("POST", "/api/proposals"): "E",
    ("GET", "/api/dashboard"): "ME",
    ("GET", "/api/users/{user_id}/kpi"): "ME",
    ("GET", "/api/employees"): "M",
    ("GET", "/api/employees/{user_id}/weight-load"): "M",
    ("POST", "/api/export"): "M",
}
PENDING_TG = 3001
BLOCKED_TG = 3002
HALF_REGISTERED_TG = 3003
STRANGER_TG = 9999


def test_roles_cover_all_routes() -> None:
    assert sorted(ROLES) == sorted(api.ROUTES)
    assert len(set(api.ROUTES)) == len(api.ROUTES)
    assert all(path.startswith("/api/") for _, path in api.ROUTES)


@dataclass
class World:
    mgr: Any
    emp: Any
    emp2: Any
    task_id: int  # активная задача Иванова
    sub_id: int  # сдача Иванова на проверке (с файлом)
    proposal_id: int  # поручение Иванова


async def build_world(ma: MiniApp) -> World:
    mgr, (emp, emp2) = await ma.seed_team(2)
    await ma.seed_user(PENDING_TG, "Заявкин Пётр", status="pending")
    await ma.seed_user(BLOCKED_TG, "Блокин Иван", status="blocked")
    await ma.seed_user(HALF_REGISTERED_TG, "", status="pending")
    task_id = await ma.seed_task(emp, mgr)
    submitted = await ma.seed_task(emp, mgr, kind="submitted", title="Отчёт", files=1)
    proposal_id = await ma.seed_task(emp, mgr, kind="proposed", title="Поручение")
    sub_id = (await ma.task(submitted)).submissions[0].id
    return World(mgr, emp, emp2, task_id, sub_id, proposal_id)


def fill(path: str, w: World, *, task_id: int | None = None, sub_id: int | None = None, user_id: int | None = None) -> str:
    return (
        path.replace("{task_id}", str(task_id if task_id is not None else w.task_id))
        .replace("{sub_id}", str(sub_id if sub_id is not None else w.sub_id))
        .replace("{user_id}", str(user_id if user_id is not None else w.emp.id))
    )


async def call(ma: MiniApp, method: str, path: str, *, as_: int | None, **kwargs: Any) -> Any:
    return await ma.request(method, path, as_=as_, **kwargs)


async def test_without_init_data_every_route_is_401(ma: MiniApp) -> None:
    w = await build_world(ma)
    for method, template in api.ROUTES:
        resp = await call(ma, method, fill(template, w), as_=None)
        assert resp.status == 401, (method, template)
        assert resp.code == "auth_missing" and resp.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("tg_id", "code"),
    [(STRANGER_TG, "not_registered"), (HALF_REGISTERED_TG, "not_registered"), (PENDING_TG, "pending"), (BLOCKED_TG, "blocked")],
    ids=["нет-в-базе", "анкета-не-заполнена", "заявка", "заблокирован"],
)
async def test_inactive_users(ma: MiniApp, tg_id: int, code: str) -> None:
    w = await build_world(ma)
    for method, template in api.ROUTES:
        resp = await call(ma, method, fill(template, w), as_=tg_id)
        if template == "/api/me":
            assert resp.status == 200
            assert resp["access"] == ("unregistered" if code == "not_registered" else code)
            assert resp["message"] and resp["role"] is None and resp["counts"] is None
        else:
            assert resp.status == 403, (method, template, resp.status)
            assert resp.code == code
    texts = {
        "not_registered": api.NOT_REGISTERED,
        "pending": api.TXT_PENDING,
        "blocked": api.TXT_BLOCKED,
    }
    resp = await call(ma, "GET", "/api/tasks", as_=tg_id)
    assert resp.error == texts[code]


async def test_employee_gets_403_on_manager_routes_even_with_empty_body(ma: MiniApp) -> None:
    w = await build_world(ma)
    for (method, template), role in ROLES.items():
        if role != "M":
            continue
        resp = await call(ma, method, fill(template, w), as_=EMP)
        assert resp.status == 403, (method, template, resp.status, resp.data)
        assert resp.code == "forbidden" and resp.error == api.MANAGER_ONLY


async def test_manager_passes_role_checks(ma: MiniApp) -> None:
    w = await build_world(ma)
    for (method, template), role in ROLES.items():
        if role not in ("M", "ME", "all"):
            continue
        resp = await call(ma, method, fill(template, w), as_=MGR)
        assert resp.status not in (401, 403), (method, template, resp.status, resp.data)


async def test_manager_cannot_propose_and_is_not_assignee(ma: MiniApp) -> None:
    w = await build_world(ma)
    resp = await call(ma, "POST", "/api/proposals", as_=MGR, json={})
    assert resp.status == 403 and resp.error == api.EMPLOYEE_ONLY
    for action in ("accept", "submit"):
        resp = await call(ma, "POST", f"/api/tasks/{w.task_id}/{action}", as_=MGR)
        assert resp.status == 403 and resp.error == api.NO_RIGHTS


async def test_foreign_task_of_employee_is_403(ma: MiniApp) -> None:
    w = await build_world(ma)
    for method, path in [
        ("GET", f"/api/tasks/{w.task_id}"),
        ("POST", f"/api/tasks/{w.task_id}/accept"),
        ("POST", f"/api/tasks/{w.task_id}/submit"),
        ("GET", f"/api/users/{w.emp.id}/kpi"),
    ]:
        resp = await call(ma, method, path, as_=EMP2)
        assert resp.status == 403, (method, path)
        assert resp.code == "forbidden"
    resp = await call(ma, "GET", "/api/tasks", as_=EMP2, params={"scope": "all"})
    assert resp.status == 403
    resp = await call(ma, "GET", "/api/tasks", as_=EMP2, params={"scope": "emp", "user_id": w.emp.id})
    assert resp.status == 403


async def test_own_objects_are_allowed_for_employee(ma: MiniApp) -> None:
    w = await build_world(ma)
    assert (await call(ma, "GET", f"/api/tasks/{w.task_id}", as_=EMP)).status == 200
    assert (await call(ma, "GET", f"/api/users/{w.emp.id}/kpi", as_=EMP)).status == 200
    assert (await call(ma, "GET", "/api/dashboard", as_=EMP)).status == 200


@pytest.mark.parametrize("bad_id", ["999999", str(2**63 + 5), "abc", "-1", "0", "1e3"])
async def test_unknown_ids_are_404(ma: MiniApp, bad_id: str) -> None:
    w = await build_world(ma)
    for method, template in api.ROUTES:
        if "{" not in template:
            continue
        path = fill(template, w, task_id=bad_id, sub_id=bad_id, user_id=bad_id)  # type: ignore[arg-type]
        resp = await call(ma, method, path, as_=MGR if ROLES[(method, template)] != "A" else EMP)
        assert resp.status == 404, (method, template, resp.status, resp.data)
        assert resp.code == "not_found"


async def test_unknown_route_and_wrong_method(ma: MiniApp) -> None:
    await build_world(ma)
    resp = await call(ma, "GET", "/api/nothing-here", as_=MGR)
    assert resp.status == 404 and resp.code == "not_found" and resp.error == api.NOT_FOUND
    assert resp.headers["Content-Type"].startswith("application/json")
    resp = await call(ma, "GET", "/api/nothing-here", as_=None)
    assert resp.status == 401
    resp = await call(ma, "DELETE", "/api/tasks", as_=MGR)
    assert resp.status == 405 and resp.code == "method_not_allowed" and resp.error == api.METHOD_NOT_ALLOWED
    resp = await call(ma, "PUT", "/api/me", as_=MGR)
    assert resp.status == 405


async def test_every_api_answer_has_version_and_no_store(ma: MiniApp) -> None:
    await build_world(ma)
    for resp in (
        await call(ma, "GET", "/api/me", as_=MGR),
        await call(ma, "GET", "/api/me", as_=None),
        await call(ma, "GET", "/api/zzz", as_=MGR),
    ):
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["X-App-Version"] == ma.ctx.static.version and len(ma.ctx.static.version) == 12


async def test_body_must_be_json_object_and_known_fields(ma: MiniApp) -> None:
    w = await build_world(ma)
    for body in ("[1, 2]", "не json", '"строка"', "1"):
        resp = await call(ma, "POST", f"/api/tasks/{w.task_id}/cancel", as_=MGR, data=body,
                          headers={"Content-Type": "application/json"})
        assert resp.status == 400 and resp.code == "bad_request" and resp.error == api.BAD_JSON
    resp = await call(ma, "POST", f"/api/tasks/{w.task_id}/cancel", as_=MGR, json={"reason": "x", "foo": 1})
    assert resp.status == 400 and resp.error == "Неизвестное поле «foo»"


async def test_too_large_json_is_413(ma: MiniApp) -> None:
    w = await build_world(ma)
    resp = await call(ma, "POST", f"/api/tasks/{w.task_id}/cancel", as_=MGR, json={"reason": "x" * (1024 * 1024 + 10)})
    assert resp.status == 413 and resp.code == "too_large"
