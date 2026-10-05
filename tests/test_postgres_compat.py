"""Совместимость с PostgreSQL (Render + Supabase): то, в чём PostgreSQL строже или иначе, чем SQLite.

* Длины VARCHAR, NUL-символы, int32 и отрицательные LIMIT/OFFSET: SQLite всё это молча принимает,
  PostgreSQL — DataError. Сервисы обрезают/очищают данные заранее (bot.services.dbsafe).
* NULL в сортировках: SQLite ставит NULL первыми по возрастанию и последними по убыванию,
  PostgreSQL — наоборот. Порядок списков на обеих базах должен совпадать.
* Гонки: две настоящие сессии (свои соединения и транзакции). Условный UPDATE второй сессии ждёт
  commit первой и не проходит; отметка напоминания (ON CONFLICT DO NOTHING) не обрывает транзакцию.
* make_engine: адрес в стиле libpq (sslmode, connect_timeout, options...), init_db двумя экземплярами
  бота одновременно, RLS на таблицах (Supabase Data API).
* DATABASE_PASSWORD: строка Supabase без пароля или с заглушкой «[YOUR-PASSWORD]» + пароль отдельно
  (любые спецсимволы, без экранирования) — бот подставляет его сам и нигде не показывает.

Тесты с фикстурой ``pg_engine`` пропускаются без TEST_DATABASE_URL (локальный PostgreSQL:
``TEST_DATABASE_URL=postgresql://postgres@localhost:5432/kpi_test pytest``). Тесты с фикстурой
``engine`` идут на той базе, на которой запущен весь набор; разбор адресов — без базы.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import ssl
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import AsyncAdaptedQueuePool, StaticPool

from bot.config import get_settings
from bot.db.base import (
    Base,
    apply_password,
    describe_url,
    init_db,
    is_password_placeholder,
    is_postgres_url,
    make_engine,
    make_sessionmaker,
    make_storage_engine,
    normalize_url,
    postgres_connect_args,
)
from bot.db.models import (
    AttachmentKind,
    EventType,
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
from bot.services import dbsafe, reminders, users
from bot.services import tasks as svc
from bot.services.errors import DomainError
from bot.services.tasks import PROPOSAL_ALREADY_PROCESSED, REVIEW_ALREADY_PROCESSED, AttachmentIn

pytestmark = pytest.mark.usefixtures("clock")

Sessionmaker = async_sessionmaker[AsyncSession]
LOCK_WAIT_SEC = 0.3  # за это время сессия, ждущая блокировку строки, точно не завершится


# --- Общие данные -----------------------------------------------------------------------------------


@dataclass
class Team:
    boss_id: int      # руководитель из ADMIN_IDS
    deputy_id: int    # второй руководитель
    employee_id: int


async def seed_team(sm: Sessionmaker) -> Team:
    async with sm() as s:
        boss, _ = await users.register_or_get(s, 1001, "boss", "Петров Пётр Петрович")
        deputy = User(tg_id=1002, full_name="Смирнова Ольга Ивановна", role=Role.MANAGER, status=UserStatus.ACTIVE)
        employee = User(tg_id=2001, full_name="Иванов Иван Иванович", role=Role.EMPLOYEE, status=UserStatus.ACTIVE)
        s.add_all([deputy, employee])
        await s.commit()
        return Team(boss.id, deputy.id, employee.id)


async def new_task(s: AsyncSession, team: Team, now: datetime, **overrides: Any) -> Task:
    boss = await s.get(User, team.boss_id)
    values: dict[str, Any] = {
        "creator": boss,
        "assignee_id": team.employee_id,
        "title": "Анализ договоров",
        "expected_result": "Проверить 100 договоров",
        "deadline": now + timedelta(days=3),
        "weight": 20,
    }
    values.update(overrides)
    return await svc.create_task(s, **values)


async def submitted_task(sm: Sessionmaker, team: Team, now: datetime) -> tuple[int, int]:
    """Задача на проверке с предварительной оценкой 100 %: (task_id, sub_id)."""
    async with sm() as s:
        task = await new_task(s, team, now)
        employee = await s.get(User, team.employee_id)
        sub = await svc.submit_result(s, task.id, employee, fact_text="Проверено 100 договоров")
        await svc.record_evaluation(s, sub.id, score=100, rationale="План выполнен", source="rules")
        await s.commit()
        return task.id, sub.id


async def decide(sm: Sessionmaker, action: Callable[[AsyncSession], Awaitable[Any]]) -> str:
    """Действие в своей сессии, как хендлер: успех — commit, DomainError — rollback и текст отказа."""
    async with sm() as s:
        try:
            await action(s)
        except DomainError as exc:
            await s.rollback()
            return exc.message
        await s.commit()
        return "ok"


async def event_types(sm: Sessionmaker, task_id: int) -> list[EventType]:
    async with sm() as s:
        return [event.type for event in await svc.task_events(s, task_id)]


# --- Длинные и «неудобные» данные: никаких DataError ------------------------------------------------


async def test_long_and_dirty_user_fields_fit_columns(engine: AsyncEngine) -> None:
    sm = make_sessionmaker(engine)
    async with sm() as s:
        boss, _ = await users.register_or_get(s, 1001, "u" * 300, "Я" * 500 + "\x00")
        assert len(boss.username) == dbsafe.column_length(User.username)
        assert boss.full_name == "Я" * 200  # NUL убран, обрезано по колонке
        newbie, _ = await users.register_or_get(s, 2001, None, "x")
        with pytest.raises(DomainError):
            await users.complete_registration(s, newbie, "Ж" * 201, None)
        # 200 кириллических символов — ровно колонка (PostgreSQL считает символы, а не байты).
        await users.complete_registration(s, newbie, "Ж" * 200, "Юрист\x00 по договорам")
        await s.commit()
    async with sm() as s:
        stored = await users.get_by_tg(s, 2001)
        assert stored.full_name == "Ж" * 200 and stored.position == "Юрист по договорам"


async def test_long_and_dirty_task_fields(engine: AsyncEngine, clock) -> None:
    sm = make_sessionmaker(engine)
    team = await seed_team(sm)
    async with sm() as s:
        with pytest.raises(DomainError):
            await new_task(s, team, clock.now, title="Т" * 256)
        with pytest.raises(DomainError):
            await new_task(s, team, clock.now, plan_value=10, plan_unit="е" * 65)
        task = await new_task(
            s,
            team,
            clock.now,
            title="Т" * 254 + "\x00!",
            expected_result="Отчёт\x00 и \ud800 таблица",
            description="Описание\x00",
            plan_value=10,
            plan_unit="е" * 64,
        )
        await s.commit()
        task_id = task.id
    async with sm() as s:
        stored = await svc.get_task(s, task_id)
        assert stored.title == "Т" * 254 + "!"
        assert stored.expected_result == "Отчёт и � таблица"
        assert stored.description == "Описание" and stored.plan_unit == "е" * 64


async def test_attachment_fields_are_clipped_to_columns(engine: AsyncEngine, clock) -> None:
    sm = make_sessionmaker(engine)
    team = await seed_team(sm)
    async with sm() as s:
        task = await new_task(s, team, clock.now)
        employee = await s.get(User, team.employee_id)
        sub = await svc.submit_result(
            s,
            task.id,
            employee,
            fact_text="Сделано\x00",
            result_text="Результат \ud83d",
            attachments=[
                AttachmentIn(
                    AttachmentKind.DOCUMENT,
                    "BQACAgIAAxkBAAI" + "x" * 60,
                    file_unique_id="u" * 300,
                    file_name="Отчёт_" + "о" * 400 + ".xlsx",
                    mime_type="application/" + "m" * 300,
                    file_size=4 * 1024**3,  # Telegram Premium: файл до 4 ГБ — больше INTEGER
                ),
                AttachmentIn(AttachmentKind.PHOTO, "p" * 300),  # file_id не помещается — файл пропускается
            ],
        )
        await svc.record_evaluation(s, sub.id, score=100, rationale="Обоснование\x00", source="ai", model="m" * 100)
        await s.commit()
        sub_id = sub.id
    async with sm() as s:
        stored = await svc.get_submission(s, sub_id)
        assert stored.fact_text == "Сделано" and stored.result_text == "Результат �"
        assert stored.ai_rationale == "Обоснование" and stored.ai_model == "m" * 64
        assert len(stored.attachments) == 1
        att = stored.attachments[0]
        assert len(att.file_name) == 255 and att.file_name.endswith("….xlsx")
        assert len(att.file_unique_id) == 128 and len(att.mime_type) == 128
        assert att.file_size == dbsafe.INT32_MAX


async def test_out_of_range_ids_mean_not_found(engine: AsyncEngine, clock) -> None:
    """Подделанная кнопка с id больше INTEGER: «не найдено», а не ошибка базы (PostgreSQL)."""
    sm = make_sessionmaker(engine)
    team = await seed_team(sm)
    huge = 2**40
    async with sm() as s:
        boss = await s.get(User, team.boss_id)
        assert await svc.get_task(s, huge) is None
        assert await svc.get_submission(s, -huge) is None
        assert await users.get_user(s, huge) is None
        assert await users.get_by_tg(s, 2**70) is None
        with pytest.raises(DomainError, match="Пользователь не найден"):
            await users.approve_user(s, huge, boss)
        with pytest.raises(DomainError, match="Задача не найдена"):
            await svc.cancel_task(s, huge, boss)
        with pytest.raises(DomainError, match="Исполнителем"):
            await new_task(s, team, clock.now, assignee_id=huge)
        assert await svc.list_tasks(s, assignee_id=huge) == []
        assert await svc.count_tasks(s, assignee_id=huge) == 0


async def test_negative_paging_and_non_finite_event_data(engine: AsyncEngine, clock) -> None:
    sm = make_sessionmaker(engine)
    team = await seed_team(sm)
    async with sm() as s:
        task = await new_task(s, team, clock.now)
        await svc.add_event(s, task, None, EventType.REMINDER, kind="x", value=math.nan, big=math.inf)
        await s.commit()
        # SQLite считает отрицательные LIMIT/OFFSET «без ограничения»/0, PostgreSQL — ошибка.
        assert [t.id for t in await svc.list_tasks(s, limit=-1, offset=-10)] == [task.id]
        assert await svc.evaluated_history(s, team.employee_id, limit=-1, offset=-5) == []
    async with sm() as s:
        event = (await svc.task_events(s, task.id))[-1]
        assert event.data == {"kind": "x", "value": None, "big": None}


async def test_due_reminders_accepts_aware_now(engine: AsyncEngine, clock) -> None:
    sm = make_sessionmaker(engine)
    team = await seed_team(sm)
    async with sm() as s:
        await new_task(s, team, clock.now, deadline=clock.now + timedelta(hours=2))
        await s.commit()
        aware = clock.now.replace(tzinfo=UTC)
        due = await reminders.due_reminders(s, aware)
        assert [r.kind for r in due] == ["before_hours"]


async def test_event_json_round_trip_keeps_values_and_key_order(engine: AsyncEngine, clock) -> None:
    sm = make_sessionmaker(engine)
    team = await seed_team(sm)
    deadline = datetime(2026, 10, 9, 13, 0)
    async with sm() as s:
        task = await new_task(s, team, clock.now)
        await svc.add_event(
            s,
            task,
            None,
            EventType.EDITED,
            zeta="последний «ключ»",
            changes={"title": ["Старое", "Новое"], "deadline": [deadline, deadline + timedelta(days=1)]},
            alpha=1.5,
        )
        await s.commit()
    async with sm() as s:
        event = (await svc.task_events(s, task.id))[-1]
        assert list(event.data) == ["zeta", "changes", "alpha"]
        assert event.data["changes"]["deadline"] == ["2026-10-09T13:00:00", "2026-10-10T13:00:00"]
        assert event.data["zeta"] == "последний «ключ»" and event.data["alpha"] == 1.5


# --- Порядок списков (NULL в сортировке) одинаков на SQLite и PostgreSQL -------------------------------


async def seed_ordering(sm: Sessionmaker, now: datetime) -> int:
    """Задачи с NULL в полях сортировки (старые данные / сбой): DONE без completed_at, SUBMITTED без submitted_at."""
    async with sm() as s:
        boss = User(tg_id=1001, full_name="Петров", role=Role.MANAGER, status=UserStatus.ACTIVE)
        emp = User(tg_id=2001, full_name="Иванов", role=Role.EMPLOYEE, status=UserStatus.ACTIVE)
        s.add_all([boss, emp])
        await s.flush()

        def task(title: str, status: TaskStatus, deadline_days: float, **values: Any) -> Task:
            return Task(
                title=title,
                expected_result="Результат",
                deadline=now + timedelta(days=deadline_days),
                weight=10,
                status=status,
                assignee=emp,
                created_by=boss,
                manager=boss,
                rework_count=0,
                submissions=[],
                **values,
            )

        s.add_all(
            [
                task("done-null", TaskStatus.DONE, -5, final_score=90, completed_at=None),
                task("done-old", TaskStatus.DONE, -4, final_score=80, completed_at=now - timedelta(days=3)),
                task("done-new", TaskStatus.DONE, -3, final_score=70, completed_at=now - timedelta(days=1)),
                task("active-late", TaskStatus.ACTIVE, -1),
                task("active-soon", TaskStatus.ACTIVE, 2),
                task("review-null", TaskStatus.SUBMITTED, 1, submitted_at=None),
                task("review-old", TaskStatus.SUBMITTED, 1, submitted_at=now - timedelta(days=2)),
                task("review-new", TaskStatus.SUBMITTED, 1, submitted_at=now - timedelta(hours=1)),
            ]
        )
        await s.commit()
        return emp.id


async def list_orders(sm: Sessionmaker, employee_id: int) -> dict[str, list[str]]:
    async with sm() as s:
        return {
            "list_tasks": [t.title for t in await svc.list_tasks(s)],
            "page_2": [t.title for t in await svc.list_tasks(s, limit=3, offset=3)],
            "history": [t.title for t in await svc.evaluated_history(s, employee_id)],
            "review": [t.title for t in await svc.list_for_review(s)],
        }


async def test_list_ordering_with_nulls_matches_sqlite(memory_engine: AsyncEngine, pg_engine: AsyncEngine, clock) -> None:
    sqlite_sm, pg_sm = make_sessionmaker(memory_engine), make_sessionmaker(pg_engine)
    sqlite_orders = await list_orders(sqlite_sm, await seed_ordering(sqlite_sm, clock.now))
    pg_orders = await list_orders(pg_sm, await seed_ordering(pg_sm, clock.now))
    assert pg_orders == sqlite_orders
    # Сам порядок: открытые по сроку, затем DONE по completed_at (новые сверху, без даты — в конце).
    assert pg_orders["history"] == ["done-new", "done-old", "done-null"]
    assert pg_orders["review"] == ["review-null", "review-old", "review-new"]
    assert pg_orders["list_tasks"][-3:] == ["done-new", "done-old", "done-null"]


# --- Гонки: две настоящие сессии PostgreSQL --------------------------------------------------------


async def test_concurrent_review_second_waits_for_lock_and_is_refused(pg_engine: AsyncEngine, clock) -> None:
    sm = make_sessionmaker(pg_engine)
    team = await seed_team(sm)
    task_id, sub_id = await submitted_task(sm, team, clock.now)
    async with sm() as first, sm() as second:
        boss = await first.get(User, team.boss_id)
        deputy = await second.get(User, team.deputy_id)
        await svc.get_submission(first, sub_id)   # оба открыли экран проверки
        await svc.get_submission(second, sub_id)
        await svc.review_confirm(first, sub_id, boss)  # строка задачи заблокирована до commit
        late = asyncio.create_task(svc.review_set_score(second, sub_id, deputy, 80, "Мало"))
        await asyncio.sleep(LOCK_WAIT_SEC)
        assert not late.done(), "второй UPDATE должен ждать commit первой сессии"
        await first.commit()
        with pytest.raises(DomainError, match=REVIEW_ALREADY_PROCESSED):
            await late
        await second.rollback()
    async with sm() as s:
        task = await svc.get_task(s, task_id)
        assert task.status == TaskStatus.DONE and task.final_score == 100
        assert task.last_submission.decision == ReviewDecision.APPROVED
    types = await event_types(sm, task_id)
    assert types.count(EventType.SCORE_CONFIRMED) == 1 and EventType.SCORE_CHANGED not in types


async def test_concurrent_review_first_rolls_back_second_wins(pg_engine: AsyncEngine, clock) -> None:
    sm = make_sessionmaker(pg_engine)
    team = await seed_team(sm)
    task_id, sub_id = await submitted_task(sm, team, clock.now)
    async with sm() as first, sm() as second:
        boss = await first.get(User, team.boss_id)
        deputy = await second.get(User, team.deputy_id)
        await svc.review_confirm(first, sub_id, boss)
        late = asyncio.create_task(svc.review_set_score(second, sub_id, deputy, 80))
        await asyncio.sleep(LOCK_WAIT_SEC)
        assert not late.done()
        await first.rollback()  # хендлер первого упал — второй проходит по свежей строке
        await late
        await second.commit()
    async with sm() as s:
        task = await svc.get_task(s, task_id)
        assert task.status == TaskStatus.DONE and task.final_score == 80
        assert task.last_submission.decision == ReviewDecision.CHANGED


async def test_simultaneous_proposal_decisions_exactly_one_wins(pg_engine: AsyncEngine, clock) -> None:
    sm = make_sessionmaker(pg_engine)
    team = await seed_team(sm)
    task_ids = []
    async with sm() as s:
        employee = await s.get(User, team.employee_id)
        for number in range(5):
            task = await svc.propose_task(
                s, employee=employee, title=f"Поручение {number}", expected_result="Сделать",
                deadline=clock.now + timedelta(days=2),
            )
            task_ids.append(task.id)
        await s.commit()

    async def approve(s: AsyncSession, task_id: int) -> None:
        await svc.approve_proposal(s, task_id, await s.get(User, team.boss_id), weight=30)

    async def reject(s: AsyncSession, task_id: int) -> None:
        await svc.reject_proposal(s, task_id, await s.get(User, team.deputy_id), "Не нужно")

    for task_id in task_ids:
        results = await asyncio.gather(
            decide(sm, lambda s, t=task_id: approve(s, t)),
            decide(sm, lambda s, t=task_id: reject(s, t)),
        )
        assert sorted(results) == sorted(["ok", PROPOSAL_ALREADY_PROCESSED])
        types = await event_types(sm, task_id)
        assert (EventType.APPROVED in types) != (EventType.REJECTED in types)
        async with sm() as s:
            task = await svc.get_task(s, task_id)
            assert task.status == (TaskStatus.ACTIVE if EventType.APPROVED in types else TaskStatus.REJECTED)


async def test_simultaneous_registration_decisions_exactly_one_wins(pg_engine: AsyncEngine) -> None:
    sm = make_sessionmaker(pg_engine)
    team = await seed_team(sm)
    async with sm() as s:
        pending = User(tg_id=3001, full_name="Новиков Пётр", role=Role.EMPLOYEE, status=UserStatus.PENDING)
        s.add(pending)
        await s.commit()
        pending_id = pending.id

    async def approve(s: AsyncSession) -> None:
        await users.approve_user(s, pending_id, await s.get(User, team.boss_id))

    async def reject(s: AsyncSession) -> None:
        await users.reject_user(s, pending_id, await s.get(User, team.deputy_id))

    results = await asyncio.gather(decide(sm, approve), decide(sm, reject))
    assert sorted(results) == sorted(["ok", "Заявка уже обработана"])
    async with sm() as s:
        status = (await users.get_user(s, pending_id)).status
    assert status == (UserStatus.ACTIVE if results[0] == "ok" else UserStatus.BLOCKED)


CHANGED_MEANWHILE = "Задача только что изменилась — откройте её заново и повторите действие"


@pytest.mark.parametrize("first_action", ["submit", "cancel"])
async def test_submit_and_cancel_interleaved(pg_engine: AsyncEngine, clock, first_action: str) -> None:
    """Сотрудник сдаёт, руководитель отменяет: оба прочитали задачу «в работе», первый занял строку.

    Второй ждёт commit первого и получает отказ — отмена не «съедает» только что сданный результат,
    а сдача не «оживляет» отменённую задачу.
    """
    sm = make_sessionmaker(pg_engine)
    team = await seed_team(sm)
    async with sm() as s:
        task_id = (await new_task(s, team, clock.now)).id
        await s.commit()

    async def submit(s: AsyncSession) -> None:
        await svc.submit_result(s, task_id, await s.get(User, team.employee_id), fact_text="Готово")

    async def cancel(s: AsyncSession) -> None:
        await svc.cancel_task(s, task_id, await s.get(User, team.boss_id), "Не актуально")

    actions = {"submit": submit, "cancel": cancel}
    second_action = "cancel" if first_action == "submit" else "submit"
    async with sm() as first, sm() as second:
        await svc.get_task(second, task_id)  # второй уже открыл задачу «в работе»
        await actions[first_action](first)
        late = asyncio.create_task(actions[second_action](second))
        await asyncio.sleep(LOCK_WAIT_SEC)
        assert not late.done()
        await first.commit()
        with pytest.raises(DomainError) as refused:
            await late
        await second.rollback()
    if first_action == "submit":
        assert refused.value.message == CHANGED_MEANWHILE
    else:
        assert refused.value.message == "Задача не в работе — сдать результат нельзя"
    async with sm() as s:
        task = await svc.get_task(s, task_id)
        submissions = await s.scalar(select(func.count()).select_from(Submission).where(Submission.task_id == task_id))
    expected = (TaskStatus.SUBMITTED, 1) if first_action == "submit" else (TaskStatus.CANCELLED, 0)
    assert (task.status, submissions) == expected


# --- Отметка «напоминание отправлено» ------------------------------------------------------------------


async def reminder_rows(sm: Sessionmaker, task_id: int) -> list[str]:
    async with sm() as s:
        return list(await s.scalars(select(ReminderLog.kind).where(ReminderLog.task_id == task_id)))


async def test_mark_sent_is_idempotent_and_keeps_transaction_usable(engine: AsyncEngine, clock) -> None:
    sm = make_sessionmaker(engine)
    team = await seed_team(sm)
    async with sm() as s:
        task = await new_task(s, team, clock.now)
        await s.commit()
        await reminders.mark_sent(s, task.id, "before_1d")
        await reminders.mark_sent(s, task.id, "before_1d")  # конфликт — не ошибка
        # Транзакция жива (в PostgreSQL ошибка без SAVEPOINT оборвала бы её): пишем дальше.
        await reminders.mark_sent(s, task.id, "before_3d")
        await svc.add_event(s, task, None, EventType.REMINDER, kind="before_1d")
        await s.commit()
    assert sorted(await reminder_rows(sm, task.id)) == ["before_1d", "before_3d"]
    assert (await event_types(sm, task.id)).count(EventType.REMINDER) == 1


async def test_mark_sent_from_two_sessions_at_once(pg_engine: AsyncEngine, clock) -> None:
    """Два экземпляра бота (обновление на Render) пишут одну отметку: вторая ждёт первую, без ошибок."""
    sm = make_sessionmaker(pg_engine)
    team = await seed_team(sm)
    async with sm() as s:
        task_id = (await new_task(s, team, clock.now)).id
        await s.commit()
    async with sm() as first, sm() as second:
        await reminders.mark_sent(first, task_id, "deadline_passed")
        late = asyncio.create_task(reminders.mark_sent(second, task_id, "deadline_passed"))
        await asyncio.sleep(LOCK_WAIT_SEC)
        assert not late.done(), "вставка того же ключа ждёт commit первой сессии"
        await first.commit()
        await late
        await reminders.mark_sent(second, task_id, "overdue_manager")
        await second.commit()
    assert sorted(await reminder_rows(sm, task_id)) == ["deadline_passed", "overdue_manager"]

    results = await asyncio.gather(
        *(decide(sm, lambda s: reminders.mark_sent(s, task_id, "overdue_2026-10-02")) for _ in range(4))
    )
    assert results == ["ok"] * 4
    assert (await reminder_rows(sm, task_id)).count("overdue_2026-10-02") == 1


# --- make_engine / init_db на настоящем PostgreSQL ------------------------------------------------------


def _test_url() -> str:
    return os.environ["TEST_DATABASE_URL"].strip()


async def test_libpq_style_url_connects(pg_engine: AsyncEngine) -> None:
    """Адрес, скопированный из панели хостинга (схема postgresql://, параметры libpq), работает с asyncpg."""
    url = make_url(_test_url()).set(drivername="postgresql").update_query_dict(
        {
            "sslmode": "disable",
            "connect_timeout": "5",
            "application_name": "kpi-compat",
            "options": "-c statement_timeout=12345",
            "gssencmode": "disable",
            "channel_binding": "prefer",
        }
    )
    engine = make_engine(url.render_as_string(hide_password=False).replace("postgresql://", "postgres://", 1))
    try:
        assert isinstance(engine.pool, AsyncAdaptedQueuePool) and engine.pool.size() == 3
        async with engine.connect() as conn:
            assert await conn.scalar(text("SHOW application_name")) == "kpi-compat"
            assert await conn.scalar(text("SHOW statement_timeout")) == "12345ms"
    finally:
        await engine.dispose()


async def test_transaction_pooler_settings_run_orm_queries(pg_engine: AsyncEngine, clock) -> None:
    """pgbouncer=true (как порт 6543 Supabase): без кэша выражений, с уникальными именами — запросы идут."""
    engine = make_engine(
        make_url(_test_url()).update_query_dict({"pgbouncer": "true"}).render_as_string(hide_password=False),
        connect_args={"server_settings": {"search_path": await _search_path(pg_engine)}},
    )
    try:
        sm = make_sessionmaker(engine)
        team = await seed_team(sm)
        for _ in range(3):  # одни и те же запросы повторно — без конфликтов имён выражений
            async with sm() as s:
                task = await new_task(s, team, clock.now)
                await reminders.mark_sent(s, task.id, "before_1d")
                await s.commit()
                assert await svc.count_tasks(s, assignee_id=team.employee_id) >= 1
    finally:
        await engine.dispose()


async def _search_path(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return str(await conn.scalar(text("SHOW search_path")))


async def test_concurrent_init_db_creates_tables_once_with_rls(pg_engine: AsyncEngine) -> None:
    """Два-три экземпляра бота стартуют одновременно на пустой базе: таблицы создаются без ошибок."""
    schema = f"kpi_init_{uuid.uuid4().hex[:10]}"
    async with pg_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engines = [
        make_engine(_test_url(), connect_args={"server_settings": {"search_path": schema}}) for _ in range(3)
    ]
    try:
        await asyncio.gather(*(init_db(engine) for engine in engines))
        await init_db(engines[0])  # повторный запуск: существующие таблицы не трогаются
        async with engines[0].connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT c.relname, c.relrowsecurity FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :schema AND c.relkind = 'r'"
                    ),
                    {"schema": schema},
                )
            ).all()
        assert {name for name, _ in rows} == set(Base.metadata.tables)
        assert all(rls for _, rls in rows), "RLS: таблицы закрыты от REST API Supabase"
    finally:
        for engine in engines:
            await engine.dispose()
        async with pg_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


