"""Общие фикстуры тестов ядра (сервисы, KPI, напоминания, AI-правила, UI).

* Окружение изолировано: переменные выставляются ДО импорта ``bot.config`` и заново на каждый
  тест (``monkeypatch`` + ``get_settings.cache_clear()``), поэтому ``.env`` разработчика и
  тесты, меняющие настройки, друг на друга не влияют. AI выключен (AI_PROVIDER=none).
* БД — in-memory SQLite (``make_engine`` + ``init_db``), своя на каждый тест; с переменной
  TEST_DATABASE_URL — PostgreSQL (схема создаётся один раз, перед каждым тестом таблицы очищаются;
  см. раздел «База данных» ниже).
* «Сейчас» управляется фикстурой ``clock``: она подменяет ``utcnow`` в модулях, которые его
  импортируют. Там, где API принимает ``now=``, тесты передают время явно.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_ENV: dict[str, str] = {
    "BOT_TOKEN": "test",
    "ADMIN_IDS": "1001",
    "AI_PROVIDER": "none",
    "GEMINI_API_KEY": "",
    # Ключи запасных AI-провайдеров из .env разработчика не должны включать AI в тестах.
    "GROQ_API_KEY": "",
    "CLOUDFLARE_API_TOKEN": "",
    "CLOUDFLARE_ACCOUNT_ID": "",
    "MISTRAL_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "TIMEZONE": "Asia/Tashkent",
    # Подстраховка: тесты никогда не трогают data/bot.db.
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    # Пароль базы из .env разработчика не подставляется в адреса тестов (bot.db.base.make_engine).
    "DATABASE_PASSWORD": "",
    # Значения по умолчанию из SPEC — явно, чтобы .env разработчика не менял ожидания тестов.
    "AI_READ_FILES": "true",
    "MAX_SCORE": "150",
    "LATE_PENALTY_PER_DAY": "2",
    "LATE_PENALTY_MAX": "20",
    "OVERDUE_COUNTS_AS_ZERO": "true",
    "DEFAULT_DEADLINE_TIME": "18:00",
    "REMINDER_DAYS_BEFORE": "3,1",
    "REMINDER_HOURS_BEFORE": "3",
    "OVERDUE_REMINDER_HOUR": "10",
    "QUIET_HOURS_START": "21",
    "QUIET_HOURS_END": "8",
    "REVIEW_REMINDER_DAYS": "2",
    # Метки слов пользователя (bot.i18n) в тестах выключены: юнит-тесты сверяют тексты render напрямую.
    # TEST_I18N_MARKS=true — прогнать набор с метками (проверка, что они не доходят до Telegram и API).
    "I18N_MARKS": os.environ.get("TEST_I18N_MARKS", "false"),
}
os.environ.update(TEST_ENV)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from bot.config import get_settings  # noqa: E402

get_settings.cache_clear()

from bot.db.base import Base, init_db, make_engine, make_sessionmaker, make_storage_engine  # noqa: E402
from bot.db.models import (  # noqa: E402
    ReviewDecision,
    Role,
    Submission,
    Task,
    TaskSource,
    TaskStatus,
    User,
    UserStatus,
)
from bot.services import users as users_service  # noqa: E402

# Пятница, 02.10.2026 12:00 по Ташкенту (UTC+5) — «сейчас» в тестах с фикстурой clock.
NOW_UTC = datetime(2026, 10, 2, 7, 0)

# Модули, которые импортируют utcnow напрямую (from bot.utils.dates import utcnow).
_CLOCK_MODULES = (
    "bot.utils.dates",
    "bot.services.tasks",
    "bot.services.kpi",
    "bot.services.periods",
    "bot.services.reminders",
    "bot.services.auto",
    "bot.services.export",
    "bot.ai.evaluate",
    "bot.ui.render",
    "bot.ui.keyboards",
)


# --- Окружение ----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _test_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Тестовые настройки на время каждого теста; кэш Settings сбрасывается до и после.
    Паузы AI-провайдеров (bot.ai.provider) тоже не переходят из теста в тест."""
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    _reset_ai_state()
    _reset_languages()
    yield
    get_settings.cache_clear()
    _reset_ai_state()
    _reset_languages()


