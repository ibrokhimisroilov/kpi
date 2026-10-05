"""Задания по времени в режиме webhook: bot.scheduler.jobs.run_due_jobs.

Фоновый цикл бота (bot.web.BackgroundLoop) раз в 5 минут — и, по желанию, внешний будильник через
/tick — вызывает run_due_jobs, и она выполняет всё, чему пора: напоминания, еженедельную сводку,
резервную копию. Её можно вызывать сколько угодно раз, а при обновлении на хостинге — из двух
экземпляров бота одновременно (у каждого свой фоновый цикл): каждое действие выполняется один раз
(ReminderLog / JobLog «занимаются» до отправки).

Два экземпляра бота — два движка SQLAlchemy на одном файле SQLite (у каждого свои соединения,
транзакции настоящие параллельные) и два фейковых Telegram API (tests/e2e/fakebot.py).
Календарь (Asia/Tashkent, UTC+5): пятница 02.10.2026; сводка — по понедельникам в 9:00.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram import methods as m
from aiogram.client.default import DefaultBotProperties
from e2e.fakebot import FakeSession
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.db.base import init_db, make_engine, make_sessionmaker
from bot.ai.evaluate import RULES_PREFIX
from bot.db.models import DigestLog, JobLog, ReminderLog, Role, Submission, TaskEvent, User, UserStatus
from bot.scheduler import backup, jobs
from bot.scheduler.jobs import run_due_jobs
from bot.services import tasks as tasks_svc
from bot.utils.dates import to_utc

MGR, MGR2, EMP = 1001, 1002, 2001


def local(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Местное время (Ташкент) 2026 года -> naive UTC, как его передаёт будильник."""
    return to_utc(datetime(2026, month, day, hour, minute))


@dataclass
class Instance:
    """Один экземпляр бота: свой движок БД (общий файл) и свой Telegram API."""

    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    bot: Bot

    @property
    def api(self) -> FakeSession:
        assert isinstance(self.bot.session, FakeSession)
        return self.bot.session

    def sent(self, method_type: type) -> list[Any]:
        return [request for request in self.api.requests if isinstance(request, method_type)]

    def clear(self) -> None:
        self.api.requests.clear()

    async def tick(self, now: datetime) -> dict[str, object]:
        return await run_due_jobs(self.bot, self.sessionmaker, now=now)


@pytest.fixture(autouse=True)
def _schedule(set_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch) -> None:
    """Расписание по умолчанию — явно (не из .env разработчика)."""
    set_env(
        BACKUP_ENABLED="true",
        BACKUP_HOUR="23",
        DIGEST_WEEKDAY="0",
        DIGEST_HOUR="9",
        QUIET_HOURS_START="21",
        QUIET_HOURS_END="8",
    )
    monkeypatch.setattr(backup, "_too_big_warned", set())


