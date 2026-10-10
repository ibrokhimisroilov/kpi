"""Восстановление базы из файла резервной копии: ``python -m bot.tools.restore файл.db [--force]``.

* SQLite -> SQLite: все таблицы бота строка в строку (id сохранены), бот открывает восстановленную базу;
  копия-снимок (SQLite в файле) и копия-выгрузка (PostgreSQL) восстанавливаются одинаково;
* в базе уже есть сотрудники — отказ (база не меняется); ``--force`` — замена; служебные строки
  пустой базы (журнал заданий, диалоги) не мешают;
* ошибка посередине — одна транзакция, база остаётся как была;
* неверный файл, копия старой версии без новых таблиц, значения под ограничения PostgreSQL,
  последовательности id PostgreSQL, командная строка (итог по-русски, пароль не печатается);
* PostgreSQL — если задан TEST_DATABASE_URL (пустая тестовая база: тесты пересоздают в ней таблицы бота).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.db.base import Base, init_db, make_engine, make_sessionmaker
from bot.db.models import (
    Attachment,
    AttachmentKind,
    DigestLog,
    EventType,
    FsmState,
    JobLog,
    ReminderLog,
    ReviewDecision,
    Role,
    Submission,
    Task,
    TaskEvent,
    TaskStatus,
    User,
    UserStatus,
)
from bot.scheduler import backup
from bot.tools import restore as restore_tool
from bot.tools.restore import RestoreError

PG_URL = os.environ.get("TEST_DATABASE_URL", "")
DEADLINE = datetime(2026, 10, 9, 13, 0)
TABLES = [table.name for table in Base.metadata.sorted_tables]


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


async def seed_source(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Строки во всех таблицах бота, id «с дырами» (как после удалений) — чтобы проверить их сохранение."""
    async with sessionmaker() as session:
        manager = User(id=3, tg_id=1001, username="boss", full_name="Петрова Анна Сергеевна",
                       role=Role.MANAGER, status=UserStatus.ACTIVE)
        employee = User(id=7, tg_id=2001, full_name="Иванов Иван Иванович", position="Юрист",
                        role=Role.EMPLOYEE, status=UserStatus.ACTIVE)
        pending = User(id=8, tg_id=5_000_000_001, full_name="Сидорова Мария Олеговна",
                       role=Role.EMPLOYEE, status=UserStatus.PENDING)
        session.add_all([manager, employee, pending])
        await session.flush()
        done = Task(id=11, title="Анализ договоров", expected_result="Проверить 100 договоров", plan_value=100,
                    plan_unit="договоров", deadline=DEADLINE, weight=30, status=TaskStatus.DONE,
                    assignee_id=employee.id, created_by_id=manager.id, manager_id=manager.id,
                    final_score=100.0, ai_score=104.0)
        active = Task(id=15, title="Отчёт за квартал", expected_result="Отчёт в PDF", deadline=DEADLINE,
                      weight=10, assignee_id=employee.id, created_by_id=manager.id, manager_id=manager.id)
        session.add_all([done, active])
        await session.flush()
        session.add_all([
            Submission(id=21, task_id=done.id, attempt=1, fact_text="Проверено 110 договоров", fact_value=110.0,
                       deadline_at_submit=DEADLINE, is_late=True, late_days=0.5, ai_score=104.0,
                       ai_source="rules", final_score=100.0, decision=ReviewDecision.CHANGED,
                       reviewer_id=manager.id, reviewed_at=DEADLINE + timedelta(days=1),
                       attachments=[Attachment(id=31, kind=AttachmentKind.DOCUMENT, file_id="BQAD",
                                               file_name="отчёт.pdf", file_size=12345)]),
            TaskEvent(id=41, task_id=done.id, actor_id=manager.id, type=EventType.CREATED,
                      data={"weight": 30, "changes": {"title": ["Старое", "Анализ договоров"]}}),
            TaskEvent(id=42, task_id=active.id, actor_id=None, type=EventType.REMINDER, data={"kind": "before_1d"}),
            ReminderLog(id=51, task_id=active.id, kind="before_1d"),
            DigestLog(id=61, period_start=datetime(2026, 9, 28, 19, 0)),
            JobLog(id=71, job="backup", key="2026-10-01"),
            FsmState(key="fsm:42:2001:2001:default", state="SubmitSG:fact", data={"task_id": 15, "files": []}),
        ])
        await session.commit()