# --- Разбор DATABASE_URL (без базы) --------------------------------------------------------------------

SUPABASE_HOST = "aws-0-eu-central-1.pooler.supabase.com"


@pytest.mark.parametrize(
    "raw",
    [
        f"postgres://postgres.ref:secret@{SUPABASE_HOST}:5432/postgres",
        f"postgresql://postgres.ref:secret@{SUPABASE_HOST}:5432/postgres",
        f"postgresql+asyncpg://postgres.ref:secret@{SUPABASE_HOST}:5432/postgres",
        f"postgresql+psycopg2://postgres.ref:secret@{SUPABASE_HOST}:5432/postgres",
        f'  "postgresql://postgres.ref:secret@{SUPABASE_HOST}:5432/postgres"  ',
    ],
)
def test_postgres_schemes_are_normalized_to_asyncpg(raw: str) -> None:
    url = normalize_url(raw)
    assert url.drivername == "postgresql+asyncpg" and url.host == SUPABASE_HOST and url.password == "secret"
    assert is_postgres_url(raw)
    assert "secret" not in describe_url(raw)


def test_supabase_session_pooler_defaults() -> None:
    url, args = postgres_connect_args(f"postgresql://postgres.ref:pw@{SUPABASE_HOST}:5432/postgres")
    assert url.query == {} and args == {"ssl": "require", "timeout": 15.0}
    engine = make_engine(f"postgresql://postgres.ref:pw@{SUPABASE_HOST}:5432/postgres")
    pool = engine.pool
    assert isinstance(pool, AsyncAdaptedQueuePool)
    assert (pool.size(), pool._max_overflow, pool._recycle, pool._pre_ping) == (3, 1, 300, True)