def _reset_languages() -> None:
    """Языки людей (bot.i18n) запоминаются в памяти процесса — из теста в тест они не переходят."""
    i18n = sys.modules.get("bot.i18n")
    if i18n is not None:
        i18n.forget_all()
        i18n.set_current(None)


def _reset_ai_state() -> None:
    provider = sys.modules.get("bot.ai.provider")
    if provider is not None:
        provider.reset_state()


@pytest.fixture
def set_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Поменять настройки в тесте: ``set_env(QUIET_HOURS_START="13")`` (кэш сбрасывается)."""

    def _set(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    return _set


# --- Время -----------------------------------------------------------------------------------------


@dataclass
class Clock:
    """Управляемое «сейчас» (naive UTC) для сервисов, которые сами вызывают utcnow()."""

    now: datetime

    def set(self, value: datetime) -> datetime:
        self.now = value
        return value

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Заморозить время на NOW_UTC; ``clock.advance(days=1)`` — сдвинуть."""
    import importlib

    state = Clock(NOW_UTC)
    for name in _CLOCK_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "utcnow", lambda: state.now)
    return state


# --- База данных -----------------------------------------------------------------------------------
#
# По умолчанию — in-memory SQLite, своя на каждый тест.
#
# TEST_DATABASE_URL=postgresql://user:pass@host:port/<имя с «test»> — те же тесты (и e2e: фикстура app
# берёт этот же engine) на PostgreSQL:
# * таблицы живут в отдельной схеме kpi_test_<воркер> (search_path), схема public не трогается;
#   схема пересоздаётся один раз за запуск (DROP SCHEMA ... CASCADE + init_db), поэтому модели
#   и таблицы всегда совпадают;
# * перед каждым тестом — TRUNCATE всех таблиц RESTART IDENTITY: база пустая, id снова с 1;
# * настоящий пул соединений, как в проде (make_engine: pool_size=3, max_overflow=1): у каждой
#   сессии своё соединение и своя транзакция, параллельные апдейты e2e действительно параллельны.
#   TEST_DATABASE_POOL=shared — одно соединение на все сессии (StaticPool, как in-memory SQLite:
#   сессии видят незакоммиченное друг друга; параллельные сессии так не работают);
# * lock_timeout=10s: тест, ждущий блокировку другой незакоммиченной сессии, падает, а не висит.
# Защита от ошибки: в имени базы должно быть «test» — тесты стирают все таблицы.
# Локальный PostgreSQL без прав администратора (Windows): ``pip install pgserver`` в отдельный venv —
# в пакете готовые программы PostgreSQL 16 (pgserver/pginstall/bin): initdb -D <папка> -U postgres -E UTF8,
# pg_ctl -D <папка> -o "-p 55433" start, CREATE DATABASE kpi_test. Близко к Supabase — пароль по
# scram-sha-256 (pg_hba.conf) и база с LC_COLLATE 'en-US' (порядок строк как у en_US.UTF-8).
#
# Тестам, которым по смыслу нужна именно in-memory SQLite (резервная копия «базы в памяти», режим
# FSM для базы в памяти), — фикстуры memory_engine / memory_sessionmaker: они всегда SQLite.

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
_PG_SCHEMA = "kpi_test_" + "".join(ch if ch.isalnum() else "_" for ch in os.environ.get("PYTEST_XDIST_WORKER", "main"))
_PG_LOCK_TIMEOUT = "10s"
_pg_schema_ready = False


def pg_test_url() -> str | None:
    """Адрес тестового PostgreSQL или None (тесты идут на SQLite)."""
    if not TEST_DATABASE_URL:
        return None
    from bot.db.base import is_postgres_url, normalize_url

    if not is_postgres_url(TEST_DATABASE_URL):
        pytest.exit("TEST_DATABASE_URL: ожидается адрес PostgreSQL (postgresql://...)", returncode=2)
    if "test" not in (normalize_url(TEST_DATABASE_URL).database or "").lower():
        pytest.exit(
            "TEST_DATABASE_URL: в имени базы должно быть «test» — тесты очищают все таблицы", returncode=2
        )
    return TEST_DATABASE_URL


