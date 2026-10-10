"""Фикстуры тестов API приложения в Telegram (bot.webapp, docs/MINIAPP_SPEC.md §12).

``ma`` — ``MiniApp``: бот целиком (``bot.main.build_dispatcher``) на фейковом Telegram API
(tests/e2e/fakebot.py) и тестовой БД (фикстура ``engine`` из tests/conftest.py: in-memory SQLite, а с
TEST_DATABASE_URL — PostgreSQL); рядом — ``web.Application()`` + ``setup_webapp`` (статика — заглушки во
временной папке) и ``aiohttp`` TestClient. Чат и API работают на ОДНОМ боте и ОДНОЙ базе:

* ``ma.h`` — BotHarness (чат): ``await ma.h.press_button(EMP, "Принял")`` и т. п.;
* ``ma.auth(tg_id)`` — заголовок с подписанным initData; ``ma.get/post/patch(path, as_=tg_id, json=…)``;
* ``ma.drain()`` — дождаться фоновых задач приложения (оценка сдачи, Excel, пересылка файлов);
* ``ma.seed_team()``, ``ma.seed_task(...)`` — данные прямо в БД.

Сети нет: Telegram — FakeSession, AI выключен (AI_PROVIDER=none) или подменён в тесте.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parents[1]
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

WEBAPP_ENV: dict[str, str] = {
    "BOT_TOKEN": "42:TEST",
    "ADMIN_IDS": "1001",
    "AI_PROVIDER": "none",
    "GEMINI_API_KEY": "",
    "GROQ_API_KEY": "",
    "CLOUDFLARE_API_TOKEN": "",
    "CLOUDFLARE_ACCOUNT_ID": "",
    "MISTRAL_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "TIMEZONE": "Asia/Tashkent",
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "DATABASE_PASSWORD": "",
    "RUN_MODE": "polling",
    "PUBLIC_URL": "",
    "RENDER_EXTERNAL_URL": "",
    "WEBAPP_ENABLED": "1",
    "WEBAPP_DEBUG": "0",
    "MAX_SCORE": "150",
    "DEFAULT_DEADLINE_TIME": "18:00",
    "OVERDUE_COUNTS_AS_ZERO": "true",
}
os.environ.update(WEBAPP_ENV)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from aiogram import Bot, Router  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402

from bot.config import get_settings  # noqa: E402

get_settings.cache_clear()

from e2e.fakebot import BotHarness, FakeSession  # noqa: E402

TOKEN = "42:TEST"
MGR = 1001  # начальник из ADMIN_IDS
MGR2 = 1002
EMP = 2001
EMP2 = 2002
EMP3 = 2003

STUB_INDEX = (
    '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
    '<script src="https://telegram.org/js/telegram-web-app.js"></script>'
    '<link rel="stylesheet" href="/app/static/app.css?v=__ASSET_VERSION__">'
    '<script defer src="/app/static/app.js?v=__ASSET_VERSION__"></script>'
    '<script id="kpi-config" type="application/json">__KPI_CONFIG__</script>'
    '</head><body><div id="app">Загрузка…</div><div id="toast" role="status" aria-live="polite"></div></body></html>'
)
STUB_JS = b"console.log('kpi');\n"
STUB_CSS = b":root{--bg:#fff}\n"


def write_static(folder: Path, *, index: str = STUB_INDEX, js: bytes = STUB_JS, css: bytes = STUB_CSS) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(index, encoding="utf-8")
    (folder / "app.js").write_bytes(js)
    (folder / "app.css").write_bytes(css)
    return folder


def release_bot_routers() -> None:
    """Отвязать модульные роутеры bot/handlers от Dispatcher'ов прошлых тестов (как tests/e2e/conftest.py)."""
    module_routers: dict[int, Router] = {}
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "bot" or name.startswith("bot.")):
            continue
        for value in list(vars(module).values()):
            if isinstance(value, Router):
                module_routers[id(value)] = value
    for router in module_routers.values():
        parent = router.parent_router
        if parent is None or id(parent) in module_routers:
            continue
        if router in parent.sub_routers:
            parent.sub_routers.remove(router)
        router._parent_router = None  # публичного API для отвязки в aiogram нет


@dataclass
class Resp:
    """Ответ API: статус, JSON-тело и заголовки."""

    status: int
    data: Any
    headers: Any
    text: str = ""

    @property
    def code(self) -> str | None:
        return self.data.get("code") if isinstance(self.data, dict) else None

    @property
    def error(self) -> str | None:
        return self.data.get("error") if isinstance(self.data, dict) else None

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