def test_dialog_storage_gets_own_single_connection_pool() -> None:
    """PostgreSQL: хранилищу диалогов — свой пул из одного соединения (основной 3 + 1): ≤ 5 соединений
    на экземпляр, 10 на два экземпляра при деплое. SQLite — None (хранилищу хватает основного движка)."""
    raw = f"postgresql://postgres.ref:[YOUR-PASSWORD]@{SUPABASE_HOST}:5432/postgres"
    engine = make_storage_engine(raw, password="pw")
    assert engine is not None
    pool = engine.pool
    assert isinstance(pool, AsyncAdaptedQueuePool)
    assert (pool.size(), pool._max_overflow, pool._recycle, pool._pre_ping) == (1, 0, 300, True)
    assert engine.url.password == "pw" and engine.url.drivername == "postgresql+asyncpg"
    main_pool = make_engine(raw, password="pw").pool
    assert main_pool.size() + main_pool._max_overflow + pool.size() + pool._max_overflow == 5  # type: ignore[attr-defined]
    assert make_storage_engine("sqlite+aiosqlite:///:memory:") is None
    assert make_storage_engine("sqlite+aiosqlite:///data/bot.db") is None


def test_transaction_pooler_port_disables_statement_caches() -> None:
    _, args = postgres_connect_args(f"postgresql://postgres.ref:pw@{SUPABASE_HOST}:6543/postgres")
    assert args["statement_cache_size"] == 0 and args["prepared_statement_cache_size"] == 0
    name_func = args["prepared_statement_name_func"]
    assert name_func() != name_func()