@pytest_asyncio.fixture
async def pair(tmp_path: Path) -> AsyncIterator[tuple[Instance, Instance]]:
    """Старый и новый экземпляры бота на одной файловой базе (как при обновлении на Render)."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'data' / 'bot.db').as_posix()}"
    instances: list[Instance] = []
    try:
        for _ in range(2):
            engine = make_engine(url)
            await init_db(engine)
            bot = Bot("42:TEST", session=FakeSession(), default=DefaultBotProperties(parse_mode="HTML"))
            instances.append(Instance(engine, make_sessionmaker(engine), bot))
        yield instances[0], instances[1]
    finally:
        for instance in instances:
            await instance.bot.session.close()
            await instance.engine.dispose()


async def seed(instance: Instance, *, employees: bool = True) -> None:
    """Два активных руководителя и (по умолчанию) один активный сотрудник."""
    async with instance.sessionmaker() as session:
        session.add_all([
            User(tg_id=MGR, full_name="Петрова Анна Сергеевна", role=Role.MANAGER, status=UserStatus.ACTIVE),
            User(tg_id=MGR2, full_name="Смирнов Олег Петрович", role=Role.MANAGER, status=UserStatus.ACTIVE),
        ])
        if employees:
            session.add(User(tg_id=EMP, full_name="Иванов Иван Иванович", position="Юрист",
                             role=Role.EMPLOYEE, status=UserStatus.ACTIVE))
        await session.commit()


async def add_task(instance: Instance, deadline: datetime) -> int:
    async with instance.sessionmaker() as session:
        creator = await session.scalar(select(User).where(User.tg_id == MGR))
        assignee = await session.scalar(select(User).where(User.tg_id == EMP))
        task = await tasks_svc.create_task(
            session,
            creator=creator,
            assignee_id=assignee.id,
            title="Анализ договоров",
            expected_result="Проверить 100 договоров и представить отчёт",
            deadline=deadline,
            weight=20,
            plan_value=100,
            plan_unit="договоров",
        )
        await session.commit()
        return task.id


async def rows(instance: Instance, model: type) -> list[Any]:
    async with instance.sessionmaker() as session:
        return list(await session.scalars(select(model)))


def chats(requests: list[Any]) -> list[int]:
    return sorted(request.chat_id for request in requests)


# --- Резервная копия --------------------------------------------------------------------------------


async def test_backup_sent_once_when_two_instances_tick_at_the_same_time(
    pair: tuple[Instance, Instance], clock: Any
) -> None:
    """Пятница 23:05: будильник одновременно попал в старый и новый экземпляры бота. Копию базы
    каждый руководитель получает один раз; повторный вызов в тот же вечер ничего не шлёт."""
    old, new = pair
    await seed(old)
    clock.set(local(10, 2, 23, 5))
    first, second = await asyncio.gather(old.tick(local(10, 2, 23, 5)), new.tick(local(10, 2, 23, 5)))

    assert sorted([first["backup"], second["backup"]]) == ["done", "sent:2"]
    documents = old.sent(m.SendDocument) + new.sent(m.SendDocument)
    assert chats(documents) == [MGR, MGR2]
    assert [(row.job, row.key) for row in await rows(old, JobLog)] == [("backup", "2026-10-02")]

    old.clear()
    new.clear()
    assert (await new.tick(local(10, 2, 23, 40)))["backup"] == "done"
    assert (await old.tick(local(10, 2, 23, 55)))["backup"] == "done"
    assert not old.sent(m.SendDocument) and not new.sent(m.SendDocument)


async def test_backup_claim_is_atomic_even_if_both_instances_passed_the_check(
    pair: tuple[Instance, Instance], monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    """Оба экземпляра одновременно увидели «копии за сегодня ещё нет» — вставку JobLog всё равно
    проходит только один (уникальный ключ job+key), второй копию не отправляет."""
    old, new = pair
    await seed(old)

    async def nothing_logged(*args: Any) -> bool:
        return False

    monkeypatch.setattr(jobs, "_job_logged", nothing_logged)
    clock.set(local(10, 2, 23, 5))
    results = await asyncio.gather(old.tick(local(10, 2, 23, 5)), new.tick(local(10, 2, 23, 5)))
    assert sorted(result["backup"] for result in results) == ["done", "sent:2"]
    assert chats(old.sent(m.SendDocument) + new.sent(m.SendDocument)) == [MGR, MGR2]
    assert len(await rows(old, JobLog)) == 1


async def test_backup_next_day_and_not_before_backup_hour(pair: tuple[Instance, Instance], clock: Any) -> None:
    """До BACKUP_HOUR копия не делается; на следующий день после BACKUP_HOUR — новая (ключ — местная дата)."""
    bot, _ = pair
    await seed(bot)
    clock.set(local(10, 2, 22, 59))
    assert (await bot.tick(local(10, 2, 22, 59)))["backup"] == "not_due"
    assert (await bot.tick(local(10, 2, 23, 0)))["backup"] == "sent:2"
    # 00:10 следующего дня по Ташкенту — новые сутки, но BACKUP_HOUR ещё не наступил.
    assert (await bot.tick(local(10, 3, 0, 10)))["backup"] == "not_due"
    assert (await bot.tick(local(10, 3, 23, 1)))["backup"] == "sent:2"
    names = [sent.file_name for sent in bot.api.sent_files if sent.kind == "document"]
    assert names == ["kpi_backup_2026-10-02.db"] * 2 + ["kpi_backup_2026-10-03.db"] * 2
    assert sorted(row.key for row in await rows(bot, JobLog)) == ["2026-10-02", "2026-10-03"]


async def test_backup_hour_in_the_morning_is_caught_up_during_the_day(
    pair: tuple[Instance, Instance], set_env: Callable[..., None], clock: Any
) -> None:
    """BACKUP_HOUR=3, а бот «проснулся» только в 10:00 (будильник не работал ночью) — копия за этот
    день всё равно уходит, один раз."""
    bot, _ = pair
    await seed(bot)
    set_env(BACKUP_HOUR="3")
    clock.set(local(10, 2, 10))
    assert (await bot.tick(local(10, 2, 10)))["backup"] == "sent:2"
    assert (await bot.tick(local(10, 2, 10, 5)))["backup"] == "done"


async def test_backup_disabled_and_invalid_hour(
    pair: tuple[Instance, Instance], set_env: Callable[..., None], clock: Any
) -> None:
    """BACKUP_ENABLED=false — копий нет; неверный BACKUP_HOUR — как в polling, 23:00."""
    bot, _ = pair
    await seed(bot)
    clock.set(local(10, 2, 23, 30))
    set_env(BACKUP_ENABLED="false")
    assert (await bot.tick(local(10, 2, 23, 30)))["backup"] == "disabled"
    assert await rows(bot, JobLog) == []

    set_env(BACKUP_ENABLED="true", BACKUP_HOUR="30")
    assert (await bot.tick(local(10, 2, 22, 30)))["backup"] == "not_due"
    assert (await bot.tick(local(10, 2, 23, 30)))["backup"] == "sent:2"


# --- Еженедельная сводка ----------------------------------------------------------------------------


async def test_digest_sent_on_first_tick_after_schedule_once(pair: tuple[Instance, Instance], clock: Any) -> None:
    """Понедельник 12.10: в 8:55 сводки ещё нет; первый «тик» после 9:00 отправляет её обоим
    руководителям за неделю 05.10–11.10; следующие «тики» в этот день — ничего."""
    bot, _ = pair
    await seed(bot)
    clock.set(local(10, 12, 8, 55))
    assert (await bot.tick(local(10, 12, 8, 55)))["digest"] == "not_due"
    assert not bot.sent(m.SendMessage)

    clock.set(local(10, 12, 9, 5))
    assert (await bot.tick(local(10, 12, 9, 5)))["digest"] == "sent"
    digests = bot.sent(m.SendMessage)
    assert chats(digests) == [MGR, MGR2]
    assert all("Еженедельная сводка" in request.text and "05.10–11.10" in request.text for request in digests)
    assert len(await rows(bot, DigestLog)) == 1

    bot.clear()
    for minute in (10, 15, 50):
        clock.set(local(10, 12, 9, minute))
        assert (await bot.tick(local(10, 12, 9, minute)))["digest"] == "done"
    assert not bot.sent(m.SendMessage)


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (14, 59, "sent"),     # опоздали почти на 6 ч (хостинг лежал с утра) — догоняем
        (15, 0, "sent"),      # ровно 6 ч — ещё догоняем
        (15, 30, "not_due"),  # больше 6 ч — эту неделю пропускаем, как и в режиме polling
    ],
)
async def test_digest_catch_up_window(
    pair: tuple[Instance, Instance], clock: Any, hour: int, minute: int, expected: str
) -> None:
    bot, _ = pair
    await seed(bot)
    clock.set(local(10, 12, hour, minute))
    assert (await bot.tick(local(10, 12, hour, minute)))["digest"] == expected
    assert len(bot.sent(m.SendMessage)) == (2 if expected == "sent" else 0)


async def test_digest_not_due_on_other_days_and_follows_settings(
    pair: tuple[Instance, Instance], set_env: Callable[..., None], clock: Any
) -> None:
    """Сводка — только в свой день и час: DIGEST_WEEKDAY=4 / DIGEST_HOUR=17 — пятница 17:00."""
    bot, _ = pair
    await seed(bot)
    clock.set(local(10, 2, 12))
    assert (await bot.tick(local(10, 2, 12)))["digest"] == "not_due"  # пятница, а сводка по понедельникам
    set_env(DIGEST_WEEKDAY="4", DIGEST_HOUR="17")
    assert (await bot.tick(local(10, 2, 16, 55)))["digest"] == "not_due"
    clock.set(local(10, 2, 17, 5))
    assert (await bot.tick(local(10, 2, 17, 5)))["digest"] == "sent"


async def test_digest_sent_once_when_two_instances_tick_at_the_same_time(
    pair: tuple[Instance, Instance], clock: Any
) -> None:
    old, new = pair
    await seed(old)
    clock.set(local(10, 12, 9, 5))
    first, second = await asyncio.gather(old.tick(local(10, 12, 9, 5)), new.tick(local(10, 12, 9, 5)))
    assert sorted([first["digest"], second["digest"]]) == ["done", "sent"]
    assert chats(old.sent(m.SendMessage) + new.sent(m.SendMessage)) == [MGR, MGR2]
    assert len(await rows(old, DigestLog)) == 1


async def test_digest_not_delivered_to_anyone_is_retried_on_next_tick(
    pair: tuple[Instance, Instance], clock: Any
) -> None:
    """Оба руководителя заблокировали бота — сводка никому не дошла и не считается отправленной:
    следующий «тик» после разблокировки её отправляет."""
    bot, _ = pair
    await seed(bot)
    bot.api.blocked_chats.update({MGR, MGR2})
    clock.set(local(10, 12, 9, 5))
    assert (await bot.tick(local(10, 12, 9, 5)))["digest"] == "failed"
    assert await rows(bot, DigestLog) == [] and await rows(bot, JobLog) == []

    bot.api.blocked_chats.clear()
    bot.clear()
    clock.set(local(10, 12, 9, 10))
    assert (await bot.tick(local(10, 12, 9, 10)))["digest"] == "sent"
    assert chats(bot.sent(m.SendMessage)) == [MGR, MGR2]


async def test_digest_without_employees_not_attempted_every_tick(
    pair: tuple[Instance, Instance], clock: Any
) -> None:
    """Сотрудников нет — сводку не о ком отправлять; следующие «тики» её больше не пробуют."""
    bot, _ = pair
    await seed(bot, employees=False)
    clock.set(local(10, 12, 9, 5))
    assert (await bot.tick(local(10, 12, 9, 5)))["digest"] == "nobody"
    assert (await bot.tick(local(10, 12, 9, 10)))["digest"] == "done"
    assert not bot.sent(m.SendMessage)


# --- Напоминания ------------------------------------------------------------------------------------


async def test_reminder_sent_once_when_two_instances_tick_at_the_same_time(
    pair: tuple[Instance, Instance], clock: Any
) -> None:
    """Пятница 12:00, срок задачи через 2,5 дня: напоминание «осталось 3 дня» сотрудник получает
    один раз, хотя будильник попал в оба экземпляра бота одновременно."""
    old, new = pair
    await seed(old)
    now = local(10, 2, 12)
    clock.set(now)
    task_id = await add_task(old, now + timedelta(days=2, hours=12))
    old.clear()

    first, second = await asyncio.gather(old.tick(now), new.tick(now))
    assert sorted([first["reminders"], second["reminders"]]) == [0, 1]
    reminders = old.sent(m.SendMessage) + new.sent(m.SendMessage)
    assert chats(reminders) == [EMP] and "До срока задачи" in reminders[0].text
    assert {row.kind for row in await rows(old, ReminderLog) if row.task_id == task_id} >= {"before_3d"}
    events = [event for event in await rows(old, TaskEvent) if event.data.get("kind") == "before_3d"]
    assert len(events) == 1

    old.clear()
    new.clear()
    assert (await old.tick(now + timedelta(minutes=5)))["reminders"] == 0
    assert not old.sent(m.SendMessage)


async def test_reminders_respect_quiet_hours(pair: tuple[Instance, Instance], clock: Any) -> None:
    """Тихие часы (21:00–8:00): по будильнику напоминания не уходят; в 8:00 — уходят."""
    bot, _ = pair
    await seed(bot)
    clock.set(local(10, 2, 12))
    await add_task(bot, local(10, 2, 12) + timedelta(days=2, hours=12))
    bot.clear()
    clock.set(local(10, 2, 22))
    assert (await bot.tick(local(10, 2, 22)))["reminders"] == 0
    clock.set(local(10, 3, 8))
    assert (await bot.tick(local(10, 3, 8)))["reminders"] == 1


# --- Устойчивость -----------------------------------------------------------------------------------


async def test_failing_job_does_not_stop_the_others(
    pair: tuple[Instance, Instance], monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    """Напоминания упали (например, сбой БД) — сводка и резервная копия всё равно выполняются."""
    bot, _ = pair
    await seed(bot)

    async def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("сбой")

    monkeypatch.setattr(jobs, "run_reminders", broken)
    clock.set(local(10, 2, 23, 5))
    result = await bot.tick(local(10, 2, 23, 5))
    assert result == {"evaluations": 0, "reminders": "error", "digest": "not_due", "backup": "sent:2"}


async def test_tick_without_now_uses_current_time(
    pair: tuple[Instance, Instance], clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Будильник вызывает run_due_jobs без времени — берётся «сейчас» (utcnow)."""
    bot, _ = pair
    await seed(bot)
    monkeypatch.setattr(jobs, "utcnow", lambda: clock.now)
    clock.set(local(10, 2, 23, 5))
    assert await run_due_jobs(bot.bot, bot.sessionmaker) == {
        "evaluations": 0, "reminders": 0, "digest": "not_due", "backup": "sent:2"
    }


