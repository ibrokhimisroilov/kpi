"""Вход на сайт через Telegram и сессия браузера (docs/MINIAPP_SPEC.md §15).

Сайт и бот работают на одном сервере и одной базе (фикстура ``ma``): браузер начинает вход
(``POST /auth/start``), человек открывает ссылку в боте (``/start login_<код>``) и нажимает число,
которое видит на сайте; браузер, начавший вход, получает сессию (``GET /auth/status``). Здесь — правила
этого подтверждения: одноразовость, срок, привязка к браузеру и к человеку, защита запросов, выход,
блокировка. Telegram — фейковый, сети нет.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any

import pytest
from sqlalchemy import select, text

from bot.webapp import api

from .conftest import EMP, EMP2, MGR, MiniApp, Resp

STRANGER_TG = 9999
PENDING_TG = 3001
BLOCKED_TG = 3002
CSRF_HEADER = "X-KPI-CSRF"


class Browser:
    """Браузер посетителя сайта: помнит cookie, которые поставил сервер, и шлёт их обратно."""

    def __init__(self, ma: MiniApp, *, agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0 Safari/537.36") -> None:
        self.ma = ma
        self.agent = agent
        self.cookies: dict[str, str] = {}
        self.set_cookie_lines: list[str] = []

    def _remember(self, resp: Resp) -> None:
        for line in resp.headers.getall("Set-Cookie", []):
            self.set_cookie_lines.append(line)
            jar: SimpleCookie = SimpleCookie()
            jar.load(line)
            for name, morsel in jar.items():
                if morsel.value and morsel["max-age"] != "0":
                    self.cookies[name] = morsel.value
                else:
                    self.cookies.pop(name, None)

    def csrf(self) -> str:
        return self.cookies.get("kpi_csrf", "")

    async def request(self, method: str, path: str, *, csrf: bool = True, headers: dict[str, str] | None = None, **kwargs: Any) -> Resp:
        all_headers = {"User-Agent": self.agent}
        if self.cookies:
            all_headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        if csrf:  # страница сайта шлёт заголовок всегда: до входа — «1», после — знак из своей cookie
            all_headers[CSRF_HEADER] = self.csrf() or "1"
        all_headers.update(headers or {})
        # У тестового клиента одна общая «банка» cookie на всех — здесь у каждого браузера своя.
        self.ma.client.session.cookie_jar.clear()
        resp = await self.ma.request(method, path, headers=all_headers, **kwargs)
        self.ma.client.session.cookie_jar.clear()
        self._remember(resp)
        return resp

    async def get(self, path: str, **kwargs: Any) -> Resp:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Resp:
        return await self.request("POST", path, **kwargs)

    async def start(self) -> Resp:
        resp = await self.post("/auth/start")
        assert resp.status == 200, resp.data
        return resp

    async def status(self) -> Resp:
        return await self.get("/auth/status")


def token_of(started: Resp) -> str:
    """Код из ссылки на бота: то, что Telegram передаст боту в ``/start login_<код>``."""
    match = re.search(r"[?&]start=(login_[A-Za-z0-9_-]+)$", started["link"])
    assert match, started["link"]
    return match.group(1)


async def open_in_bot(ma: MiniApp, tg_id: int, started: Resp) -> Any:
    return await ma.h.send_command(tg_id, "start", token_of(started))


async def sign_in(ma: MiniApp, tg_id: int, browser: Browser | None = None) -> Browser:
    """Полный вход: сайт -> бот -> верное число -> сайт получил сессию."""
    browser = browser or Browser(ma)
    started = await browser.start()
    await open_in_bot(ma, tg_id, started)
    await ma.h.press_button(tg_id, started["code"])
    resp = await browser.status()
    assert resp.status == 200 and resp["state"] == "ok", resp.data
    return browser


@pytest.fixture
def now(clock: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """«Сейчас» заморожено и для сайта, и для бота, и для API (clock.advance сдвигает)."""
    from bot.handlers import weblogin as bot_side
    from bot.webapp import site

    for module in (site, bot_side, api):
        monkeypatch.setattr(module, "utcnow", lambda: clock.now)
    return clock


# --- Начало входа ---------------------------------------------------------------------------------------


async def test_start_gives_number_link_and_qr_and_keeps_secret_in_cookie(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    resp = await browser.start()

    assert re.fullmatch(r"\d{2}", resp["code"])
    assert resp["link"].startswith("https://t.me/test_bot?start=login_")
    assert resp["expires_in"] == 300
    rows = resp["qr"]
    assert len(rows) >= 21 and all(len(row) == len(rows) and set(row) <= {"0", "1"} for row in rows)

    # Секрет браузера — только в cookie, недоступной скриптам; в ответе и в ссылке его нет.
    secret = browser.cookies["kpi_login"]
    assert len(secret) >= 32 and secret not in resp.text and secret != token_of(resp)
    [line] = browser.set_cookie_lines
    lowered = line.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    assert resp.headers["Cache-Control"] == "no-store"


async def test_status_before_start_is_none_and_waiting_after(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    assert (await browser.status())["state"] == "none"
    started = await browser.start()
    resp = await browser.status()
    assert resp["state"] == "waiting" and resp["opened"] is False and 0 < resp["expires_in"] <= 300
    assert resp["code"] == started["code"]  # страницу могли перезагрузить — число показываем снова


async def test_database_keeps_only_hashes(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    started = await browser.start()
    login_secret = browser.cookies["kpi_login"]
    await open_in_bot(ma, EMP, started)
    await ma.h.press_button(EMP, started["code"])
    await browser.status()
    session_token = browser.cookies["kpi_session"]

    async with ma.db() as session:
        dump = ""
        for table in ("web_logins", "web_sessions"):
            rows = (await session.execute(text(f"SELECT * FROM {table}"))).all()
            assert rows, table
            dump += repr(rows)
    for secret in (login_secret, session_token, token_of(started).removeprefix("login_"), browser.csrf()):
        assert secret and secret not in dump


# --- Подтверждение в боте --------------------------------------------------------------------------------


async def test_bot_asks_to_pick_the_number_shown_on_the_site(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    started = await browser.start()
    await open_in_bot(ma, EMP, started)

    message = ma.h.last_text(EMP) or ""
    assert "Вход на сайт" in message and "Chrome, Windows" in message
    assert "По чужой просьбе вход не подтверждайте" in message
    buttons = ma.h.buttons(EMP)
    numbers = [label for label in buttons if label.isdigit()]
    assert len(numbers) == 3 and len(set(numbers)) == 3 and started["code"] in numbers
    assert any("Это не я" in label for label in buttons)
    assert (await browser.status())["opened"] is True


async def test_right_number_signs_the_browser_in_as_that_person(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = await sign_in(ma, EMP)

    assert "подтверждён" in (ma.h.last_text(EMP) or "")
    # Чисел и «Это не я» больше нет (нажать второй раз нельзя); осталась кнопка «выйти везде».
    assert [label for label in ma.h.buttons(EMP) if "всех устройствах" not in label] == []
    me = await browser.get("/api/me")
    assert me.status == 200 and me["access"] == "active" and me["role"] == "employee"
    assert me["user"]["full_name"] == "Иванов Иван Иванович"

    session_line = next(line for line in browser.set_cookie_lines if line.startswith("kpi_session="))
    lowered = session_line.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    csrf_line = next(line for line in browser.set_cookie_lines if line.startswith("kpi_csrf="))
    assert "httponly" not in csrf_line.lower()  # этот знак читает страница и возвращает в заголовке
    assert "kpi_login" not in browser.cookies  # попытка входа больше не нужна


async def test_manager_signs_in_as_manager(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = await sign_in(ma, MGR)
    me = await browser.get("/api/me")
    assert me["role"] == "manager"
    assert (await browser.get("/api/employees")).status == 200


async def test_wrong_number_cancels_the_attempt(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    started = await browser.start()
    await open_in_bot(ma, EMP, started)
    wrong = next(label for label in ma.h.buttons(EMP) if label.isdigit() and label != started["code"])
    stored = ma.h.last_message(EMP)
    right = next(button.callback_data for button in stored.buttons if button.text == started["code"])
    await ma.h.press_button(EMP, wrong)

    assert "не совпало" in (ma.h.last_text(EMP) or "")
    assert (await browser.status())["state"] == "denied"
    assert "kpi_session" not in browser.cookies
    # Верное число после отказа уже не поможет: попытка закрыта.
    await ma.h.press(EMP, right, stored.message_id)
    assert (await browser.status())["state"] in ("none", "expired", "denied")
    assert "kpi_session" not in browser.cookies


async def test_not_me_cancels_the_attempt(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    started = await browser.start()
    await open_in_bot(ma, EMP, started)
    await ma.h.press_button(EMP, "Это не я")

    assert "отменён" in (ma.h.last_text(EMP) or "")
    assert (await browser.status())["state"] == "denied"
    assert "kpi_session" not in browser.cookies


async def _speak_uzbek(ma: MiniApp, *tg_ids: int) -> None:
    from bot.services import users as users_svc

    async with ma.db() as session:
        for tg_id in tg_ids:
            await users_svc.set_lang(session, await users_svc.get_by_tg(session, tg_id), "uz")
        await session.commit()


async def test_bot_speaks_uzbek_to_a_person_who_chose_uzbek(ma: MiniApp) -> None:
    await ma.seed_team(1)
    await _speak_uzbek(ma, EMP)
    browser = Browser(ma)
    started = await browser.start()
    await open_in_bot(ma, EMP, started)

    message = ma.h.last_text(EMP) or ""
    assert "saytiga kirish" in message and "Qurilma: Chrome, Windows" in message
    assert not re.search("[А-Яа-яЁё]", message), message
    assert any("Bu men emas" in label for label in ma.h.buttons(EMP))
    await ma.h.press_button(EMP, started["code"])
    assert "tasdiqlandi" in (ma.h.last_text(EMP) or "")
    assert (await browser.status())["state"] == "ok"


@pytest.mark.parametrize("outcome", ["wrong", "not_me", "expired", "foreign", "stranger"])
async def test_every_bot_answer_about_login_has_an_uzbek_version(ma: MiniApp, now: Any, outcome: str) -> None:
    """Ни одно сообщение бота о входе не остаётся русским у того, кто выбрал узбекский (или чей Telegram
    на узбекском)."""
    await ma.seed_team(2)
    await _speak_uzbek(ma, EMP, EMP2)
    stranger = ma.h.profile(STRANGER_TG, first_name="Ali")
    ma.h.api.profiles[STRANGER_TG] = stranger.model_copy(update={"language_code": "uz"})
    browser = Browser(ma)
    started = await browser.start()
    reader = EMP
    if outcome == "stranger":
        reader = STRANGER_TG
        await open_in_bot(ma, STRANGER_TG, started)
    elif outcome == "foreign":
        reader = EMP2
        await open_in_bot(ma, EMP, started)
        await open_in_bot(ma, EMP2, started)
    elif outcome == "expired":
        now.advance(minutes=6)
        await open_in_bot(ma, EMP, started)
    else:
        await open_in_bot(ma, EMP, started)
        wrong = next(label for label in ma.h.buttons(EMP) if label.isdigit() and label != started["code"])
        await ma.h.press_button(EMP, wrong if outcome == "wrong" else "Bu men emas")
    message = ma.h.last_text(reader) or ""
    assert message and not re.search("[А-Яа-яЁё]", message), message


# --- Одноразовость и срок ---------------------------------------------------------------------------------


async def test_confirmation_works_once(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    started = await browser.start()
    login_cookie = browser.cookies["kpi_login"]
    await open_in_bot(ma, EMP, started)
    await ma.h.press_button(EMP, started["code"])
    assert (await browser.status())["state"] == "ok"

    # Тот же секрет попытки второй раз сессию не даёт (даже у того, кто его перехватил).
    thief = Browser(ma)
    thief.cookies["kpi_login"] = login_cookie
    resp = await thief.status()
    assert resp["state"] != "ok" and "kpi_session" not in thief.cookies
    async with ma.db() as session:
        count = (await session.execute(text("SELECT COUNT(*) FROM web_sessions"))).scalar_one()
    assert count == 1

    # И ссылка в боте второй раз не срабатывает.
    await open_in_bot(ma, EMP, started)
    assert "устарела" in (ma.h.last_text(EMP) or "")


async def test_attempt_expires_in_five_minutes(ma: MiniApp, now: Any) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    started = await browser.start()
    now.advance(minutes=5, seconds=1)

    assert (await browser.status())["state"] == "expired"
    await open_in_bot(ma, EMP, started)
    assert "устарела" in (ma.h.last_text(EMP) or "")
    assert ma.h.buttons(EMP) == []


async def test_approved_but_not_collected_in_time_gives_no_session(ma: MiniApp, now: Any) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    started = await browser.start()
    await open_in_bot(ma, EMP, started)
    await ma.h.press_button(EMP, started["code"])
    now.advance(minutes=6)

    assert (await browser.status())["state"] == "expired"
    assert "kpi_session" not in browser.cookies


async def test_number_pressed_after_expiry_is_refused(ma: MiniApp, now: Any) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    started = await browser.start()
    await open_in_bot(ma, EMP, started)
    now.advance(minutes=6)
    await ma.h.press_button(EMP, started["code"])

    assert "устарела" in (ma.h.last_text(EMP) or "")
    assert (await browser.status())["state"] == "expired"


async def test_new_attempt_after_expiry_works(ma: MiniApp, now: Any) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    await browser.start()
    now.advance(minutes=6)
    assert (await browser.status())["state"] == "expired"

    await sign_in(ma, EMP, browser)
    assert (await browser.get("/api/me"))["access"] == "active"


# --- Привязка к браузеру и к человеку -----------------------------------------------------------------------


async def test_only_the_browser_that_started_gets_the_session(ma: MiniApp) -> None:
    await ma.seed_team(1)
    mine = Browser(ma)
    started = await mine.start()
    await open_in_bot(ma, EMP, started)
    await ma.h.press_button(EMP, started["code"])

    # Чужой браузер знает ссылку и число (подсмотрел экран), но не секрет в cookie.
    other = Browser(ma)
    assert (await other.status())["state"] == "none"
    other.cookies["kpi_login"] = token_of(started).removeprefix("login_")
    assert (await other.status())["state"] in ("none", "expired")
    assert "kpi_session" not in other.cookies

    assert (await mine.status())["state"] == "ok"


async def test_link_opened_by_one_person_cannot_be_confirmed_by_another(ma: MiniApp) -> None:
    await ma.seed_team(2)
    browser = Browser(ma)
    started = await browser.start()
    await open_in_bot(ma, EMP, started)
    stored = ma.h.last_message(EMP)
    right = next(button.callback_data for button in stored.buttons if button.text == started["code"])

    # Второй человек открывает ту же ссылку — ему отказ, кнопок он не получает.
    await open_in_bot(ma, EMP2, started)
    assert "другим человеком" in (ma.h.last_text(EMP2) or "")
    assert ma.h.buttons(EMP2) == []
    # И подделанное нажатие «чужой» кнопки не подтверждает вход.
    await ma.h.press(EMP2, right, chat_id=EMP2)
    assert (await browser.status())["state"] == "waiting"

    await ma.h.press(EMP, right, stored.message_id)
    assert (await browser.status())["state"] == "ok"
    assert (await browser.get("/api/me"))["user"]["full_name"] == "Иванов Иван Иванович"


@pytest.mark.parametrize(
    ("tg_id", "needle"),
    [(STRANGER_TG, "/start"), (PENDING_TG, "Заявка на рассмотрении"), (BLOCKED_TG, "Доступ закрыт")],
    ids=["нет-в-базе", "заявка", "заблокирован"],
)
async def test_people_without_access_cannot_sign_in(ma: MiniApp, tg_id: int, needle: str) -> None:
    await ma.seed_team(1)
    await ma.seed_user(PENDING_TG, "Заявкин Пётр", status="pending")
    await ma.seed_user(BLOCKED_TG, "Блокин Иван", status="blocked")
    browser = Browser(ma)
    started = await browser.start()
    await open_in_bot(ma, tg_id, started)

    assert needle in (ma.h.last_text(tg_id) or "")
    assert not any(label.isdigit() for label in ma.h.buttons(tg_id))
    assert (await browser.status())["state"] == "denied"
    assert "kpi_session" not in browser.cookies


async def test_unknown_or_garbage_code_in_bot(ma: MiniApp) -> None:
    await ma.seed_team(1)
    for payload in ("login_" + "A" * 22, "login_", "login_../../x", "login_" + "я" * 5):
        await ma.h.send_command(EMP, "start", payload)
        assert "устарела" in (ma.h.last_text(EMP) or ""), payload


# --- Защита запросов --------------------------------------------------------------------------------------


async def test_session_cookie_alone_is_not_enough(ma: MiniApp) -> None:
    """Запрос с чужого сайта принесёт cookie, но не знак из страницы: сервер такой запрос отклоняет."""
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr, accepted=False)
    browser = await sign_in(ma, EMP)

    for method, path in (("GET", "/api/me"), ("GET", "/api/tasks"), ("POST", f"/api/tasks/{task_id}/accept")):
        resp = await browser.request(method, path, csrf=False)
        assert resp.status == 403 and resp.code == "csrf", (method, path, resp.data)
        resp = await browser.request(method, path, csrf=False, headers={CSRF_HEADER: "0" * 64})
        assert resp.status == 403 and resp.code == "csrf", (method, path)
    assert (await ma.task(task_id)).accepted_at is None


@pytest.mark.parametrize(
    "headers",
    [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}],
    ids=["чужой-origin", "cross-site", "same-site"],
)
async def test_requests_from_other_sites_are_refused(ma: MiniApp, headers: dict[str, str]) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr, accepted=False)
    browser = await sign_in(ma, EMP)

    resp = await browser.post(f"/api/tasks/{task_id}/accept", headers=headers)
    assert resp.status == 403 and resp.code == "csrf"
    assert (await ma.task(task_id)).accepted_at is None
    for path in ("/auth/start", "/auth/logout"):
        resp = await browser.post(path, headers=headers)
        assert resp.status == 403 and resp.code == "csrf", path
    assert (await browser.get("/api/me")).status == 200  # сессия цела: чужой сайт её не завершил


async def test_login_routes_need_the_page_header(ma: MiniApp) -> None:
    """Форма или картинка на чужом сайте заголовок поставить не может — без него вход даже не начинается."""
    await ma.seed_team(1)
    browser = Browser(ma)
    for method, path in (("POST", "/auth/start"), ("GET", "/auth/status"), ("POST", "/auth/logout")):
        resp = await browser.request(method, path, csrf=False)
        assert resp.status == 403 and resp.code == "csrf", (method, path)
    assert "kpi_login" not in browser.cookies


async def test_own_page_requests_pass(ma: MiniApp) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr, accepted=False)
    browser = await sign_in(ma, EMP)
    host = f"{ma.client.host}:{ma.client.port}"

    resp = await browser.post(
        f"/api/tasks/{task_id}/accept", headers={"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"}
    )
    assert resp.status == 200, resp.data
    assert (await ma.task(task_id)).accepted_at is not None


async def test_too_many_starts_are_limited(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    for _ in range(10):  # целый отдел за одним адресом входит утром одновременно — это можно
        await browser.start()
    resp = await browser.post("/auth/start")
    assert resp.status == 429 and resp.code == "rate_limited" and resp["retry_after"] > 0


# --- Права — те же, что в приложении ------------------------------------------------------------------------


async def test_employee_session_cannot_do_manager_actions_or_see_foreign_tasks(ma: MiniApp) -> None:
    mgr, (emp, emp2) = await ma.seed_team(2)
    own = await ma.seed_task(emp, mgr)
    foreign = await ma.seed_task(emp2, mgr, title="Чужая задача")
    submitted = await ma.seed_task(emp2, mgr, kind="submitted", title="Чужой отчёт")
    sub_id = (await ma.task(submitted)).submissions[0].id
    browser = await sign_in(ma, EMP)

    assert (await browser.get(f"/api/tasks/{own}")).status == 200
    resp = await browser.get(f"/api/tasks/{foreign}")
    assert resp.status == 403 and resp.code == "forbidden"
    listed = await browser.get("/api/tasks")
    assert [item["id"] for item in listed["items"]] == [own]
    for method, path in (
        ("POST", "/api/tasks"),
        ("PATCH", f"/api/tasks/{own}"),
        ("POST", f"/api/tasks/{own}/cancel"),
        ("GET", "/api/review"),
        ("POST", f"/api/submissions/{sub_id}/confirm"),
        ("GET", "/api/employees"),
        ("POST", "/api/export"),
    ):
        resp = await browser.request(method, path)
        assert resp.status == 403 and resp.code == "forbidden", (method, path, resp.data)
    assert (await ma.task(submitted)).status.value == "submitted"


async def test_demoted_manager_loses_manager_rights_at_once(ma: MiniApp) -> None:
    await ma.seed_team(1)
    mgr2 = await ma.seed_user(1002, "Орлова Мария Петровна", role="manager")
    browser = await sign_in(ma, 1002)
    assert (await browser.get("/api/employees")).status == 200

    from bot.db.models import Role, User

    async with ma.db() as session:
        user = await session.get(User, mgr2.id)
        user.role = Role.EMPLOYEE
        await session.commit()
    resp = await browser.get("/api/employees")
    assert resp.status == 403 and resp.code == "forbidden"


# --- Выход, блокировка, срок сессии -------------------------------------------------------------------------


async def test_logout_ends_the_site_session_but_not_telegram(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = await sign_in(ma, EMP)
    old = dict(browser.cookies)

    resp = await browser.post("/auth/logout")
    assert resp.status == 200 and resp["ok"] is True
    assert "kpi_session" not in browser.cookies and "kpi_csrf" not in browser.cookies

    # Старая cookie (её могли скопировать до выхода) больше не действует.
    stale = Browser(ma)
    stale.cookies.update(old)
    resp = await stale.get("/api/me")
    assert resp.status == 401 and resp.code == "web_session"
    # Приложение в Telegram у того же человека работает как раньше.
    assert (await ma.get("/api/me", as_=EMP))["access"] == "active"
    # Выход закрыл сессию только этого человека.
    assert (await ma.get("/api/me", as_=MGR))["access"] == "active"


async def test_signing_in_again_in_the_same_browser_replaces_the_old_session(ma: MiniApp) -> None:
    """В браузере сидел один человек, вошёл другой: прежняя сессия закрывается, а не остаётся жить в базе."""
    await ma.seed_team(1)
    browser = await sign_in(ma, EMP)
    old = dict(browser.cookies)
    await sign_in(ma, MGR, browser)

    assert (await browser.get("/api/me"))["role"] == "manager"
    stale = Browser(ma)
    stale.cookies.update(old)
    resp = await stale.get("/api/me")
    assert resp.status == 401 and resp.code == "web_session"
    async with ma.db() as session:
        assert (await session.execute(text("SELECT COUNT(*) FROM web_sessions"))).scalar_one() == 1


async def test_session_cookie_outlives_the_server_side_term(ma: MiniApp) -> None:
    """Срок сессии держит сервер (30 дней без обращений, работа продлевает); cookie в браузере живёт дольше —
    иначе человека, который работает каждый день, выбрасывало бы ровно через 30 дней после входа."""
    await ma.seed_team(1)
    browser = await sign_in(ma, EMP)
    line = next(line for line in browser.set_cookie_lines if line.startswith("kpi_session="))
    max_age = int(re.search(r"Max-Age=(\d+)", line, re.IGNORECASE).group(1))
    assert max_age >= 300 * 24 * 3600


async def test_logout_of_one_browser_keeps_the_other(ma: MiniApp) -> None:
    await ma.seed_team(1)
    laptop = await sign_in(ma, EMP)
    phone = await sign_in(ma, EMP, Browser(ma, agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1"))
    await laptop.post("/auth/logout")
    assert (await phone.get("/api/me")).status == 200


async def test_blocked_person_loses_the_site_at_once(ma: MiniApp) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr)
    browser = await sign_in(ma, EMP)
    assert (await browser.get(f"/api/tasks/{task_id}")).status == 200

    # Статус «заблокирован» появился в базе (каким угодно путём) — следующий же запрос получает отказ:
    # сервер проверяет статус на каждом запросе, как у Mini App.
    from bot.db.models import User, UserStatus

    async with ma.db() as session:
        (await session.get(User, emp.id)).status = UserStatus.BLOCKED
        await session.commit()

    resp = await browser.get(f"/api/tasks/{task_id}")
    assert resp.status == 403 and resp.code == "blocked"
    resp = await browser.post(f"/api/tasks/{task_id}/accept")
    assert resp.status == 403 and resp.code == "blocked"
    me = await browser.get("/api/me")
    assert me.status == 200 and me["access"] == "blocked" and me["role"] is None
    # И войти заново заблокированный не может.
    other = Browser(ma)
    started = await other.start()
    await open_in_bot(ma, EMP, started)
    assert "Доступ закрыт" in (ma.h.last_text(EMP) or "")
    assert (await other.status())["state"] == "denied"


async def test_blocking_in_the_bot_ends_site_sessions_and_unblocking_does_not_revive_them(ma: MiniApp) -> None:
    """Начальник блокирует сотрудника обычным путём — его сессии сайта удаляются сразу. Если cookie успели
    украсть, после разблокировки она не оживёт: человек входит заново."""
    mgr, (emp,) = await ma.seed_team(1)
    task_id = await ma.seed_task(emp, mgr)
    browser = await sign_in(ma, EMP)
    boss = await sign_in(ma, MGR)
    from bot.services import users as users_svc

    async with ma.db() as session:
        await users_svc.block_user(session, emp.id, await users_svc.get_by_tg(session, MGR))
        await session.commit()
    resp = await browser.get(f"/api/tasks/{task_id}")
    assert resp.status == 401 and resp.code == "web_session"
    async with ma.db() as session:
        left = (await session.execute(text("SELECT COUNT(*) FROM web_sessions WHERE user_id = :u"), {"u": emp.id})).scalar_one()
    assert left == 0
    assert (await boss.get("/api/me")).status == 200  # чужие сессии не тронуты

    async with ma.db() as session:
        await users_svc.unblock_user(session, emp.id, await users_svc.get_by_tg(session, MGR))
        await session.commit()
    resp = await browser.get(f"/api/tasks/{task_id}")
    assert resp.status == 401 and resp.code == "web_session"
    await sign_in(ma, EMP, browser)  # новый вход — обычным путём
    assert (await browser.get(f"/api/tasks/{task_id}")).status == 200


async def test_bot_button_ends_all_site_sessions_of_that_person(ma: MiniApp) -> None:
    """Подтвердил вход по ошибке или потерял ноутбук — кнопка под сообщением бота закрывает все входы на сайт."""
    await ma.seed_team(1)
    laptop = await sign_in(ma, EMP)
    phone = await sign_in(ma, EMP, Browser(ma, agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1"))
    boss = await sign_in(ma, MGR)

    await ma.h.press_button(EMP, "всех устройствах")
    assert "завершены" in (ma.h.last_text(EMP) or "")
    assert ma.h.buttons(EMP) == []
    for browser in (laptop, phone):
        resp = await browser.get("/api/me")
        assert resp.status == 401 and resp.code == "web_session"
    assert (await boss.get("/api/me")).status == 200
    assert (await ma.get("/api/me", as_=EMP))["access"] == "active"  # приложение в Telegram не тронуто


async def test_session_cannot_be_kept_alive_forever(ma: MiniApp, now: Any) -> None:
    """Работа продлевает сессию, но не бесконечно: раз в 90 дней вход подтверждается заново."""
    await ma.seed_team(1)
    browser = await sign_in(ma, EMP)
    for _ in range(4):
        now.advance(days=20)
        assert (await browser.get("/api/me")).status == 200
    now.advance(days=11)  # 91-й день
    resp = await browser.get("/api/me")
    assert resp.status == 401 and resp.code == "web_session"


async def test_starting_again_cancels_the_previous_attempt(ma: MiniApp) -> None:
    """«Войти» нажали второй раз (или открыли вторую вкладку): прежняя попытка закрыта — её кнопки в боте
    больше ничего не подтверждают, чтобы человек не подтвердил вход, который никто не получит."""
    await ma.seed_team(1)
    browser = Browser(ma)
    first = await browser.start()
    await open_in_bot(ma, EMP, first)
    stored = ma.h.last_message(EMP)
    old_button = next(button.callback_data for button in stored.buttons if button.text == first["code"])

    second = await browser.start()
    await ma.h.press(EMP, old_button, stored.message_id)
    assert "устарела" in (ma.h.last_text(EMP) or "")
    assert (await browser.status())["state"] == "waiting"
    await open_in_bot(ma, EMP, second)
    await ma.h.press_button(EMP, second["code"])
    assert (await browser.status())["state"] == "ok"


async def test_cancel_closes_the_attempt(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = Browser(ma)
    started = await browser.start()
    resp = await browser.post("/auth/cancel")
    assert resp.status == 200 and resp["ok"] is True and "kpi_login" not in browser.cookies
    assert (await browser.status())["state"] == "none"
    await open_in_bot(ma, EMP, started)
    assert "устарела" in (ma.h.last_text(EMP) or "")
    assert (await browser.request("POST", "/auth/cancel", csrf=False)).status == 403


async def test_forged_button_with_strange_number_is_harmless(ma: MiniApp) -> None:
    """Кнопку «нажали» подделанным клиентом с нечисловым значением — без ошибки сервера, вход не выдан."""
    from bot.ui.callbacks import WebLoginCB

    await ma.seed_team(1)
    browser = Browser(ma)
    started = await browser.start()
    await open_in_bot(ma, EMP, started)
    stored = ma.h.last_message(EMP)
    login_id = WebLoginCB.unpack(stored.buttons[0].callback_data).login_id
    await ma.h.press(EMP, WebLoginCB(login_id=login_id, pick="я").pack(), stored.message_id)
    assert (await browser.status())["state"] == "denied"
    assert "kpi_session" not in browser.cookies


async def test_origin_is_checked_against_the_public_address(ma: MiniApp) -> None:
    """Свой адрес — тот, что в настройках (хостинг может подменить Host): запрос с него проходит, с того же
    имени по незащищённому http — нет."""
    from test_web import BASE, make_settings

    await ma.seed_team(1)
    ma.ctx.settings = make_settings()  # webhook, https
    resp = await ma.post("/auth/start", headers={CSRF_HEADER: "1", "Origin": BASE, "Sec-Fetch-Site": "same-origin"})
    assert resp.status == 200, resp.data
    resp = await ma.post("/auth/start", headers={CSRF_HEADER: "1", "Origin": BASE.replace("https://", "http://")})
    assert resp.status == 403 and resp.code == "csrf"
    resp = await ma.post("/auth/start", headers={CSRF_HEADER: "1", "Origin": "null"})
    assert resp.status == 403 and resp.code == "csrf"


async def test_only_waiting_attempts_count_towards_the_common_limit(ma: MiniApp, now: Any) -> None:
    from bot.services import weblogin

    await ma.seed_team(1)
    await sign_in(ma, EMP)  # использованная попытка
    denied = Browser(ma)
    started = await denied.start()
    await open_in_bot(ma, EMP, started)
    await ma.h.press_button(EMP, "Это не я")
    await Browser(ma).start()  # ждущая
    async with ma.db() as session:
        assert await weblogin.pending_count(session, now.now) == 1


async def test_blocked_between_opening_and_pressing_the_number(ma: MiniApp) -> None:
    mgr, (emp,) = await ma.seed_team(1)
    browser = Browser(ma)
    started = await browser.start()
    await open_in_bot(ma, EMP, started)
    from bot.services import users as users_svc

    async with ma.db() as session:
        await users_svc.block_user(session, emp.id, await users_svc.get_by_tg(session, MGR))
        await session.commit()
    await ma.h.press_button(EMP, started["code"])

    assert (await browser.status())["state"] != "ok"
    assert "kpi_session" not in browser.cookies


async def test_session_lives_thirty_days_and_is_extended_by_use(ma: MiniApp, now: Any) -> None:
    await ma.seed_team(1)
    browser = await sign_in(ma, EMP)
    now.advance(days=20)
    assert (await browser.get("/api/me")).status == 200  # работа продлевает сессию
    now.advance(days=20)
    assert (await browser.get("/api/me")).status == 200
    now.advance(days=31)
    resp = await browser.get("/api/me")
    assert resp.status == 401 and resp.code == "web_session"


async def test_unknown_session_cookie_is_401(ma: MiniApp) -> None:
    from bot.webapp import site

    await ma.seed_team(1)
    browser = Browser(ma)
    browser.cookies["kpi_session"] = "x" * 43
    browser.cookies["kpi_csrf"] = site.csrf_for("x" * 43)
    resp = await browser.get("/api/me")
    assert resp.status == 401 and resp.code == "web_session"


# --- Приложение в Telegram не изменилось --------------------------------------------------------------------


async def test_init_data_still_works_and_wins_over_cookie(ma: MiniApp) -> None:
    await ma.seed_team(1)
    browser = await sign_in(ma, EMP)
    # Заголовок Telegram есть — cookie сайта не читается вовсе (и знак страницы не нужен).
    resp = await browser.get("/api/me", csrf=False, headers=ma.auth(MGR))
    assert resp.status == 200 and resp["role"] == "manager"
    resp = await browser.get("/api/me", csrf=False, headers={"X-Telegram-Init-Data": "broken"})
    assert resp.status == 401 and resp.code == "auth_invalid"


async def test_without_any_identity_api_is_401_auth_missing(ma: MiniApp) -> None:
    await ma.seed_team(1)
    resp = await ma.get("/api/me")
    assert resp.status == 401 and resp.code == "auth_missing"


async def test_site_can_be_switched_off(ma: MiniApp) -> None:
    """WEBSITE_ENABLED=0: входа на сайт нет, cookie сайта не читаются; приложение в Telegram работает."""
    await ma.seed_team(1)
    browser = await sign_in(ma, EMP)
    ma.ctx.settings = type(ma.ctx.settings)(_env_file=None, website_enabled=False)

    resp = await browser.get("/api/me")
    assert resp.status == 401 and resp.code == "auth_missing"
    resp = await Browser(ma).post("/auth/start")
    assert resp.status == 404
    assert (await ma.get("/api/me", as_=EMP))["access"] == "active"


# --- Уборка -----------------------------------------------------------------------------------------------


async def test_old_attempts_and_sessions_are_removed(ma: MiniApp, now: Any) -> None:
    from bot.db.models import WebLogin, WebSession

    await ma.seed_team(1)
    await sign_in(ma, EMP)
    await Browser(ma).start()
    now.advance(days=40)
    await Browser(ma).start()  # новая попытка заодно убирает просроченное

    async with ma.db() as session:
        logins = (await session.scalars(select(WebLogin))).all()
        sessions = (await session.scalars(select(WebSession))).all()
    assert len(logins) == 1 and sessions == []
    assert all(isinstance(item.expires_at, datetime) and item.expires_at > now.now - timedelta(days=1) for item in logins)