def test_libpq_params_are_translated_or_dropped() -> None:
    url, args = postgres_connect_args(
        f"postgresql://u:p@{SUPABASE_HOST}:5432/db?sslmode=verify-full&connect_timeout=7"
        "&application_name=kpi&options=-c%20search_path%3Dkpi%20-c%20lock_timeout%3D5s"
        "&keepalives=1&gssencmode=disable&statement_cache_size=50&command_timeout=30&target_session_attrs=read-write"
    )
    assert url.query == {}
    assert args["ssl"] == "verify-full" and args["timeout"] == 7.0
    assert args["server_settings"] == {"application_name": "kpi", "search_path": "kpi", "lock_timeout": "5s"}
    assert args["statement_cache_size"] == 50 and args["command_timeout"] == 30.0
    assert args["target_session_attrs"] == "read-write"
    assert "keepalives" not in args and "gssencmode" not in args


def test_local_hosts_keep_asyncpg_default_ssl() -> None:
    for host in ("localhost", "127.0.0.1", "[::1]", "db", "10.1.2.3", "192.168.0.10"):
        _, args = postgres_connect_args(f"postgresql://u:p@{host}:5432/db")
        assert "ssl" not in args, host
    _, args = postgres_connect_args("postgresql://u:p@db.abcdef.supabase.co:5432/postgres")
    assert args["ssl"] == "require"
    _, args = postgres_connect_args(f"postgresql://u:p@{SUPABASE_HOST}:5432/db?sslmode=disable")
    assert args["ssl"] == "disable"