def test_tick_schedule_summary_mentions_jobs(set_env: Callable[..., None]) -> None:
    summary = jobs.tick_schedule_summary()
    assert "напоминания" in summary and "понедельникам с 9:00" in summary and "после 23:00" in summary
    assert "cron-job.org" not in summary and "/tick" not in summary
    # Как часто бот сам запускает задания (bot.web.JOB_INTERVAL_SEC) — для строки в лог при запуске.
    assert "напоминания — каждые 5 мин (кроме тихих часов 21:00–8:00)" in jobs.tick_schedule_summary(300)
    assert "напоминания — каждые 0.5 с" in jobs.tick_schedule_summary(0.5)
    set_env(BACKUP_ENABLED="false")
    assert "резервная копия выключена" in jobs.tick_schedule_summary()


# --- Сдачи, оценку которых прервала остановка бота ---------------------------------------------------------


async def submit(instance: Instance, task_id: int, *, fact_value: float | None = None) -> int:
    """Сотрудник сдал результат; оценки нет — бота остановили посреди AI-оценки (время — clock)."""
    async with instance.sessionmaker() as session:
        employee = await session.scalar(select(User).where(User.tg_id == EMP))
        sub = await tasks_svc.submit_result(
            session, task_id, employee, fact_text="Проверено 90 договоров", fact_value=fact_value
        )
        await session.commit()
        return sub.id


