"""Ежедневная резервная копия базы руководителям в Telegram (bot/scheduler/backup.py, SPEC §8, §10.8).

* снимок файловой базы (online backup API SQLite) содержит все таблицы и строки и открывается ботом;
* база не в файле (PostgreSQL) — выгрузка всех таблиц во временный файл SQLite с той же схемой:
  строка в строку как в базе, открывается ботом, временные файлы не остаются, предел размера;
* send_backup отправляет по одному документу каждому активному руководителю и ничего — сотрудникам,
  неактивным руководителям и заблокировавшим бота; никогда не бросает исключений;
* база больше предела Telegram — файл не отправляется, руководителей предупреждают один раз;
* BACKUP_ENABLED=false — задание в планировщике не регистрируется.

Telegram — фейковый (tests/e2e/fakebot.py FakeSession): настоящий aiogram Bot без сети.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram import methods as m
from aiogram.client.default import DefaultBotProperties
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.config import get_settings
from bot.db.base import Base, init_db, make_engine, make_sessionmaker
from bot.db.models import (
    Attachment,
    AttachmentKind,
    DigestLog,
    FsmState,
    JobLog,
    ReminderLog,
    ReviewDecision,
    Role,
    Submission,
    Task,
    User,
    UserStatus,
)
from bot.scheduler import backup
from bot.services import tasks as tasks_svc
from bot.utils.dates import utcnow
from e2e.fakebot import FakeSession

# 02.10.2026 23:00 по Ташкенту (UTC+5) — время копии по умолчанию (BACKUP_HOUR=23).
BACKUP_AT = datetime(2026, 10, 2, 18, 0)
CAPTION = (
    "💾 Резервная копия базы за 02.10.2026. "
    "Храните этот файл: из него можно восстановить все задачи и оценки."
)
FILENAME = "kpi_backup_2026-10-02.db"

MGR, MGR2, MGR_BLOCKED, MGR_PENDING = 1001, 1002, 1003, 1004
EMP, EMP2 = 2001, 2002
TASK_TITLE = "Анализ договоров"


# --- Окружение ---------------------------------------------------------------------------------


@dataclass
class FileDb:
    url: str
    path: Path
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Каждый тест начинает с чистого «руководителей уже предупредили о размере»."""
    monkeypatch.setattr(backup, "_too_big_warned", set())


@pytest_asyncio.fixture
async def file_db(tmp_path: Path) -> AsyncIterator[FileDb]:
    """Файловая база, как у бота (data/bot.db, WAL): движок остаётся открытым во время снимка."""
    path = tmp_path / "data" / "bot.db"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    engine = make_engine(url)
    try:
        await init_db(engine)
        yield FileDb(url, path, engine, make_sessionmaker(engine))
    finally:
        await engine.dispose()


async def seed(db: FileDb) -> None:
    """Два активных руководителя, заблокированный и ожидающий руководители, два сотрудника и задача."""
    async with db.sessionmaker() as session:
        people = [
            User(tg_id=MGR, full_name="Петрова Анна Сергеевна", role=Role.MANAGER, status=UserStatus.ACTIVE),
            User(tg_id=MGR2, full_name="Смирнов Олег Петрович", role=Role.MANAGER, status=UserStatus.ACTIVE),
            User(tg_id=MGR_BLOCKED, full_name="Козлов Иван Ильич", role=Role.MANAGER, status=UserStatus.BLOCKED),
            User(tg_id=MGR_PENDING, full_name="Орлов Пётр Ильич", role=Role.MANAGER, status=UserStatus.PENDING),
            User(tg_id=EMP, full_name="Иванов Иван Иванович", position="Юрист",
                 role=Role.EMPLOYEE, status=UserStatus.ACTIVE),
            User(tg_id=EMP2, full_name="Сидорова Мария Олеговна", position="Экономист",
                 role=Role.EMPLOYEE, status=UserStatus.ACTIVE),
        ]
        session.add_all(people)
        await session.flush()
        await tasks_svc.create_task(
            session,
            creator=people[0],
            assignee_id=people[4].id,
            title=TASK_TITLE,
            expected_result="Проверить 100 договоров и представить отчёт",
            deadline=utcnow() + timedelta(days=5),
            weight=30,
            plan_value=100,
            plan_unit="договоров",
        )
        await session.commit()