def _self_signed_ca(path) -> None:
    """Самоподписанный сертификат CA в файл (только для разбора sslrootcert; подключений нет)."""
    crypto = pytest.importorskip("cryptography")
    del crypto
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kpi-bot test CA")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_sslrootcert_builds_verifying_context(tmp_path) -> None:
    ca = tmp_path / "ca.pem"
    _self_signed_ca(ca)
    _, args = postgres_connect_args(f"postgresql://u:p@{SUPABASE_HOST}:5432/db?sslmode=verify-full&sslrootcert={ca}")
    context = args["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    _, args = postgres_connect_args(f"postgresql://u:p@{SUPABASE_HOST}:5432/db?sslmode=verify-ca&sslrootcert={ca}")
    assert not args["ssl"].check_hostname and args["ssl"].verify_mode == ssl.CERT_REQUIRED


def test_password_with_unescaped_at_sign() -> None:
    url = normalize_url(f"postgresql://postgres.ref:p@ss@w0rd@{SUPABASE_HOST}:5432/postgres")
    assert url.host == SUPABASE_HOST and url.password == "p@ss@w0rd" and url.username == "postgres.ref"


def test_bad_sslmode_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match="sslmode"):
        postgres_connect_args(f"postgresql://u:p@{SUPABASE_HOST}:5432/db?sslmode=required")