async def test_interrupted_evaluation_reaches_manager_once(pair: tuple[Instance, Instance], clock: Any) -> None:
    """Сдача без оценки (экземпляр бота остановили посреди AI-оценки): пока оценка ещё могла идти —
    ничего; через бюджет AI + 5 мин — оценка по правилам, руководителю — сдача, сотруднику — «передан
    руководителю». Оба экземпляра тикают одновременно — сдача передаётся один раз (JobLog «eval»)."""
    old, new = pair
    await seed(old)
    task_id = await add_task(old, local(10, 20, 18))
    clock.set(local(10, 2, 12))
    sub_id = await submit(old, task_id, fact_value=90)
    old.clear()
    new.clear()

    early = await old.tick(local(10, 2, 12, 5))
    assert early["evaluations"] == 0 and not old.sent(m.SendMessage)

    first, second = await asyncio.gather(old.tick(local(10, 2, 12, 10)), new.tick(local(10, 2, 12, 10)))
    assert sorted([first["evaluations"], second["evaluations"]]) == [0, 1]
    messages = old.sent(m.SendMessage) + new.sent(m.SendMessage)
    assert chats(messages) == [MGR, EMP]
    [to_manager] = [message for message in messages if message.chat_id == MGR]
    [to_employee] = [message for message in messages if message.chat_id == EMP]
    assert "Анализ договоров" in to_manager.text and to_manager.reply_markup is not None
    assert "передан руководителю на проверку" in to_employee.text
    async with old.sessionmaker() as session:
        sub = await session.get(Submission, sub_id)
        assert sub is not None and (sub.ai_source, sub.ai_score) == ("rules", 90)
        assert sub.ai_rationale.startswith(RULES_PREFIX)
    assert [(row.job, row.key) for row in await rows(old, JobLog)] == [("eval", str(sub_id))]

    old.clear()
    new.clear()
    assert (await new.tick(local(10, 2, 12, 15)))["evaluations"] == 0
    assert (await old.tick(local(10, 2, 13)))["evaluations"] == 0
    assert not old.sent(m.SendMessage) and not new.sent(m.SendMessage)


async def test_evaluated_or_old_submissions_are_not_touched(pair: tuple[Instance, Instance], clock: Any) -> None:
    """Оценённую сдачу (обработчик успел) не трогаем; сдачу старше суток — тоже: о ней руководителю
    напомнит review_pending. Режим polling запускает ту же проверку из APScheduler."""
    bot, _ = pair
    await seed(bot)
    evaluated = await add_task(bot, local(10, 20, 18))
    old_one = await add_task(bot, local(10, 20, 18))
    clock.set(local(10, 1, 6))
    old_sub = await submit(bot, old_one)
    clock.set(local(10, 2, 12))
    sub_id = await submit(bot, evaluated)
    async with bot.sessionmaker() as session:
        await tasks_svc.record_evaluation(session, sub_id, score=100, rationale="План выполнен.", source="ai")
        await session.commit()
    bot.clear()

    result = await bot.tick(local(10, 2, 12, 30))
    assert result["evaluations"] == 0 and not bot.sent(m.SendMessage)
    async with bot.sessionmaker() as session:
        assert (await session.get(Submission, old_sub)).ai_source is None
    assert not await rows(bot, JobLog)
    assert jobs.setup_scheduler(bot.bot, bot.sessionmaker).get_job("stalled_evaluations") is not None