def make_pg_test_engine(
    *, shared: bool | None = None, factory: Callable[..., AsyncEngine | None] = make_engine, **kwargs: object
) -> AsyncEngine:
    """Движок тестового PostgreSQL в схеме _PG_SCHEMA (shared=True — одно соединение на всех).

    factory — чем создавать движок: make_engine (по умолчанию) или make_storage_engine (пул хранилища диалогов).
    """
    url = pg_test_url()
    assert url is not None, "TEST_DATABASE_URL не задан"
    if shared is None:
        shared = os.environ.get("TEST_DATABASE_POOL", "").strip().lower() == "shared"
    if shared:
        kwargs.setdefault("poolclass", StaticPool)
    settings = {"search_path": _PG_SCHEMA, "lock_timeout": _PG_LOCK_TIMEOUT}
    db_engine = factory(url, connect_args={"server_settings": settings}, **kwargs)
    assert db_engine is not None, "factory не создала движок для PostgreSQL"
    return db_engine


async def reset_pg_database(db_engine: AsyncEngine) -> None:
    """Чистая база: схема создаётся один раз за запуск, дальше таблицы только очищаются."""
    global _pg_schema_ready
    if not _pg_schema_ready:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE'))
            await conn.execute(text(f'CREATE SCHEMA "{_PG_SCHEMA}"'))
        await init_db(db_engine)
        _pg_schema_ready = True
        return
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