def test_sqlite_urls_keep_old_behaviour(tmp_path) -> None:
    memory = make_engine("sqlite+aiosqlite:///:memory:")
    assert isinstance(memory.pool, StaticPool)
    path = tmp_path / "nested" / "dir" / "bot.db"
    file_engine = make_engine(f"sqlite:///{path.as_posix()}")  # синхронная схема -> aiosqlite
    assert file_engine.url.drivername == "sqlite+aiosqlite" and path.parent.is_dir()


def test_engine_kwargs_override_pool() -> None:
    engine = make_engine(f"postgresql://u:p@{SUPABASE_HOST}:5432/db", poolclass=StaticPool)
    assert isinstance(engine.pool, StaticPool)


# --- Пароль отдельно от адреса (DATABASE_PASSWORD) -------------------------------------------------------

# Все символы, которые в адресе пришлось бы экранировать (и пробел, и кириллица).
SECRET = "p@ss:w/rd#?%&=+ [x]'\"Пароль"
SUPABASE_URI = f"postgresql://postgres.ref:[YOUR-PASSWORD]@{SUPABASE_HOST}:5432/postgres"


def test_password_is_injected_url_escaped() -> None:
    url = apply_password(f"postgresql://postgres.ref@{SUPABASE_HOST}:5432/postgres", SECRET)
    assert (url.drivername, url.username, url.host, url.port, url.database) == (
        "postgresql+asyncpg", "postgres.ref", SUPABASE_HOST, 5432, "postgres"
    )
    assert url.password == SECRET  # asyncpg получает пароль как есть
    rendered = url.render_as_string(hide_password=False)
    assert SECRET not in rendered and make_url(rendered).password == SECRET  # в строке — экранирован
    for shown in (describe_url(url), str(url), repr(url)):
        assert SECRET not in shown and "Пароль" not in shown


