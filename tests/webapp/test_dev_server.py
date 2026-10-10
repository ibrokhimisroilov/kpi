"""Dev-сервер приложения для проверки в браузере (bot/webapp/dev.py, docs/MINIAPP_SPEC.md §4.6, §12.2).

Сети нет: Telegram — FakeSession (с эхом в список), база — фикстура ``engine`` (SQLite или PostgreSQL).
Окружение процесса тесты не меняют: ``main()`` проверяется только на отказах (до выставления окружения).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.ai.evaluate import RULES_PREFIX
from bot.config import Settings
from bot.db.base import make_sessionmaker
from bot.db.models import (
    AttachmentKind,
    ReviewDecision,
    Role,
    Submission,
    Task,
    TaskEvent,
    TaskSource,
    TaskStatus,
    User,
    UserStatus,
)
from bot.services import periods
from bot.webapp import dev
from bot.webapp.auth import validate_init_data

NOW = datetime(2026, 10, 7, 9, 0)  # ср 07.10.2026 14:00 по Ташкенту


def dev_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "bot_token": dev.DEV_TOKEN,
        "run_mode": "polling",
        "webapp_enabled": True,
        "webapp_debug": True,
        "ai_provider": "none",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@asynccontextmanager
async def dev_client(engine: AsyncEngine, echo: list[str] | None = None) -> AsyncIterator[tuple[TestClient, Any]]:
    sessionmaker = make_sessionmaker(engine)
    session = dev.make_fake_session(echo.append if echo is not None else None)
    bot = Bot(dev.DEV_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    app = dev.build_dev_app(bot=bot, sessionmaker=sessionmaker, settings=dev_settings())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, session
    finally:
        await client.close()
        await bot.session.close()


async def add_user(engine: AsyncEngine, tg_id: int, full_name: str, **fields: Any) -> None:
    async with make_sessionmaker(engine)() as session:
        session.add(
            User(
                tg_id=tg_id,
                full_name=full_name,
                role=fields.get("role", Role.EMPLOYEE),
                status=fields.get("status", UserStatus.ACTIVE),
                position=fields.get("position"),
            )
        )
        await session.commit()


# --- Окружение и выбор базы --------------------------------------------------------------------------------


def test_dev_environment_without_secrets(tmp_path: Path) -> None:
    db = tmp_path / "dev.db"
    env = dev.dev_environment(db)
    assert env["RUN_MODE"] == "polling" and env["WEBAPP_ENABLED"] == "1" and env["WEBAPP_DEBUG"] == "1"
    assert env["DATABASE_URL"] == f"sqlite+aiosqlite:///{db.as_posix()}"
    assert env["BOT_TOKEN"] == dev.DEV_TOKEN and env["AI_PROVIDER"] == "none"
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "CLOUDFLARE_API_TOKEN", "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
                "DATABASE_PASSWORD", "PUBLIC_URL", "RENDER_EXTERNAL_URL"):
        assert env[key] == "", key
    # Настройки из такого окружения: отладка действует, кнопок и webhook нет.
    settings = Settings(_env_file=None, **{k.lower(): v for k, v in env.items() if k != "ADMIN_IDS"})  # type: ignore[call-arg]
    assert settings.webapp_debug_active is True
    assert settings.webapp_url == ""


def test_real_telegram_keeps_token_and_ai_from_env(tmp_path: Path) -> None:
    env = dev.dev_environment(tmp_path / "dev.db", real_telegram=True)
    assert "BOT_TOKEN" not in env and "AI_PROVIDER" not in env
    assert env["WEBAPP_DEBUG"] == "1" and env["RUN_MODE"] == "polling"


def test_default_db_is_in_temp_dir() -> None:
    path = dev.resolve_db_path(None)
    assert path == dev.default_db_path()
    assert path.name == "kpi_webapp_dev.db"
    assert dev.resolve_db_path("  ") == dev.default_db_path()


@pytest.mark.parametrize(
    "raw",
    [
        str(dev.PROJECT_DB),
        str(dev.PROJECT_DB).upper() if os.name == "nt" else str(dev.PROJECT_DB),
        "postgresql://postgres:pg@127.0.0.1:5432/kpi",
        "postgresql+asyncpg://user@host/db",
        "sqlite+aiosqlite:///data/bot.db",
    ],
)
def test_refuses_project_db_and_database_urls(raw: str) -> None:
    with pytest.raises(dev.DevConfigError):
        dev.resolve_db_path(raw)


def test_refuses_project_db_by_relative_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(dev.REPO_ROOT)
    with pytest.raises(dev.DevConfigError):
        dev.resolve_db_path("data/bot.db")
    with pytest.raises(dev.DevConfigError):
        dev.resolve_db_path("./data/../data/bot.db")


def test_other_file_is_allowed(tmp_path: Path) -> None:
    assert dev.resolve_db_path(str(tmp_path / "x.db")) == tmp_path / "x.db"


@pytest.mark.parametrize("argv", [["--db", str(dev.PROJECT_DB)], ["--db", "postgresql://u@h/db"], ["--port", "0"]])
def test_main_refuses_with_exit_code_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    before = dict(os.environ)
    assert dev.main(argv) == 2
    assert "Ошибка:" in capsys.readouterr().err
    assert dict(os.environ) == before  # окружение не тронуто: сервер не запускался


# --- Приложение ------------------------------------------------------------------------------------------


async def test_build_dev_app_needs_no_network_and_mounts_routes(engine: AsyncEngine) -> None:
    echo: list[str] = []
    async with dev_client(engine, echo) as (client, session):
        assert session.requests == []  # сборка и старт — без единого запроса к «Telegram»
        response = await client.get("/app")
        assert response.status == 200
        page = await response.text()
        assert '"debug":true' in page.replace(" ", "")
        assert (await client.get("/api/me")).status == 401
        response = await client.get("/dev/", allow_redirects=False)
        assert response.status == 200 and response.content_type == "text/html"
        response = await client.get("/", allow_redirects=False)
        assert response.status == 302 and response.headers["Location"] == "/dev/"
    assert echo == []


def test_build_dev_app_refuses_without_debug(engine: AsyncEngine) -> None:
    bot = Bot(dev.DEV_TOKEN, session=dev.make_fake_session(None))
    sessionmaker = make_sessionmaker(engine)
    for settings in (dev_settings(webapp_debug=False), dev_settings(run_mode="webhook", public_url="https://x.example")):
        with pytest.raises(dev.DevConfigError):
            dev.build_dev_app(bot=bot, sessionmaker=sessionmaker, settings=settings)


async def test_login_redirects_with_valid_init_data(engine: AsyncEngine) -> None:
    await add_user(engine, 1001, "Петрова Анна Сергеевна", role=Role.MANAGER)
    async with dev_client(engine) as (client, _):
        response = await client.get(dev.login_url(1001, "/review?tab=proposals"), allow_redirects=False)
        assert response.status == 302
        assert response.headers["Cache-Control"] == "no-store"
        location = urlsplit(response.headers["Location"])
        assert location.path == "/app"
        assert location.fragment == "/review?tab=proposals"
        init_data = parse_qs(location.query)["tg_debug_init"][0]
        init = validate_init_data(init_data, dev.DEV_TOKEN)
        assert init.tg_id == 1001
        assert (init.first_name, init.last_name) == ("Анна", "Петрова")  # имя — из базы
        # С этим initData API пускает как обычно (подпись проверена, исключений для отладки нет).
        me = await client.get("/api/me", headers={"X-Telegram-Init-Data": init_data})
        body = await me.json()
        assert me.status == 200 and body["access"] == "active" and body["role"] == "manager"


async def test_login_unknown_user_gets_unregistered_screen(engine: AsyncEngine) -> None:
    async with dev_client(engine) as (client, _):
        response = await client.get(dev.login_url(dev.GUEST_TG_ID), allow_redirects=False)
        assert response.status == 302
        init_data = parse_qs(urlsplit(response.headers["Location"]).query)["tg_debug_init"][0]
        me = await client.get("/api/me", headers={"X-Telegram-Init-Data": init_data})
        assert (await me.json())["access"] == "unregistered"


@pytest.mark.parametrize(
    "query",
    ["tg_id=abc", "tg_id=0", "tg_id=-5", "tg_id=", "", "tg_id=1001&to=javascript:alert(1)", "tg_id=1001&to=team",
     "tg_id=99999999999999999999"],
)
async def test_login_rejects_bad_parameters(engine: AsyncEngine, query: str) -> None:
    async with dev_client(engine) as (client, _):
        response = await client.get("/dev/login?" + query, allow_redirects=False)
        assert response.status == 400


async def test_index_lists_users_escaped(engine: AsyncEngine) -> None:
    await add_user(engine, 1001, "Петрова Анна Сергеевна", role=Role.MANAGER)
    await add_user(engine, 2001, "<b>Иванов</b> Иван", position="Юрист & ко")
    await add_user(engine, 2004, "Смирнов Олег Викторович", status=UserStatus.PENDING)
    async with dev_client(engine) as (client, _):
        page = await (await client.get("/dev/")).text()
    assert "<b>Иванов</b>" not in page and "&lt;b&gt;Иванов&lt;/b&gt;" in page
    assert "Юрист &amp; ко" in page
    assert page.index("Петрова") < page.index("Иванов") < page.index("Смирнов")  # начальник, активные, заявки
    assert 'href="/dev/login?tg_id=1001&amp;to=/team"' in page  # быстрые ссылки на вкладки роли
    assert "to=/my" in page
    assert f"tg_id={dev.GUEST_TG_ID}" in page


# --- Защита входа: Host и настоящий токен ------------------------------------------------------------------

REAL_TOKEN = "123456:REAL-LOOKING-TOKEN"


@asynccontextmanager
async def real_token_client(engine: AsyncEngine, key: str | None = "run-key") -> AsyncIterator[tuple[TestClient, Any]]:
    """Dev-сервер с «настоящим» токеном (как --real-telegram), но с фейковым Telegram."""
    bot = Bot(REAL_TOKEN, session=dev.make_fake_session(None), default=DefaultBotProperties(parse_mode="HTML"))
    app = dev.build_dev_app(
        bot=bot, sessionmaker=make_sessionmaker(engine), settings=dev_settings(bot_token=REAL_TOKEN), key=key
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, app
    finally:
        await client.close()
        await bot.session.close()


@pytest.mark.parametrize("path", ["/dev/", "/dev/login?tg_id=1001", "/api/me", "/app"])
async def test_foreign_host_is_refused(engine: AsyncEngine, path: str) -> None:
    """DNS rebinding: страница чужого сайта обращается к 127.0.0.1:8081 под своим именем — 403."""
    await add_user(engine, 1001, "Петрова Анна Сергеевна", role=Role.MANAGER)
    async with dev_client(engine) as (client, _):
        response = await client.get(path, headers={"Host": "attacker.example:8081"}, allow_redirects=False)
        assert response.status == 403
        for host in ("127.0.0.1:8081", "localhost:8081", "LOCALHOST"):
            ok = await client.get("/dev/login?tg_id=1001", headers={"Host": host}, allow_redirects=False)
            assert ok.status == 302, host


async def test_fake_token_needs_no_key(engine: AsyncEngine) -> None:
    async with dev_client(engine) as (client, _):
        assert dev.access_key(client.server.app) is None
        assert (await client.get("/dev/")).status == 200


async def test_real_token_requires_run_key_and_known_user(engine: AsyncEngine) -> None:
    """С настоящим токеном подпись годится и для рабочего бота: без ключа запуска — 403, за чужой
    tg_id (нет в dev-базе) — 404; с ключом за своего — вход, подпись тем же токеном."""
    await add_user(engine, 1001, "Петрова Анна Сергеевна", role=Role.MANAGER)
    async with real_token_client(engine) as (client, app):
        assert dev.access_key(app) == "run-key"
        for url in ("/dev/", dev.login_url(1001), dev.login_url(1001, key="wrong"), "/dev/?key=", "/dev/login?tg_id=1001&key="):
            response = await client.get(url, allow_redirects=False)
            assert response.status == 403, url
            assert "ключ запуска" in await response.text()
        unknown = await client.get(dev.login_url(777000111, key="run-key"), allow_redirects=False)
        assert unknown.status == 404
        guest = await client.get(dev.login_url(dev.GUEST_TG_ID, key="run-key"), allow_redirects=False)
        assert guest.status == 404
        response = await client.get(dev.login_url(1001, "/team", "run-key"), allow_redirects=False)
        assert response.status == 302
        init_data = parse_qs(urlsplit(response.headers["Location"]).query)["tg_debug_init"][0]
        assert validate_init_data(init_data, REAL_TOKEN).tg_id == 1001
        page = await (await client.get(dev.index_url("run-key"))).text()
        assert 'href="/dev/login?tg_id=1001&amp;key=run-key"' in page
        assert "to=/team&amp;key=run-key" in page
        assert f"tg_id={dev.GUEST_TG_ID}" not in page  # «войти как незарегистрированный» — только с тестовым токеном
        assert (await client.get("/dev?key=run-key", allow_redirects=False)).headers["Location"] == "/dev/?key=run-key"


async def test_real_token_generates_random_key(engine: AsyncEngine) -> None:
    async with real_token_client(engine, key=None) as (_, first), real_token_client(engine, key=None) as (_, second):
        assert len(dev.access_key(first) or "") >= 20
        assert dev.access_key(first) != dev.access_key(second)


async def test_echo_session_prints_each_telegram_request() -> None:
    echo: list[str] = []
    bot = Bot(dev.DEV_TOKEN, session=dev.make_fake_session(echo.append), default=DefaultBotProperties(parse_mode="HTML"))
    try:
        await bot.send_message(2001, "🆕 Вам поставлена <b>новая задача</b> " + "x" * 300)
    finally:
        await bot.session.close()
    assert len(echo) == 1
    line = echo[0]
    assert line.startswith("Telegram ← SendMessage chat=2001: 🆕 Вам поставлена новая задача")
    assert "<b>" not in line and line.endswith("…")
    assert len(line.split(": ", 1)[1]) == dev.ECHO_TEXT_LIMIT


# --- Демо-данные ---------------------------------------------------------------------------------------------


async def _tasks_of(engine: AsyncEngine, tg_id: int) -> list[Task]:
    async with make_sessionmaker(engine)() as session:
        user = await session.scalar(select(User).where(User.tg_id == tg_id))
        assert user is not None
        return list((await session.scalars(select(Task).where(Task.assignee_id == user.id))).unique())


async def test_seed_demo_creates_described_set(engine: AsyncEngine) -> None:
    sessionmaker = make_sessionmaker(engine)
    assert await dev.seed_demo(sessionmaker, now=NOW) is True
    async with sessionmaker() as session:
        users = {u.tg_id: u for u in await session.scalars(select(User))}
    assert set(users) == {1001, 2001, 2002, 2003, 2004}
    assert users[1001].role == Role.MANAGER and users[1001].status == UserStatus.ACTIVE
    assert users[1001].full_name == "Петрова Анна Сергеевна"
    assert [(users[t].full_name, users[t].position) for t in (2001, 2002, 2003)] == [
        ("Иванов Иван Иванович", "Юрист"),
        ("Сидоров Пётр Ильич", "Экономист"),
        ("Кузнецова Анна Сергеевна", "Аналитик"),
    ]
    assert all(users[t].status == UserStatus.ACTIVE and users[t].role == Role.EMPLOYEE for t in (2001, 2002, 2003))
    assert users[2004].status == UserStatus.PENDING and users[2004].full_name.strip()

    this_week = periods.get_period("week", 0, NOW)
    last_week = periods.get_period("week", -1, NOW)
    for tg_id in (2001, 2002, 2003):
        tasks = await _tasks_of(engine, tg_id)
        active = [t for t in tasks if t.status == TaskStatus.ACTIVE]
        assert any(t.accepted_at is not None and t.deadline > NOW for t in active), tg_id
        assert any(t.accepted_at is None and t.deadline > NOW for t in active), tg_id
        assert any(t.deadline < NOW for t in active), tg_id  # просроченная
        rework = [t for t in tasks if t.status == TaskStatus.REWORK]
        assert len(rework) == 1 and rework[0].rework_count == 1
        last = rework[0].last_submission
        assert last.decision == ReviewDecision.REWORK and last.review_comment
        submitted = [t for t in tasks if t.status == TaskStatus.SUBMITTED]
        assert len(submitted) == 1
        sub = submitted[0].last_submission
        assert sub.decision is None and sub.ai_source == "rules" and sub.ai_score is not None
        assert sub.ai_rationale.startswith(RULES_PREFIX)
        assert [(a.kind, a.file_id) for a in sub.attachments] == [(AttachmentKind.DOCUMENT, "demo-doc-1")]
        recent_done = {
            t.final_score
            for t in tasks
            if t.status == TaskStatus.DONE and last_week.start <= t.deadline < this_week.end
        }
        assert recent_done == {90.0, 100.0, 110.0}, tg_id
        assert all(t.completed_at is not None and t.completed_at <= NOW for t in tasks if t.status == TaskStatus.DONE)
        proposed = [t for t in tasks if t.status == TaskStatus.PROPOSED]
        assert len(proposed) == 1 and proposed[0].source == TaskSource.EMPLOYEE and proposed[0].manager_id is None
        assert sum(t.status == TaskStatus.CANCELLED for t in tasks) == 1
        # История оценок прошлых недель — для графика тренда.
        assert any(t.status == TaskStatus.DONE and t.deadline < last_week.start for t in tasks), tg_id

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(TaskEvent)) > 0
        times = list(await session.scalars(select(Submission.created_at)))
        assert all(at <= NOW for at in times)

    # Повторный запуск — база уже не пустая: ничего не добавляется.
    assert await dev.seed_demo(sessionmaker, now=NOW) is False
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 5


async def test_seed_demo_extended_adds_leader_and_newcomer(engine: AsyncEngine) -> None:
    sessionmaker = make_sessionmaker(engine)
    assert await dev.seed_demo(sessionmaker, now=NOW, extended=True) is True
    leader = await _tasks_of(engine, 2005)
    sub = next(t for t in leader if t.status == TaskStatus.SUBMITTED).last_submission
    assert sub.ai_source == "ai" and sub.ai_model and sub.ai_rationale
    assert {a.kind for a in sub.attachments} == {AttachmentKind.PHOTO, AttachmentKind.DOCUMENT}
    newcomer = await _tasks_of(engine, 2006)
    assert newcomer and all(t.status == TaskStatus.ACTIVE for t in newcomer)
    assert any(t.accepted_at is None for t in newcomer)


async def test_seeded_demo_works_through_api(engine: AsyncEngine) -> None:
    """Демо-база отдаётся API без ошибок: дашборд, очередь проверки, карточки, KPI сотрудника."""
    await dev.seed_demo(make_sessionmaker(engine), extended=True)
    async with dev_client(engine) as (client, _):

        async def get(tg_id: int, path: str) -> Any:
            location = (await client.get(dev.login_url(tg_id), allow_redirects=False)).headers["Location"]
            init_data = parse_qs(urlsplit(location).query)["tg_debug_init"][0]
            response = await client.get(path, headers={"X-Telegram-Init-Data": init_data})
            assert response.status == 200, (path, await response.text())
            return await response.json()

        dash = await get(1001, "/api/dashboard")
        assert dash["totals"]["employees"] == 5
        assert sum(1 for p in dash["trend"]["points"] if p["kpi"] is not None) >= 6
        review = await get(1001, "/api/review")
        assert len(review["items"]) == 4
        assert {item["submission"]["ai"]["source"] for item in review["items"]} == {"rules", "ai"}
        for item in review["items"]:
            await get(1001, f"/api/tasks/{item['task']['id']}")
        assert len((await get(1001, "/api/proposals"))["items"]) == 3
        me = await get(1001, "/api/me")
        assert me["counts"]["pending_users"] == 1
        mine = await get(2001, "/api/tasks?scope=my&status=all&limit=50")
        assert {t["status"] for t in mine["items"]} >= {"active", "rework", "submitted", "done", "proposed"}
        kpi = await get(2001, f"/api/users/{(await get(2001, '/api/me'))['user']['id']}/kpi")
        assert kpi["history"]["total"] == 5


def test_seed_week_offsets_cover_trend_window() -> None:
    """Оценённые задачи демо-команды лежат в окне тренда (8 недель) — график не пустой."""
    weeks = {done.week for profile in dev._PROFILES.values() for done in profile.done}
    assert weeks <= set(range(-7, 1)) and {0, -1} <= weeks
    assert len(weeks) >= 6  # почти каждая неделя окна — с оценённой задачей