@pytest_asyncio.fixture
async def bot() -> AsyncIterator[Bot]:
    fake = Bot("42:TEST", session=FakeSession(), default=DefaultBotProperties(parse_mode="HTML"))
    try:
        yield fake
    finally:
        await fake.session.close()


def api(bot: Bot) -> FakeSession:
    assert isinstance(bot.session, FakeSession)
    return bot.session


async def seed_everything(db: FileDb) -> None:
    """seed + строка в каждой таблице бота: сдача с файлом, журналы, задание, незавершённый диалог —
    все типы колонок (даты, JSON, перечисления, флаги, дробные числа)."""
    await seed(db)
    async with db.sessionmaker() as session:
        task = await session.scalar(select(Task))
        manager = await session.scalar(select(User).where(User.tg_id == MGR))
        assert task is not None and manager is not None
        submission = Submission(
            task_id=task.id,
            attempt=1,
            fact_text="Проверено 110 договоров",
            result_text="Отчёт в приложении",
            fact_value=110.0,
            deadline_at_submit=task.deadline,
            is_late=True,
            late_days=1.5,
            ai_score=104.0,
            ai_rationale="План перевыполнен",
            ai_source="rules",
            final_score=100.0,
            decision=ReviewDecision.CHANGED,
            review_comment="Хорошо",
            reviewer_id=manager.id,
            reviewed_at=utcnow(),
            attachments=[
                Attachment(kind=AttachmentKind.DOCUMENT, file_id="BQAD", file_name="отчёт.pdf",
                           mime_type="application/pdf", file_size=12345),
            ],
        )
        session.add_all([
            submission,
            ReminderLog(task_id=task.id, kind="before_3d"),
            DigestLog(period_start=datetime(2026, 9, 28, 19, 0)),
            JobLog(job="backup", key="2026-10-01"),
            FsmState(key="fsm:42:2001:2001:default", state="SubmitSG:fact", data={"task_id": task.id, "files": []}),
        ])
        await session.commit()


def table_rows(conn: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    primary_key = ", ".join(column.name for column in Base.metadata.tables[table].primary_key.columns)
    return conn.execute(f"SELECT * FROM {table} ORDER BY {primary_key}").fetchall()


def restore(data: bytes, path: Path) -> sqlite3.Connection:
    """Записать файл копии на диск (как его скачает руководитель) и открыть."""
    path.write_bytes(data)
    return sqlite3.connect(path)


# --- Снимок базы ---------------------------------------------------------------------------------


async def test_snapshot_contains_tables_and_rows(file_db: FileDb, tmp_path: Path) -> None:
    """Снимок файловой базы, пока бот с ней работает (движок открыт, данные ещё в WAL), — полная
    база SQLite: все таблицы бота, пользователи, задача и её журнал."""
    await seed(file_db)
    data = await backup.make_backup_bytes(file_db.url)

    assert data is not None and data.startswith(b"SQLite format 3\x00")
    with restore(data, tmp_path / "restored.db") as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert set(Base.metadata.tables) <= tables
        names = {row[0] for row in conn.execute("SELECT full_name FROM users")}
        assert {"Петрова Анна Сергеевна", "Иванов Иван Иванович", "Сидорова Мария Олеговна"} <= names
        assert conn.execute("SELECT title, weight, plan_value FROM tasks").fetchall() == [(TASK_TITLE, 30, 100.0)]
        assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] >= 1
    conn.close()


async def test_restored_backup_opens_in_bot(file_db: FileDb, tmp_path: Path) -> None:
    """Восстановление по README: файл копии кладут как data/bot.db — бот видит задачи и сотрудников."""
    await seed(file_db)
    data = await backup.make_backup_bytes(file_db.url)
    assert data is not None
    restored = tmp_path / "new_server" / "data" / "bot.db"
    restored.parent.mkdir(parents=True)
    restored.write_bytes(data)

    engine = make_engine(f"sqlite+aiosqlite:///{restored.as_posix()}")
    try:
        await init_db(engine)  # как при запуске бота: существующие таблицы не трогаются
        async with make_sessionmaker(engine)() as session:
            task = await session.scalar(select(Task))
            assert task is not None and task.title == TASK_TITLE
            assert task.assignee.full_name == "Иванов Иван Иванович"
            assert len(list(await session.scalars(select(User)))) == 6
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite://",
        "not a database url",
    ],
)
async def test_no_snapshot_for_in_memory_or_invalid_databases(url: str) -> None:
    assert await backup.make_backup_bytes(url) is None