@dataclass
class MiniApp:
    h: BotHarness
    client: TestClient
    app: web.Application
    sessionmaker: async_sessionmaker[AsyncSession]
    static_dir: Path
    users: dict[int, Any] = field(default_factory=dict)

    @property
    def ctx(self) -> Any:
        from bot.webapp import CTX

        return self.app[CTX]

    @property
    def api(self) -> Any:
        return self.h.api  # FakeSession: что бот отправил в Telegram

    def db(self) -> AsyncSession:
        return self.sessionmaker()

    def init_data(self, tg_id: int, **kwargs: Any) -> str:
        from bot.webapp.auth import sign_init_data

        kwargs.setdefault("first_name", "Тест")
        return sign_init_data(TOKEN, tg_id=tg_id, **kwargs)

    def auth(self, tg_id: int, **kwargs: Any) -> dict[str, str]:
        from bot.webapp.auth import INIT_DATA_HEADER

        return {INIT_DATA_HEADER: self.init_data(tg_id, **kwargs)}

    async def request(
        self,
        method: str,
        path: str,
        *,
        as_: int | None = None,
        json: Any = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        all_headers = dict(self.auth(as_)) if as_ is not None else {}
        all_headers.update(headers or {})
        kwargs: dict[str, Any] = {"headers": all_headers}
        if json is not None:
            kwargs["json"] = json
        if data is not None:
            kwargs["data"] = data
        if params is not None:
            kwargs["params"] = {key: str(value) for key, value in params.items()}
        async with self.client.request(method, path, **kwargs) as response:
            text = await response.text()
            try:
                body = await response.json(content_type=None) if text else None
            except ValueError:
                body = None
            return Resp(response.status, body, response.headers, text)

    async def get(self, path: str, **kwargs: Any) -> Resp:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Resp:
        return await self.request("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> Resp:
        return await self.request("PATCH", path, **kwargs)

    async def drain(self, timeout: float = 30) -> None:
        await self.ctx.tasks.drain(timeout=timeout)

    # --- Данные ------------------------------------------------------------------------------------

    async def seed_user(self, tg_id: int, full_name: str, **kwargs: Any) -> Any:
        user = await self.h.seed_user(tg_id, full_name, **kwargs)
        self.users[tg_id] = user
        return user

    async def seed_team(self, employees: int = 2) -> tuple[Any, list[Any]]:
        """Начальник Петрова (1001) и сотрудники Иванов (2001), Сидоров (2002), Кузнецова (2003)."""
        mgr = await self.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
        people = [
            (EMP, "Иванов Иван Иванович", "Юрист"),
            (EMP2, "Сидоров Пётр Ильич", "Экономист"),
            (EMP3, "Кузнецова Анна Сергеевна", "Аналитик"),
        ]
        staff = [await self.seed_user(tg, name, position=pos) for tg, name, pos in people[:employees]]
        return mgr, staff

    async def seed_task(
        self,
        assignee: Any,
        manager: Any,
        *,
        kind: str = "active",
        title: str = "Анализ договоров",
        expected_result: str = "Проверить 100 договоров и представить отчёт",
        plan_value: float | None = 100.0,
        plan_unit: str | None = "договоров",
        deadline: datetime | None = None,
        weight: int = 20,
        accepted: bool = True,
        ai_score: float | None = 110.0,
        final_score: float | None = 110.0,
        late: bool = False,
        files: int = 0,
        completed_at: datetime | None = None,
    ) -> int:
        """Задача прямо в БД. kind: active | overdue | rework | submitted | done | proposed | cancelled | rejected."""
        from bot.db.models import (
            Attachment,
            AttachmentKind,
            Priority,
            ReviewDecision,
            Submission,
            Task,
            TaskSource,
            TaskStatus,
        )
        from bot.utils.dates import utcnow

        now = utcnow()
        if deadline is None:
            deadline = now - timedelta(days=1) if kind == "overdue" else now + timedelta(days=3)
        status = {
            "active": TaskStatus.ACTIVE,
            "overdue": TaskStatus.ACTIVE,
            "rework": TaskStatus.REWORK,
            "submitted": TaskStatus.SUBMITTED,
            "done": TaskStatus.DONE,
            "proposed": TaskStatus.PROPOSED,
            "cancelled": TaskStatus.CANCELLED,
            "rejected": TaskStatus.REJECTED,
        }[kind]
        employee_source = kind in ("proposed", "rejected")
        async with self.db() as session:
            task = Task(
                title=title,
                expected_result=expected_result,
                plan_value=plan_value,
                plan_unit=plan_unit,
                deadline=deadline,
                priority=Priority.MEDIUM,
                weight=weight,
                status=status,
                source=TaskSource.EMPLOYEE if employee_source else TaskSource.MANAGER,
                assignee_id=assignee.id,
                created_by_id=assignee.id if employee_source else manager.id,
                manager_id=None if employee_source else manager.id,
                accepted_at=(now - timedelta(days=1)) if accepted and not employee_source else None,
                rework_count=0,
            )
            if kind in ("rework", "submitted", "done"):
                submitted_at = deadline + timedelta(hours=5) if late else min(now, deadline) - timedelta(hours=5)
                sub = Submission(
                    attempt=1,
                    fact_text="Проверено 110 договоров",
                    fact_value=110.0,
                    created_at=submitted_at,
                    deadline_at_submit=deadline,
                    is_late=late,
                    late_days=round((submitted_at - deadline) / timedelta(days=1), 1) if late else 0.0,
                    ai_score=ai_score,
                    ai_rationale=("Расчёт по правилам (AI недоступен): План: 100, факт: 110." if ai_score else None),
                    ai_source="rules" if ai_score is not None else None,
                    attachments=[
                        Attachment(
                            kind=AttachmentKind.DOCUMENT,
                            file_id=f"doc-{assignee.id}-{n}",
                            file_name=f"report{n}.pdf",
                            mime_type="application/pdf",
                            file_size=1000 + n,
                        )
                        for n in range(files)
                    ],
                )
                task.submissions = [sub]
                task.submitted_at = submitted_at
                task.ai_score = ai_score
                if kind == "rework":
                    task.rework_count = 1
                    sub.decision = ReviewDecision.REWORK
                    sub.review_comment = "Добавьте выводы"
                    sub.reviewer_id = manager.id
                    sub.reviewed_at = submitted_at + timedelta(hours=1)
                elif kind == "done":
                    task.final_score = final_score
                    task.completed_at = completed_at or submitted_at + timedelta(hours=1)
                    sub.decision = ReviewDecision.APPROVED
                    sub.final_score = final_score
                    sub.reviewer_id = manager.id
                    sub.reviewed_at = task.completed_at
            session.add(task)
            await session.commit()
            return task.id

    async def task(self, task_id: int) -> Any:
        return await self.h.get_task(task_id)


@pytest.fixture
def frozen(clock: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """«Сейчас» заморожено (tests/conftest.py::clock: пт 02.10.2026 12:00 по Ташкенту) — и в API приложения."""
    from bot.webapp import api as api_module

    monkeypatch.setattr(api_module, "utcnow", lambda: clock.now)
    return clock


def plain(html_text: str | None) -> str:
    """HTML чата -> видимый текст (как serializers._plain)."""
    import html
    import re

    return html.unescape(re.sub(r"<[^>]*>", "", html_text or ""))


@pytest.fixture
def webapp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in WEBAPP_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def ma(
    webapp_env: None, engine: AsyncEngine, storage_engine: AsyncEngine | None, tmp_path: Path
) -> AsyncIterator[MiniApp]:
    """Бот (чат) + Mini App на одном боте и одной базе, aiohttp TestClient."""
    from bot.db.base import make_sessionmaker
    from bot.main import build_dispatcher, default_storage
    from bot.webapp import setup_webapp

    sessionmaker = make_sessionmaker(engine)
    storage = default_storage(make_sessionmaker(storage_engine)) if storage_engine is not None else None
    release_bot_routers()
    dp = build_dispatcher(sessionmaker, storage)
    bot = Bot(TOKEN, session=FakeSession(), default=DefaultBotProperties(parse_mode="HTML"))
    harness = BotHarness(dp, bot, sessionmaker)
    static_dir = write_static(tmp_path / "static")
    app = web.Application()
    setup_webapp(app, bot=bot, sessionmaker=sessionmaker, settings=get_settings(), static_dir=static_dir)
    client = TestClient(TestServer(app))
    await client.start_server()
    mini = MiniApp(harness, client, app, sessionmaker, static_dir)
    try:
        yield mini
    finally:
        try:
            await mini.ctx.tasks.drain(timeout=30)
        finally:
            await client.close()
            await bot.session.close()
            release_bot_routers()