@pytest.mark.parametrize(
    "placeholder", ["[YOUR-PASSWORD]", "YOUR-PASSWORD", "[your_password]", "<YOUR-PASSWORD>", "%5BYOUR-PASSWORD%5D", ""]
)
def test_supabase_placeholder_is_replaced(placeholder: str) -> None:
    raw = f"postgresql://postgres.ref:{placeholder}@{SUPABASE_HOST}:5432/postgres?sslmode=require"
    url = apply_password(raw, SECRET)
    assert url.password == SECRET and url.host == SUPABASE_HOST and url.query == {"sslmode": "require"}
    assert is_password_placeholder(make_url(raw).password)


def test_password_in_url_wins_and_other_urls_are_untouched() -> None:
    assert apply_password(f"postgresql://u:real-pass@{SUPABASE_HOST}:5432/db", SECRET).password == "real-pass"
    assert apply_password(f"postgresql://u:p@ss@w0rd@{SUPABASE_HOST}:5432/db", SECRET).password == "p@ss@w0rd"
    assert apply_password("sqlite+aiosqlite:///:memory:", SECRET).password is None
    assert apply_password(f"postgresql://u@{SUPABASE_HOST}/db", "").password is None
    assert apply_password(f"postgresql://u@{SUPABASE_HOST}/db", None).password is None
    assert apply_password(f"postgresql://u@{SUPABASE_HOST}/db", "pw\r\n").password == "pw"  # вставка из Блокнота
    for real in ("password", "my-password", "[secret]", "your-password-2026"):
        assert not is_password_placeholder(real)