async def test_no_snapshot_for_in_memory_engine(memory_engine: AsyncEngine) -> None:
    assert await backup.make_backup_bytes(memory_engine) is None


async def test_sqlite_engine_uses_snapshot_not_export(file_db: FileDb, monkeypatch: pytest.MonkeyPatch) -> None:
    """Движок бота на SQLite в файле — тот же снимок online backup, что и по адресу."""
    await seed(file_db)

    async def no_export(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("SQLite в файле не выгружается построчно")

    monkeypatch.setattr(backup, "export_database", no_export)
    data = await backup.make_backup_bytes(file_db.engine)
    assert data is not None and data.startswith(b"SQLite format 3\x00")


# --- Выгрузка базы не в файле (PostgreSQL) ----------------------------------------------------------------


async def test_export_matches_database_row_by_row(file_db: FileDb, tmp_path: Path) -> None:
    """Выгрузка через SQLAlchemy — файл SQLite с той же схемой, каждая таблица бота строка в строку
    совпадает с базой (id сохранены, даты, JSON, перечисления, флаги и числа — как были)."""
    await seed_everything(file_db)
    data = await backup.export_database(file_db.engine)

    assert data.startswith(b"SQLite format 3\x00")
    source = sqlite3.connect(file_db.path)
    exported = restore(data, tmp_path / "exported.db")
    try:
        assert exported.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert exported.execute("PRAGMA journal_mode").fetchone() != ("wal",)  # один файл, без -wal
        for table in Base.metadata.sorted_tables:
            rows = table_rows(source, table.name)
            assert rows, f"в тестовых данных нет строк {table.name}"
            assert table_rows(exported, table.name) == rows, table.name
    finally:
        source.close()
        exported.close()


async def test_exported_backup_opens_in_bot(file_db: FileDb, tmp_path: Path) -> None:
    """Файл выгрузки кладут как data/bot.db — бот видит задачи, сдачи, файлы и сотрудников."""
    await seed_everything(file_db)
    data = await backup.export_database(file_db.engine)
    restored = tmp_path / "pc" / "data" / "bot.db"
    restored.parent.mkdir(parents=True)
    restored.write_bytes(data)

    engine = make_engine(f"sqlite+aiosqlite:///{restored.as_posix()}")
    try:
        await init_db(engine)
        async with make_sessionmaker(engine)() as session:
            task = await session.scalar(select(Task))
            assert task is not None and task.title == TASK_TITLE
            assert task.assignee.full_name == "Иванов Иван Иванович"
            [submission] = task.submissions
            assert submission.decision == ReviewDecision.CHANGED and submission.is_late is True
            assert submission.attachments[0].file_name == "отчёт.pdf"
            assert len(list(await session.scalars(select(User)))) == 6
    finally:
        await engine.dispose()


async def test_export_from_in_memory_engine(engine: AsyncEngine, session: AsyncSession, tmp_path: Path) -> None:
    """Выгрузка работает с любой базой SQLAlchemy — в том числе с той, у которой нет файла."""
    session.add(User(tg_id=MGR, full_name="Петрова Анна Сергеевна", role=Role.MANAGER, status=UserStatus.ACTIVE))
    await session.commit()
    data = await backup.export_database(engine)
    with restore(data, tmp_path / "memory.db") as conn:
        assert conn.execute("SELECT tg_id, role, status FROM users").fetchall() == [(MGR, "manager", "active")]
    conn.close()


async def test_export_size_limit_and_no_temp_files_left(
    file_db: FileDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Копия больше предела — BackupTooLarge сразу; временный файл удаляется и при успехе, и при ошибке."""
    await seed_everything(file_db)
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))

    with pytest.raises(backup.BackupTooLarge) as caught:
        await backup.export_database(file_db.engine, max_bytes=1024)
    assert caught.value.size > 1024 and caught.value.limit == 1024
    assert list(temp_dir.iterdir()) == []

    data = await backup.export_database(file_db.engine, max_bytes=backup.MAX_BACKUP_BYTES)
    assert data.startswith(b"SQLite format 3\x00")
    assert list(temp_dir.iterdir()) == []


async def test_postgres_url_is_exported_through_temporary_engine(
    file_db: FileDb, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DATABASE_URL PostgreSQL (как его даёт Supabase) — движок asyncpg на время выгрузки, затем закрывается."""
    await seed(file_db)
    opened: list[object] = []

    def fake_make_engine(url: object) -> AsyncEngine:
        opened.append(url)
        return file_db.engine  # вместо сервера PostgreSQL — та же база, выгрузка через SQLAlchemy

    monkeypatch.setattr(backup, "make_engine", fake_make_engine)
    data = await backup.make_backup_bytes(
        "postgresql://postgres.ref:secret@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    )

    assert len(opened) == 1 and opened[0].drivername == "postgresql+asyncpg"  # type: ignore[attr-defined]
    assert data is not None
    with restore(data, tmp_path / "pg.db") as conn:
        assert conn.execute("SELECT title FROM tasks").fetchall() == [(TASK_TITLE,)]
    conn.close()


async def test_no_snapshot_and_no_empty_db_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "data" / "bot.db"
    assert await backup.make_backup_bytes(f"sqlite+aiosqlite:///{missing.as_posix()}") is None
    assert not missing.exists()


async def test_snapshot_size_limit(file_db: FileDb) -> None:
    await seed(file_db)
    with pytest.raises(backup.BackupTooLarge) as caught:
        await backup.make_backup_bytes(file_db.url, max_bytes=1024)
    assert caught.value.size > 1024 and caught.value.limit == 1024
    data = await backup.make_backup_bytes(file_db.url, max_bytes=backup.MAX_BACKUP_BYTES)
    assert data is not None and len(data) == caught.value.size


def test_backup_filename_uses_local_date() -> None:
    assert backup.backup_filename(BACKUP_AT) == FILENAME
    # 19:30 UTC — уже 00:30 следующего дня по Ташкенту.
    assert backup.backup_filename(datetime(2026, 10, 2, 19, 30)) == "kpi_backup_2026-10-03.db"


# --- Отправка руководителям ---------------------------------------------------------------------------


async def test_send_backup_one_document_per_active_manager(file_db: FileDb, bot: Bot, tmp_path: Path) -> None:
    """Каждому активному руководителю — один документ (имя с датой, подпись, без звука);
    сотрудникам, заблокированному и не подтверждённому руководителю — ничего."""
    await seed(file_db)
    sent = await backup.send_backup(bot, file_db.sessionmaker, now=BACKUP_AT)

    assert sent == 2
    session = api(bot)
    assert not session.errors
    assert {call.chat_id for call in session.calls} == {MGR, MGR2}
    documents = [request for request in session.requests if isinstance(request, m.SendDocument)]
    assert len(documents) == len(session.requests) == 2
    assert all(request.disable_notification for request in documents)

    for chat_id in (MGR, MGR2):
        [file] = [item for item in session.sent_files if item.chat_id == chat_id]
        assert file.kind == "document" and file.file_name == FILENAME
        assert file.caption == CAPTION
        assert file.content is not None
        with restore(file.content, tmp_path / f"from_{chat_id}.db") as conn:
            assert conn.execute("SELECT title FROM tasks").fetchall() == [(TASK_TITLE,)]
        conn.close()
    for chat_id in (EMP, EMP2, MGR_BLOCKED, MGR_PENDING):
        assert not [item for item in session.sent_files if item.chat_id == chat_id]

    # Файл загружается один раз: второму руководителю уходит тот же file_id.
    first, second = documents
    assert not isinstance(first.document, str) and second.document == session.sent_files[0].file_id


async def test_send_backup_survives_blocked_manager(file_db: FileDb, bot: Bot) -> None:
    """Руководитель заблокировал бота — остальным копия всё равно приходит, исключений нет."""
    await seed(file_db)
    api(bot).blocked_chats.add(MGR)

    assert await backup.send_backup(bot, file_db.sessionmaker, now=BACKUP_AT) == 1
    assert [item.chat_id for item in api(bot).sent_files] == [MGR2]
    assert api(bot).sent_files[0].file_name == FILENAME


async def test_send_backup_too_big_warns_managers_once(
    file_db: FileDb, bot: Bot, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """База больше предела Telegram: файл не отправляется, в лог — предупреждение, руководителям —
    одно сообщение (на следующий день не повторяется). Удачная копия снимает отметку."""
    await seed(file_db)
    monkeypatch.setattr(backup, "MAX_BACKUP_BYTES", 1024)

    with caplog.at_level(logging.WARNING, logger=backup.__name__):
        assert await backup.send_backup(bot, file_db.sessionmaker, now=BACKUP_AT) == 0
    assert any("не отправлена" in record.getMessage() for record in caplog.records)
    session = api(bot)
    assert not session.sent_files and not session.errors
    warnings = [request for request in session.requests if isinstance(request, m.SendMessage)]
    assert sorted(request.chat_id for request in warnings) == [MGR, MGR2]
    assert all("02.10.2026 не отправлена" in request.text and "data" in request.text for request in warnings)
    assert all(request.disable_notification for request in warnings)  # 23:00 — тихие часы

    session.requests.clear()
    assert await backup.send_backup(bot, file_db.sessionmaker, now=BACKUP_AT + timedelta(days=1)) == 0
    assert session.requests == []  # уже предупредили

    monkeypatch.setattr(backup, "MAX_BACKUP_BYTES", 45 * 1024 * 1024)
    assert await backup.send_backup(bot, file_db.sessionmaker, now=BACKUP_AT + timedelta(days=2)) == 2
    assert backup._too_big_warned == set()


async def test_send_backup_server_database_sends_export(
    file_db: FileDb, bot: Bot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """База не в файле (PostgreSQL): копия читается через движок бота (без нового подключения) и уходит
    руководителям тем же документом — файлом SQLite, из которого восстанавливаются все задачи."""
    await seed_everything(file_db)
    sources: list[object] = []

    async def as_server_database(database: object, *, max_bytes: int | None = None) -> bytes | None:
        sources.append(database)
        assert isinstance(database, AsyncEngine)
        return await backup.export_database(database, max_bytes=max_bytes)

    monkeypatch.setattr(backup, "make_backup_bytes", as_server_database)
    assert await backup.send_backup(bot, file_db.sessionmaker, now=BACKUP_AT) == 2
    assert sources == [file_db.engine]

    session = api(bot)
    assert [item.chat_id for item in session.sent_files] == [MGR, MGR2]
    file = session.sent_files[0]
    assert file.file_name == FILENAME and file.caption == CAPTION and file.content is not None
    with restore(file.content, tmp_path / "from_pg.db") as conn:
        assert conn.execute("SELECT title FROM tasks").fetchall() == [(TASK_TITLE,)]
        assert conn.execute("SELECT count(*) FROM attachments").fetchone() == (1,)
    conn.close()


async def test_send_backup_from_real_postgres(pg_engine: AsyncEngine, bot: Bot, tmp_path: Path) -> None:
    """Настоящий PostgreSQL (как Supabase): выгрузка через пул бота (REPEATABLE READ) -> файл SQLite,
    каждая таблица строка в строку как в базе (id, даты, JSON, перечисления, дробные числа)."""
    sessionmaker = make_sessionmaker(pg_engine)
    await seed_everything(SimpleNamespace(sessionmaker=sessionmaker))  # type: ignore[arg-type]
    assert await backup.send_backup(bot, sessionmaker, now=BACKUP_AT) == 2
    files = api(bot).sent_files
    assert [item.chat_id for item in files] == [MGR, MGR2] and files[0].file_name == FILENAME
    assert files[0].content is not None
    path = tmp_path / FILENAME
    path.write_bytes(files[0].content)
    exported = make_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        for table in Base.metadata.sorted_tables:
            query = select(table).order_by(*table.primary_key.columns)
            async with pg_engine.connect() as conn:
                expected = (await conn.execute(query)).all()
            async with exported.connect() as conn:
                actual = (await conn.execute(query)).all()
            assert expected, f"в тестовых данных нет строк {table.name}"
            assert actual == expected, table.name
    finally:
        await exported.dispose()


async def test_send_backup_server_database_too_big(
    file_db: FileDb, bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Копия PostgreSQL больше предела — предупреждение без совета про папку data (её нет), с pg_dump."""
    await seed(file_db)
    monkeypatch.setattr(backup, "MAX_BACKUP_BYTES", 1024)
    monkeypatch.setattr(backup, "_is_file_database", lambda _database: False)

    async def as_server_database(database: AsyncEngine, *, max_bytes: int | None = None) -> bytes | None:
        return await backup.export_database(database, max_bytes=max_bytes)

    monkeypatch.setattr(backup, "make_backup_bytes", as_server_database)
    assert await backup.send_backup(bot, file_db.sessionmaker, now=BACKUP_AT) == 0
    warnings = [request for request in api(bot).requests if isinstance(request, m.SendMessage)]
    assert sorted(request.chat_id for request in warnings) == [MGR, MGR2]
    assert all("02.10.2026 не отправлена" in request.text and "pg_dump" in request.text for request in warnings)
    assert not any("<code>data</code>" in request.text for request in warnings)
    assert not api(bot).sent_files


async def test_send_backup_in_memory_database_sends_nothing(
    memory_sessionmaker: async_sessionmaker[AsyncSession], bot: Bot
) -> None:
    """База в памяти (не в файле) — копировать нечего: ничего не отправляется, ошибок нет.

    Всегда in-memory SQLite (memory_sessionmaker): с TEST_DATABASE_URL фикстура sessionmaker — PostgreSQL,
    а его копия как раз делается (test_send_backup_server_database_sends_export).
    """
    async with memory_sessionmaker() as session:
        session.add(User(tg_id=MGR, full_name="Петрова Анна Сергеевна", role=Role.MANAGER, status=UserStatus.ACTIVE))
        await session.commit()
    assert await backup.send_backup(bot, memory_sessionmaker, now=BACKUP_AT) == 0
    assert api(bot).requests == []


async def test_send_backup_never_raises(
    file_db: FileDb, bot: Bot, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Сбой снимка (диск, повреждённая база) — запись в лог, 0, бот продолжает работать."""
    await seed(file_db)

    async def broken(*_args: object, **_kwargs: object) -> bytes:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(backup, "make_backup_bytes", broken)
    with caplog.at_level(logging.ERROR, logger=backup.__name__):
        assert await backup.send_backup(bot, file_db.sessionmaker, now=BACKUP_AT) == 0
    assert any(record.exc_info for record in caplog.records)
    assert api(bot).requests == []


async def test_send_backup_without_managers(file_db: FileDb, bot: Bot) -> None:
    assert await backup.send_backup(bot, file_db.sessionmaker, now=BACKUP_AT) == 0
    assert api(bot).requests == []


# --- Планировщик ---------------------------------------------------------------------------------


async def test_backup_job_registered(
    bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], set_env: Callable[..., None]
) -> None:
    """BACKUP_ENABLED (по умолчанию) — ежедневно в BACKUP_HOUR:00 по TIMEZONE; неверный час — 23:00."""
    from bot.scheduler.jobs import setup_scheduler

    def fields(job: object) -> dict[str, str]:
        return {field.name: str(field) for field in job.trigger.fields}  # type: ignore[attr-defined]

    set_env(BACKUP_ENABLED="true", BACKUP_HOUR="23")
    assert get_settings().backup_enabled and get_settings().backup_hour == 23
    job = setup_scheduler(bot, sessionmaker).get_job("backup")
    assert job is not None and job.func is backup.send_backup
    assert job.args == (bot, sessionmaker)
    trigger = fields(job)
    assert (trigger["hour"], trigger["minute"], trigger["day_of_week"]) == ("23", "0", "*")
    assert str(job.trigger.timezone) == "Asia/Tashkent"
    assert job.coalesce is True and job.max_instances == 1
    assert 30 * 60 <= job.misfire_grace_time <= 2 * 3600

    set_env(BACKUP_HOUR="7")
    assert fields(setup_scheduler(bot, sessionmaker).get_job("backup"))["hour"] == "7"
    set_env(BACKUP_HOUR="30")
    assert fields(setup_scheduler(bot, sessionmaker).get_job("backup"))["hour"] == "23"


async def test_backup_job_not_registered_when_disabled(
    bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], set_env: Callable[..., None]
) -> None:
    from bot.scheduler.jobs import setup_scheduler

    set_env(BACKUP_ENABLED="false")
    scheduler = setup_scheduler(bot, sessionmaker)
    assert scheduler.get_job("backup") is None
    assert scheduler.get_job("reminders") is not None and scheduler.get_job("weekly_digest") is not None