async def create_test_engine() -> AsyncEngine:
    """Движок для теста: чистая in-memory SQLite или (TEST_DATABASE_URL) очищенный PostgreSQL."""
    if pg_test_url() is None:
        db_engine = make_engine("sqlite+aiosqlite:///:memory:")
        await init_db(db_engine)
        return db_engine
    db_engine = make_pg_test_engine()
    try:
        await reset_pg_database(db_engine)
    except BaseException:
        await db_engine.dispose()
        raise
    return db_engine


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Тестовая БД: in-memory SQLite или (TEST_DATABASE_URL) очищенный PostgreSQL."""
    db_engine = await create_test_engine()
    try:
        yield db_engine
    finally:
        await db_engine.dispose()


@pytest_asyncio.fixture
async def memory_engine() -> AsyncIterator[AsyncEngine]:
    """Всегда in-memory SQLite (даже с TEST_DATABASE_URL) — для тестов поведения «базы в памяти»."""
    db_engine = make_engine("sqlite+aiosqlite:///:memory:")
    await init_db(db_engine)
    try:
        yield db_engine
    finally:
        await db_engine.dispose()


@pytest.fixture
def memory_sessionmaker(memory_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(memory_engine)


@pytest_asyncio.fixture
async def pg_engine() -> AsyncIterator[AsyncEngine]:
    """Очищенный PostgreSQL с настоящим пулом (гонки, диалект); без TEST_DATABASE_URL тест пропускается."""
    if pg_test_url() is None:
        pytest.skip("нужен PostgreSQL: задайте TEST_DATABASE_URL")
    db_engine = make_pg_test_engine(shared=False)
    try:
        await reset_pg_database(db_engine)
        yield db_engine
    finally:
        await db_engine.dispose()


@pytest_asyncio.fixture
async def storage_engine(engine: AsyncEngine) -> AsyncIterator[AsyncEngine | None]:
    """Как в bot.main.main: на PostgreSQL у хранилища диалогов свой пул (make_storage_engine) в той же
    тестовой схеме, что и ``engine``; SQLite и TEST_DATABASE_POOL=shared — None (хранилище на ``engine``)."""
    if engine.dialect.name != "postgresql" or isinstance(engine.pool, StaticPool):
        yield None
        return
    db_engine = make_pg_test_engine(shared=False, factory=make_storage_engine)
    try:
        yield db_engine
    finally:
        await db_engine.dispose()


@pytest_asyncio.fixture
async def pg_engine_factory(pg_engine: AsyncEngine) -> AsyncIterator[Callable[..., AsyncEngine]]:
    """Ещё движки к той же очищенной схеме PostgreSQL, что и pg_engine (свои пулы):
    ``pg_engine_factory(make_storage_engine)``, ``pg_engine_factory(pool_timeout=1)``; закрываются после теста."""
    made: list[AsyncEngine] = []

    def make(factory: Callable[..., AsyncEngine | None] = make_engine, **kwargs: object) -> AsyncEngine:
        made.append(make_pg_test_engine(shared=False, factory=factory, **kwargs))
        return made[-1]

    try:
        yield make
    finally:
        for db_engine in made:
            await db_engine.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(engine)


@pytest_asyncio.fixture
async def session(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as db_session:
        yield db_session


# --- Пользователи ----------------------------------------------------------------------------------


async def add_user(
    session: AsyncSession,
    tg_id: int,
    full_name: str,
    *,
    role: Role = Role.EMPLOYEE,
    status: UserStatus = UserStatus.ACTIVE,
    position: str | None = None,
) -> User:
    """Создать пользователя напрямую в БД (без диалога регистрации)."""
    user = User(tg_id=tg_id, full_name=full_name, position=position, role=role, status=status)
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def manager(session: AsyncSession) -> User:
    """Активный начальник из ADMIN_IDS (tg 1001) — через register_or_get, как при /start."""
    user, created = await users_service.register_or_get(session, 1001, "boss", "Петров Пётр Петрович")
    assert created and user.is_manager
    return user


@pytest_asyncio.fixture
async def employee(session: AsyncSession) -> User:
    return await add_user(session, 2001, "Иванов Иван Иванович", position="Юрист")


@pytest_asyncio.fixture
async def employee2(session: AsyncSession) -> User:
    return await add_user(session, 2002, "Сидорова Анна Сергеевна", position="Экономист")


@pytest.fixture
def user_factory(session: AsyncSession) -> Callable[..., Awaitable[User]]:
    """``await user_factory(3001, "Петров П.", role=Role.MANAGER, status=UserStatus.PENDING)``."""

    async def _make(tg_id: int, full_name: str, **kwargs: object) -> User:
        return await add_user(session, tg_id, full_name, **kwargs)  # type: ignore[arg-type]

    return _make


# --- Задачи ----------------------------------------------------------------------------------------

TaskFactory = Callable[..., Awaitable[Task]]


@pytest.fixture
def make_task(session: AsyncSession, manager: User) -> TaskFactory:
    """Задача напрямую через модели — с любым сроком (в т.ч. в прошлом) и любым статусом.

    ``late`` — is_late последней сдачи (для SUBMITTED/DONE сдача создаётся автоматически),
    ``final_score`` — итоговая оценка DONE, ``source`` — кто внёс задачу.
    """

    async def _make(
        assignee: User,
        *,
        deadline: datetime,
        title: str = "Задача",
        weight: int = 10,
        status: TaskStatus = TaskStatus.ACTIVE,
        source: TaskSource = TaskSource.MANAGER,
        final_score: float | None = None,
        ai_score: float | None = None,
        late: bool | None = None,
        submitted_at: datetime | None = None,
        accepted: bool = True,
        plan_value: float | None = None,
        plan_unit: str | None = None,
        expected_result: str = "Ожидаемый результат",
    ) -> Task:
        submissions: list[Submission] = []
        if status in (TaskStatus.SUBMITTED, TaskStatus.DONE) or late is not None:
            created = submitted_at or (deadline + timedelta(days=1) if late else deadline - timedelta(hours=1))
            submissions.append(
                Submission(
                    attempt=1,
                    fact_text="Сделано",
                    created_at=created,
                    deadline_at_submit=deadline,
                    is_late=bool(late),
                    late_days=1.0 if late else 0.0,
                    ai_score=ai_score,
                    ai_source="rules" if ai_score is not None else None,
                    final_score=final_score if status == TaskStatus.DONE else None,
                    decision=ReviewDecision.APPROVED if status == TaskStatus.DONE else None,
                    reviewer=manager if status == TaskStatus.DONE else None,
                    attachments=[],
                )
            )
        last = submissions[-1] if submissions else None
        task = Task(
            title=title,
            expected_result=expected_result,
            plan_value=plan_value,
            plan_unit=plan_unit,
            deadline=deadline,
            weight=weight,
            status=status,
            source=source,
            assignee=assignee,
            created_by=assignee if source == TaskSource.EMPLOYEE else manager,
            manager=manager,
            accepted_at=(deadline - timedelta(days=5)) if accepted else None,
            submitted_at=last.created_at if last else None,
            completed_at=(last.created_at + timedelta(hours=1)) if status == TaskStatus.DONE and last else None,
            ai_score=ai_score,
            final_score=final_score,
            rework_count=0,
            submissions=submissions,
        )
        session.add(task)
        await session.flush()
        return task

    return _make