def rows_by_table(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Все строки всех таблиц бота из файла SQLite (сортировка по первичному ключу)."""
    with sqlite3.connect(path) as conn:
        result: dict[str, list[tuple[Any, ...]]] = {}
        for table in Base.metadata.sorted_tables:
            order = ", ".join(column.name for column in table.primary_key.columns)
            result[table.name] = conn.execute(f"SELECT * FROM {table.name} ORDER BY {order}").fetchall()
    conn.close()
    return result


async def open_db(url: str, *, recreate: bool = False) -> AsyncEngine:
    engine = make_engine(url)
    if recreate:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await init_db(engine)
    return engine


@pytest_asyncio.fixture
async def source(tmp_path: Path) -> AsyncIterator[Path]:
    """Рабочая база бота (data/bot.db) с данными; движок закрыт — бот остановлен."""
    path = tmp_path / "old_pc" / "data" / "bot.db"
    engine = await open_db(sqlite_url(path))
    try:
        await seed_source(make_sessionmaker(engine))
    finally:
        await engine.dispose()
    return path


@pytest_asyncio.fixture
async def backup_file(source: Path, tmp_path: Path) -> Path:
    """Файл копии из Telegram (снимок, как его присылает бот)."""
    data = await backup.make_backup_bytes(sqlite_url(source))
    assert data is not None
    path = tmp_path / "Downloads" / "kpi_backup_2026-10-02.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return tmp_path / "new_server" / "data" / "bot.db"


# --- SQLite -> SQLite ------------------------------------------------------------------------------------


async def test_round_trip_sqlite(source: Path, backup_file: Path, target: Path) -> None:
    result = await restore_tool.restore(backup_file, sqlite_url(target))

    assert rows_by_table(target) == rows_by_table(source)
    assert result.counts == {
        "users": 3, "tasks": 2, "submissions": 1, "attachments": 1, "task_events": 2,
        "reminder_log": 1, "digest_log": 1, "job_log": 1, "fsm_state": 1,
    }
    assert result.total == 13 and result.missing_tables == [] and result.cleared_rows == 0
    assert result.fixed_values == 0 and result.sequences_reset is False

    # Бот открывает восстановленную базу: связи на месте, новые записи получают свободные id.
    engine = await open_db(sqlite_url(target))
    try:
        async with make_sessionmaker(engine)() as session:
            task = await session.get(Task, 11)
            assert task is not None and task.assignee.full_name == "Иванов Иван Иванович"
            assert task.submissions[0].attachments[0].file_name == "отчёт.pdf"
            assert task.submissions[0].decision == ReviewDecision.CHANGED
            event = await session.get(TaskEvent, 41)
            assert event is not None and event.data["changes"]["title"] == ["Старое", "Анализ договоров"]
            newcomer = User(tg_id=2002, full_name="Новый Сотрудник", role=Role.EMPLOYEE, status=UserStatus.PENDING)
            session.add(newcomer)
            await session.commit()
            assert newcomer.id == 9
    finally:
        await engine.dispose()


async def test_restore_from_export_file(source: Path, target: Path, tmp_path: Path) -> None:
    """Копия облачной базы (выгрузка через SQLAlchemy) восстанавливается так же, как снимок."""
    engine = make_engine(sqlite_url(source))
    try:
        data = await backup.export_database(engine)
    finally:
        await engine.dispose()
    exported = tmp_path / "kpi_backup_export.db"
    exported.write_bytes(data)

    await restore_tool.restore(exported, sqlite_url(target))
    assert rows_by_table(target) == rows_by_table(source)


async def test_refuses_database_with_users(backup_file: Path, target: Path) -> None:
    engine = await open_db(sqlite_url(target))
    try:
        async with make_sessionmaker(engine)() as session:
            session.add(User(tg_id=1001, full_name="Действующий Начальник", role=Role.MANAGER,
                             status=UserStatus.ACTIVE))
            await session.commit()
    finally:
        await engine.dispose()
    before = rows_by_table(target)

    with pytest.raises(RestoreError, match="--force") as caught:
        await restore_tool.restore(backup_file, sqlite_url(target))
    assert "сотрудников и начальников — 1" in str(caught.value)
    assert rows_by_table(target) == before


async def test_force_replaces_existing_data(source: Path, backup_file: Path, target: Path) -> None:
    engine = await open_db(sqlite_url(target))
    try:
        async with make_sessionmaker(engine)() as session:
            boss = User(tg_id=9001, full_name="Другой Начальник", role=Role.MANAGER, status=UserStatus.ACTIVE)
            session.add(boss)
            await session.flush()
            session.add(Task(title="Чужая задача", expected_result="—", deadline=DEADLINE,
                             assignee_id=boss.id, created_by_id=boss.id))
            session.add(JobLog(job="backup", key="2026-10-01"))  # тот же ключ, что в копии
            await session.commit()
    finally:
        await engine.dispose()

    result = await restore_tool.restore(backup_file, sqlite_url(target), force=True)
    assert result.cleared_rows == 3
    assert rows_by_table(target) == rows_by_table(source)


async def test_service_rows_of_empty_database_do_not_block(source: Path, backup_file: Path, target: Path) -> None:
    """Бот в облаке уже запускался (журнал заданий, диалог), но сотрудников нет — восстановление без --force."""
    engine = await open_db(sqlite_url(target))
    try:
        async with make_sessionmaker(engine)() as session:
            session.add_all([
                JobLog(id=71, job="backup", key="2026-10-01"),
                FsmState(key="fsm:42:2001:2001:default", state=None, data={"x": 1}),
            ])
            await session.commit()
    finally:
        await engine.dispose()

    result = await restore_tool.restore(backup_file, sqlite_url(target))
    assert result.cleared_rows == 2
    assert rows_by_table(target) == rows_by_table(source)


async def test_error_midway_leaves_database_unchanged(
    backup_file: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await open_db(sqlite_url(target))
    try:
        async with make_sessionmaker(engine)() as session:
            session.add(User(tg_id=9001, full_name="Другой Начальник", role=Role.MANAGER, status=UserStatus.ACTIVE))
            await session.commit()
    finally:
        await engine.dispose()
    before = rows_by_table(target)
    original = restore_tool._copy_table

    async def broken(conn: Any, backup_engine: Any, table: Any, columns: list[str], *, fit: bool) -> Any:
        if table.name == "submissions":
            raise sqlite3.OperationalError("disk I/O error")
        return await original(conn, backup_engine, table, columns, fit=fit)

    monkeypatch.setattr(restore_tool, "_copy_table", broken)
    with pytest.raises(Exception, match="disk I/O error"):
        await restore_tool.restore(backup_file, sqlite_url(target), force=True)
    assert rows_by_table(target) == before


async def test_old_backup_without_new_tables(source: Path, backup_file: Path, target: Path) -> None:
    """Копия от старой версии бота (до таблиц job_log и fsm_state) — восстанавливается, их нет в отчёте."""
    with sqlite3.connect(backup_file) as conn:
        conn.execute("DROP TABLE job_log")
        conn.execute("DROP TABLE fsm_state")
        conn.execute("ALTER TABLE attachments DROP COLUMN mime_type")  # колонка, которой ещё не было
    conn.close()

    result = await restore_tool.restore(backup_file, sqlite_url(target))
    assert sorted(result.missing_tables) == ["fsm_state", "job_log"]
    restored = rows_by_table(target)
    assert restored["users"] == rows_by_table(source)["users"]
    assert restored["job_log"] == [] and restored["fsm_state"] == []
    summary = restore_tool.format_summary(backup_file, result)
    assert "В копии не было таблиц" in summary and "fsm_state):" not in summary and "job_log):" not in summary


@pytest.mark.parametrize("problem", ["missing", "not_sqlite", "no_bot_tables", "same_file"])
async def test_invalid_backup_file(problem: str, backup_file: Path, tmp_path: Path, target: Path) -> None:
    path = tmp_path / "bad.db"
    database = target
    if problem == "missing":
        message = "не найден"
    elif problem == "not_sqlite":
        path.write_text("это не база", encoding="utf-8")
        message = "не резервная копия бота"
    elif problem == "no_bot_tables":
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY)")
        conn.close()
        message = "нет таблиц бота"
    else:
        path, database, message = backup_file, backup_file, "один и тот же файл"

    with pytest.raises(RestoreError, match=message):
        await restore_tool.restore(path, sqlite_url(database))
    if problem != "same_file":
        assert not target.exists()


# --- PostgreSQL ------------------------------------------------------------------------------------------------


def test_values_fitted_to_postgres_limits() -> None:
    """PostgreSQL строже SQLite: длинные строки, NUL, целые вне INTEGER и NaN в JSON поправляются."""
    users, tasks = Base.metadata.tables["users"], Base.metadata.tables["tasks"]
    attachments, events = Base.metadata.tables["attachments"], Base.metadata.tables["task_events"]

    rows, fixed = restore_tool._fit_rows(users, [
        {"id": 1, "tg_id": 5_000_000_001, "username": "u" * 70, "full_name": "Иванов\x00 Иван",
         "role": Role.MANAGER, "status": UserStatus.ACTIVE},
    ])
    assert rows[0]["username"] == "u" * 64 and rows[0]["full_name"] == "Иванов Иван"
    assert rows[0]["tg_id"] == 5_000_000_001 and rows[0]["role"] is Role.MANAGER
    assert fixed == 2

    rows, fixed = restore_tool._fit_rows(tasks, [{"id": 1, "title": "Т" * 300, "weight": 30}])
    assert len(rows[0]["title"]) == 255 and fixed == 1

    rows, fixed = restore_tool._fit_rows(attachments, [
        {"id": 1, "file_name": "очень_" * 60 + ".pdf", "file_size": 3 * 2**30},
    ])
    assert len(rows[0]["file_name"]) == 255 and rows[0]["file_name"].endswith("….pdf")
    assert rows[0]["file_size"] == 2**31 - 1 and fixed == 2

    rows, fixed = restore_tool._fit_rows(events, [{"id": 1, "data": {"score": float("nan"), "ok": [1.5]}}])
    assert rows[0]["data"] == {"score": None, "ok": [1.5]} and fixed == 1


async def test_postgres_sequences_statements() -> None:
    """После вставки с явными id — setval(max(id) + 1) для каждой таблицы с автоинкрементом (без сервера)."""
    from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

    executed: list[tuple[str, dict[str, Any]]] = []

    class FakeConnection:
        dialect = PGDialect_asyncpg()

        async def execute(self, statement: Any, params: dict[str, Any]) -> None:
            executed.append((str(statement), params))

    await restore_tool._reset_sequences(FakeConnection())  # type: ignore[arg-type]
    tables = [params["table"] for _sql, params in executed]
    assert tables == [name for name in TABLES if name != "fsm_state"]  # у fsm_state ключ — строка
    sql, params = next((sql, params) for sql, params in executed if params["table"] == "users")
    assert sql == "SELECT setval(pg_get_serial_sequence(:table, :column), COALESCE(MAX(id), 0) + 1, false) FROM users"
    assert params == {"table": "users", "column": "id"}


@pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL не задан — проверка на PostgreSQL пропущена")
async def test_round_trip_postgres(source: Path, backup_file: Path, tmp_path: Path) -> None:
    """SQLite-копия -> PostgreSQL -> копия PostgreSQL (выгрузка) — те же строки; новые id после max(id)."""
    engine = await open_db(PG_URL, recreate=True)
    await engine.dispose()

    result = await restore_tool.restore(backup_file, PG_URL)
    assert result.sequences_reset is True and result.total == 13

    with pytest.raises(RestoreError, match="--force"):
        await restore_tool.restore(backup_file, PG_URL)
    result = await restore_tool.restore(backup_file, PG_URL, force=True)
    assert result.cleared_rows == 13

    engine = make_engine(PG_URL)
    try:
        data = await backup.export_database(engine)
        async with make_sessionmaker(engine)() as session:
            newcomer = User(tg_id=2002, full_name="Новый Сотрудник", role=Role.EMPLOYEE, status=UserStatus.PENDING)
            session.add(newcomer)
            await session.flush()
            assert newcomer.id == 9
            task = Task(title="Новая", expected_result="—", deadline=DEADLINE, assignee_id=7, created_by_id=3)
            session.add(task)
            await session.flush()
            assert task.id == 16
            await session.rollback()
            assert await session.scalar(text("SELECT count(*) FROM users")) == 3
            assert (await session.scalars(select(Task.id).order_by(Task.id))).all() == [11, 15]
    finally:
        await engine.dispose()
    exported = tmp_path / "from_postgres.db"
    exported.write_bytes(data)
    assert rows_by_table(exported) == rows_by_table(source)


@pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL не задан — проверка на PostgreSQL пропущена")
async def test_cli_postgres_with_separate_password(
    backup_file: Path,
    source: Path,
    tmp_path: Path,
    set_env: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Как у начальника: DATABASE_URL — строка Supabase с «[YOUR-PASSWORD]», пароль — в DATABASE_PASSWORD."""
    from urllib.parse import quote

    from bot.db.base import describe_url, normalize_url

    real = normalize_url(PG_URL)
    if not real.password:
        pytest.skip("в TEST_DATABASE_URL нет пароля — подстановку DATABASE_PASSWORD нечем проверить")
    engine = await open_db(PG_URL, recreate=True)
    await engine.dispose()
    port = f":{real.port}" if real.port else ""
    url = f"postgresql://{quote(real.username or '', safe='')}:[YOUR-PASSWORD]@{real.host}{port}/{real.database}"
    set_env(DATABASE_URL=url, DATABASE_PASSWORD=real.password)

    assert await run_cli([str(backup_file)]) == 0
    captured = capsys.readouterr()
    assert "Всего: 13." in captured.out and "Счётчики номеров (id) PostgreSQL сдвинуты" in captured.out
    if real.password not in describe_url(PG_URL):  # короткий пароль вроде «postgres» совпал бы с именем
        assert real.password not in captured.out + captured.err

    engine = make_engine(PG_URL)
    try:
        async with make_sessionmaker(engine)() as session:
            assert (await session.scalars(select(User.id).order_by(User.id))).all() == [3, 7, 8]
        data = await backup.export_database(engine)
    finally:
        await engine.dispose()
    exported = tmp_path / "from_postgres.db"
    exported.write_bytes(data)
    assert rows_by_table(exported) == rows_by_table(source)


# --- Командная строка --------------------------------------------------------------------------------------------


async def run_cli(argv: list[str]) -> int:
    """main() запускает свой asyncio.run — в тесте (уже внутри цикла событий) — в отдельном потоке."""
    import asyncio

    return await asyncio.to_thread(restore_tool.main, argv)


async def test_cli_restores_and_prints_summary(
    backup_file: Path, target: Path, set_env: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    set_env(DATABASE_URL=sqlite_url(target))
    assert await run_cli([str(backup_file)]) == 0
    out = capsys.readouterr().out
    assert "Готово: копия kpi_backup_2026-10-02.db загружена" in out
    assert "задачи (tasks): 2" in out and "сотрудники и начальники (users): 3" in out
    assert "Всего: 13." in out

    assert await run_cli([str(backup_file)]) == 1  # второй раз — база уже не пустая
    err = capsys.readouterr().err
    assert "Ошибка:" in err and "--force" in err

    assert await run_cli([str(backup_file), "--force"]) == 0
    assert "Перед загрузкой из базы удалено строк: 13." in capsys.readouterr().out


async def test_cli_connection_error_hides_password(
    backup_file: Path, set_env: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    set_env(DATABASE_URL="postgresql://postgres.ref:TopSecret42@127.0.0.1:1/postgres")
    assert await run_cli([str(backup_file)]) == 1
    captured = capsys.readouterr()
    assert "TopSecret42" not in captured.out + captured.err
    assert "postgres.ref:***@127.0.0.1:1/postgres" in captured.out
    assert "не удалось подключиться к базе" in captured.err and "база не изменена" in captured.err


def test_cli_requires_backup_path(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        restore_tool.main([])
    assert caught.value.code == 2
    assert "backup" in capsys.readouterr().err