async def test_make_engine_takes_password_from_settings(
    set_env: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    set_env(DATABASE_PASSWORD=SECRET)
    with caplog.at_level(logging.DEBUG):
        engine = make_engine(SUPABASE_URI)
        explicit = make_engine(f"postgresql://postgres.ref@{SUPABASE_HOST}:5432/postgres", password="other")
        sqlite = make_engine("sqlite+aiosqlite:///:memory:")
    try:
        assert engine.url.password == SECRET and explicit.url.password == "other"
        assert engine.url.drivername == "postgresql+asyncpg" and sqlite.url.password is None
        settings = get_settings()
        assert settings.database_password == SECRET
        assert SECRET not in repr(settings) and SECRET not in str(settings)
        assert SECRET not in caplog.text and SECRET not in repr(engine) and SECRET not in str(engine.url)
    finally:
        for item in (engine, explicit, sqlite):
            await item.dispose()


async def test_placeholder_without_password_is_a_clear_warning(
    set_env: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    set_env(DATABASE_PASSWORD="")
    with caplog.at_level(logging.WARNING, logger="bot.db.base"):
        engine = make_engine(SUPABASE_URI)
    await engine.dispose()
    assert "[YOUR-PASSWORD]" in caplog.text and "DATABASE_PASSWORD" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="bot.db.base"):
        engine = make_engine(f"postgresql://postgres.ref@{SUPABASE_HOST}:5432/postgres")  # пароль, например, в .pgpass
    await engine.dispose()
    assert caplog.text == ""


def _uri_without_password(placeholder: bool) -> tuple[str, str]:
    """TEST_DATABASE_URL так, как его показывает Supabase: без пароля / с «[YOUR-PASSWORD]»; и сам пароль."""
    real = normalize_url(_test_url())
    if not real.password:
        pytest.skip("в TEST_DATABASE_URL нет пароля — подстановку DATABASE_PASSWORD нечем проверить")
    userinfo = quote(real.username or "", safe="") + (":[YOUR-PASSWORD]" if placeholder else "")
    host = f"[{real.host}]" if real.host and ":" in real.host else real.host
    port = f":{real.port}" if real.port else ""
    return f"postgresql://{userinfo}@{host}{port}/{real.database}", real.password


@pytest.mark.parametrize("placeholder", [False, True], ids=["no-password", "your-password"])
async def test_database_password_connects_to_real_postgres(
    pg_engine: AsyncEngine, set_env: Callable[..., None], caplog: pytest.LogCaptureFixture, placeholder: bool
) -> None:
    """Настоящее подключение: строка без пароля / с заглушкой Supabase + DATABASE_PASSWORD."""
    raw, password = _uri_without_password(placeholder)
    search_path = {"server_settings": {"search_path": await _search_path(pg_engine)}}
    set_env(DATABASE_PASSWORD=password)
    engine = make_engine(raw, connect_args=search_path)
    try:
        sm = make_sessionmaker(engine)
        team = await seed_team(sm)  # ORM-запросы через это подключение
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT current_user")) == normalize_url(_test_url()).username
        async with sm() as s:
            assert (await users.get_user(s, team.employee_id)).full_name == "Иванов Иван Иванович"
    finally:
        await engine.dispose()

    # Неверный пароль: сервер с проверкой пароля отказывает — и в тексте ошибки пароля нет.
    wrong = "wrong-" + SECRET
    set_env(DATABASE_PASSWORD=wrong)
    engine = make_engine(raw, connect_args=search_path)
    try:
        async with engine.connect() as conn:
            await conn.scalar(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — нужна только проверка текста ошибки
        assert wrong not in f"{exc!r} {exc}" and "Пароль" not in str(exc)
    finally:
        await engine.dispose()
    assert wrong not in caplog.text
    if password not in describe_url(_test_url()):  # короткий пароль вроде «postgres» совпал бы с именем
        assert password not in caplog.text
