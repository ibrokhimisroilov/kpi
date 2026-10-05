"""Гонки: два руководителя (или руководитель и сотрудник) действуют над одной задачей одновременно.

Бот обрабатывает апдейты разных пользователей параллельно, каждый — в своей сессии БД. Тесты
воспроизводят это двумя сессиями к одной файловой SQLite (как в проде: WAL, отдельные соединения):
оба уже открыли экран (прочитали задачу/сдачу), затем нажимают кнопки. Решение должен принять ровно
один, второй — получить понятный отказ («Результат уже обработан», «Предложение уже обработано»),
а в БД и журнале не должно остаться следов второго решения (SPEC 3.2, 7.4, 7.5, 7.7).

В конце — тот же сценарий целиком через бота (bot.main.build_dispatcher + фейковый Telegram API).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot, Router
from aiogram.client.default import DefaultBotProperties
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.base import init_db, make_engine, make_sessionmaker
from bot.db.models import (
    EventType,
    ReviewDecision,
    Role,
    Submission,
    Task,
    TaskEvent,
    TaskStatus,
    User,
    UserStatus,
)
from bot.services import tasks as svc
from bot.services import users
from bot.services.errors import DomainError
from bot.utils.dates import utcnow

ALREADY_REVIEWED = "Результат уже обработан"
ALREADY_DECIDED = "Предложение уже обработано"
NOT_CANCELLABLE = "Отменить можно только незавершённую задачу"
CHANGED_MEANWHILE = "Задача только что изменилась — откройте её заново и повторите действие"
DECISION_EVENTS = {
    EventType.APPROVED,
    EventType.REJECTED,
    EventType.SCORE_CONFIRMED,
    EventType.SCORE_CHANGED,
    EventType.REWORK,
    EventType.CANCELLED,
}


# --- Офис: файловая БД, два руководителя и сотрудник ----------------------------------------------


@dataclass
class Office:
    sm: async_sessionmaker[AsyncSession]
    boss_id: int      # Петров — поставил задачу (ADMIN_IDS)
    deputy_id: int    # Смирнова — второй руководитель
    employee_id: int  # Иванов — исполнитель


def file_db_url(path: Path) -> str:
    return "sqlite+aiosqlite:///" + path.as_posix()


@pytest_asyncio.fixture
async def office(tmp_path: Path, clock) -> AsyncIterator[Office]:
    engine = make_engine(file_db_url(tmp_path / "race.db"))
    await init_db(engine)
    sm = make_sessionmaker(engine)
    async with sm() as s:
        boss, _ = await users.register_or_get(s, 1001, "boss", "Петров Пётр Петрович")
        deputy = User(tg_id=1002, full_name="Смирнова Ольга Игоревна", role=Role.MANAGER, status=UserStatus.ACTIVE)
        employee = User(tg_id=2001, full_name="Иванов Иван Иванович", role=Role.EMPLOYEE, status=UserStatus.ACTIVE)
        s.add_all([deputy, employee])
        await s.commit()
        ids = boss.id, deputy.id, employee.id
    try:
        yield Office(sm, *ids)
    finally:
        await engine.dispose()


async def active_task(office: Office, clock) -> int:
    """Петров ставит Иванову «Анализ договоров»: проверить 100 договоров за 3 дня."""
    async with office.sm() as s:
        boss = await s.get(User, office.boss_id)
        task = await svc.create_task(
            s, creator=boss, assignee_id=office.employee_id, title="Анализ договоров",
            expected_result="Проверить 100 договоров и представить отчёт", deadline=clock.now + timedelta(days=3),
            weight=20, plan_value=100, plan_unit="договоров",
        )
        await s.commit()
        return task.id


async def submit(office: Office, task_id: int, fact_value: float = 110) -> int:
    """Иванов сдаёт результат, правила предлагают оценку (110 % при факте 110)."""
    async with office.sm() as s:
        employee = await s.get(User, office.employee_id)
        sub = await svc.submit_result(
            s, task_id, employee, fact_text=f"Проверено {fact_value:g} договоров", fact_value=fact_value
        )
        await svc.record_evaluation(s, sub.id, score=fact_value, rationale="План 100, факт", source="rules")
        await s.commit()
        return sub.id


async def submitted_task(office: Office, clock) -> tuple[int, int]:
    task_id = await active_task(office, clock)
    return task_id, await submit(office, task_id)


async def proposed_task(office: Office, clock) -> int:
    """Иванов вносит устное поручение «Справка по закупкам»."""
    async with office.sm() as s:
        employee = await s.get(User, office.employee_id)
        task = await svc.propose_task(
            s, employee=employee, title="Справка по закупкам", expected_result="Справка на 2 страницы",
            deadline=clock.now + timedelta(days=4),
        )
        await s.commit()
        return task.id


@dataclass
class Screen:
    """Открытый экран пользователя: своя сессия БД, задача (и сдача) уже прочитаны."""

    session: AsyncSession
    user: User


@asynccontextmanager
async def screen(office: Office, user_id: int, task_id: int) -> AsyncIterator[Screen]:
    async with office.sm() as s:
        user = await s.get(User, user_id)
        assert user is not None
        task = await svc.get_task(s, task_id)
        assert task is not None
        yield Screen(s, user)


@dataclass
class DbState:
    status: TaskStatus
    final_score: float | None
    decisions: list[ReviewDecision | None]
    events: list[EventType]

    @property
    def decision_events(self) -> list[EventType]:
        return [event for event in self.events if event in DECISION_EVENTS]


async def db_state(office: Office, task_id: int) -> DbState:
    """Что реально сохранено в БД (свежая сессия)."""
    async with office.sm() as s:
        task = await s.get(Task, task_id)
        assert task is not None
        subs = list(await s.scalars(select(Submission).where(Submission.task_id == task_id).order_by(Submission.id)))
        events = list(await s.scalars(select(TaskEvent.type).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id)))
        return DbState(task.status, task.final_score, [sub.decision for sub in subs], events)


# --- Одновременные действия -------------------------------------------------------------------------

Action = Callable[[AsyncSession, User], Awaitable[Any]]
Opener = Callable[[AsyncSession], Awaitable[Any]]


def task_screen(task_id: int) -> Opener:
    return lambda s: svc.get_task(s, task_id)


async def at_once(office: Office, open_screen: Opener, *moves: tuple[int, Action]) -> list[Any]:
    """Каждый открыл экран (open_screen читает объект в своей сессии), затем все одновременно нажали кнопку.

    Возвращает результат каждого действия или DomainError. После отказа сессия коммитится — как
    в хендлерах, которые ловят DomainError и показывают alert (middleware затем делает commit):
    если бы сервис успел что-то записать до отказа, это попало бы в БД и тест бы это увидел.
    """
    barrier = asyncio.Barrier(len(moves))

    async def play(user_id: int, action: Action) -> Any:
        async with office.sm() as s:
            actor = await s.get(User, user_id)
            assert actor is not None
            await open_screen(s)
            await barrier.wait()
            try:
                result = await action(s, actor)
            except DomainError as exc:
                await s.commit()
                return exc
            await s.commit()
            return result

    return await asyncio.wait_for(asyncio.gather(*(play(uid, action) for uid, action in moves)), timeout=30)


def split(results: list[Any]) -> tuple[int, DomainError]:
    """Индекс единственного успешного действия и отказ второго."""
    errors = [r for r in results if isinstance(r, DomainError)]
    winners = [i for i, r in enumerate(results) if not isinstance(r, BaseException)]
    assert len(winners) == 1 and len(errors) == len(results) - 1, results
    return winners[0], errors[0]


# =====================================================================================================
# Проверка результата
# =====================================================================================================


def review_moves(task_id: int, sub_id: int) -> dict[str, tuple[Action, TaskStatus, EventType]]:
    """Кнопки проверки: действие, статус задачи после него, событие журнала."""
    return {
        "confirm": (lambda s, m: svc.review_confirm(s, sub_id, m), TaskStatus.DONE, EventType.SCORE_CONFIRMED),
        "change": (
            lambda s, m: svc.review_set_score(s, sub_id, m, 90, "Без рекомендаций"),
            TaskStatus.DONE,
            EventType.SCORE_CHANGED,
        ),
        "rework": (
            lambda s, m: svc.review_rework(s, sub_id, m, "Добавьте реестр нарушений"),
            TaskStatus.REWORK,
            EventType.REWORK,
        ),
        "cancel": (lambda s, m: svc.cancel_task(s, task_id, m, "Неактуально"), TaskStatus.CANCELLED, EventType.CANCELLED),
    }


async def test_second_manager_cannot_change_score_after_first_confirmed(office: Office, clock) -> None:
    """Петров и Смирнова открыли результат Иванова. Петров подтвердил «110 %», Смирнова секундой позже
    ставит «90 %» — бот отвечает «Результат уже обработан». Итог — 110 %, в журнале одно решение,
    а экран Смирновой после отказа показывает актуальное состояние (оценено Петровым)."""
    task_id, sub_id = await submitted_task(office, clock)
    async with screen(office, office.deputy_id, task_id) as late:
        async with screen(office, office.boss_id, task_id) as first:
            await svc.review_confirm(first.session, sub_id, first.user)
            await first.session.commit()

        with pytest.raises(DomainError) as refused:
            await svc.review_set_score(late.session, sub_id, late.user, 90, "Мало")
        assert refused.value.message == ALREADY_REVIEWED
        task = await svc.get_task(late.session, task_id)
        assert task is not None
        assert (task.status, task.final_score) == (TaskStatus.DONE, 110)
        assert task.last_submission is not None
        assert task.last_submission.decision == ReviewDecision.APPROVED
        assert task.last_submission.reviewer_id == office.boss_id
        await late.session.commit()  # как после alert: ничего лишнего не сохраняется

    state = await db_state(office, task_id)
    assert (state.status, state.final_score) == (TaskStatus.DONE, 110)
    assert state.decisions == [ReviewDecision.APPROVED]
    assert state.decision_events == [EventType.SCORE_CONFIRMED]


async def test_confirm_after_other_manager_returned_for_rework(office: Office, clock) -> None:
    """Смирнова вернула результат на доработку, Петров на старом экране жмёт «✅ Подтвердить 110 %» —
    «Результат уже обработан»; задача остаётся на доработке, Иванову ничего не засчитано."""
    task_id, sub_id = await submitted_task(office, clock)
    async with screen(office, office.boss_id, task_id) as stale:
        async with screen(office, office.deputy_id, task_id) as first:
            await svc.review_rework(first.session, sub_id, first.user, "Добавьте реестр нарушений")
            await first.session.commit()
        with pytest.raises(DomainError, match=f"^{ALREADY_REVIEWED}$"):
            await svc.review_confirm(stale.session, sub_id, stale.user)
        await stale.session.commit()

    state = await db_state(office, task_id)
    assert (state.status, state.final_score) == (TaskStatus.REWORK, None)
    assert state.decisions == [ReviewDecision.REWORK]
    assert state.decision_events == [EventType.REWORK]


async def test_stale_review_screen_after_resubmission(office: Office, clock) -> None:
    """Смирнова открыла первую сдачу и отвлеклась. Петров вернул её на доработку, Иванов сдал заново —
    задача снова «на проверке». Смирнова жмёт «Подтвердить» на старом экране первой сдачи: бот отвечает
    «Результат уже обработан», вторая сдача по-прежнему ждёт проверки (статус задачи совпадает,
    но решение по старой сдаче не должно закрыть новую)."""
    task_id, first_sub = await submitted_task(office, clock)
    async with screen(office, office.deputy_id, task_id) as stale:
        async with office.sm() as s:
            boss = await s.get(User, office.boss_id)
            await svc.review_rework(s, first_sub, boss, "Добавьте реестр нарушений")
            await s.commit()
        second_sub = await submit(office, task_id, fact_value=100)

        with pytest.raises(DomainError, match=f"^{ALREADY_REVIEWED}$"):
            await svc.review_confirm(stale.session, first_sub, stale.user)
        await stale.session.commit()

    state = await db_state(office, task_id)
    assert state.status == TaskStatus.SUBMITTED
    assert state.decisions == [ReviewDecision.REWORK, None]
    assert state.decision_events == [EventType.REWORK]
    async with office.sm() as s:  # новую сдачу можно проверить как обычно
        deputy = await s.get(User, office.deputy_id)
        done = await svc.review_confirm(s, second_sub, deputy)
        assert (done.status, done.final_score) == (TaskStatus.DONE, 100)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("confirm", "confirm"),
        ("confirm", "change"),
        ("change", "rework"),
        ("rework", "rework"),
        ("rework", "cancel"),
        ("cancel", "confirm"),
    ],
)
async def test_two_managers_decide_simultaneously(office: Office, clock, first: str, second: str) -> None:
    """Петров и Смирнова одновременно нажимают кнопки по одному результату Иванова: срабатывает ровно
    одно решение, второй получает понятный отказ (а не «database is locked»), в журнале одно решение."""
    task_id, sub_id = await submitted_task(office, clock)
    moves = review_moves(task_id, sub_id)
    results = await at_once(
        office, task_screen(task_id), (office.boss_id, moves[first][0]), (office.deputy_id, moves[second][0])
    )
    winner, refusal = split(results)
    won = (first, second)[winner]
    lost = (first, second)[1 - winner]
    _, status, event = moves[won]

    if lost != "cancel":
        expected_refusal = ALREADY_REVIEWED
    else:  # отмена проиграла: после доработки задачу ещё можно отменить, после оценки — нет
        expected_refusal = CHANGED_MEANWHILE if status == TaskStatus.REWORK else NOT_CANCELLABLE
    assert refusal.message == expected_refusal

    state = await db_state(office, task_id)
    assert state.status == status
    assert state.decision_events == [event]
    if status == TaskStatus.DONE:
        assert state.final_score == (110 if won == "confirm" else 90)


# =====================================================================================================
# Предложения сотрудника
# =====================================================================================================


def proposal_moves(task_id: int) -> dict[str, tuple[Action, TaskStatus, EventType]]:
    return {
        "approve": (lambda s, m: svc.approve_proposal(s, task_id, m, weight=20), TaskStatus.ACTIVE, EventType.APPROVED),
        "reject": (
            lambda s, m: svc.reject_proposal(s, task_id, m, "Не входит в план"),
            TaskStatus.REJECTED,
            EventType.REJECTED,
        ),
        "cancel": (lambda s, m: svc.cancel_task(s, task_id, m), TaskStatus.CANCELLED, EventType.CANCELLED),
    }


async def test_proposal_rejected_after_other_manager_approved(office: Office, clock) -> None:
    """Петров подтвердил поручение Иванова (вес 20 %), Смирнова на своём уведомлении жмёт «❌ Отклонить» —
    «Предложение уже обработано»; поручение в работе с весом Петрова, отказа в журнале нет."""
    task_id = await proposed_task(office, clock)
    async with screen(office, office.deputy_id, task_id) as late:
        async with screen(office, office.boss_id, task_id) as first:
            await svc.approve_proposal(first.session, task_id, first.user, weight=20)
            await first.session.commit()
        with pytest.raises(DomainError, match=f"^{ALREADY_DECIDED}$"):
            await svc.reject_proposal(late.session, task_id, late.user, "Не входит в план")
        task = await svc.get_task(late.session, task_id)
        assert task is not None and task.status == TaskStatus.ACTIVE  # экран обновлён
        await late.session.commit()

    state = await db_state(office, task_id)
    assert state.status == TaskStatus.ACTIVE
    assert state.events == [EventType.PROPOSED, EventType.APPROVED]
    async with office.sm() as s:
        task = await s.get(Task, task_id)
        assert task is not None and (task.weight, task.manager_id) == (20, office.boss_id)


@pytest.mark.parametrize(
    ("first", "second"),
    [("approve", "approve"), ("approve", "reject"), ("reject", "reject"), ("approve", "cancel")],
)
async def test_two_managers_decide_proposal_simultaneously(office: Office, clock, first: str, second: str) -> None:
    """Оба руководителя одновременно решают по одному поручению: срабатывает одно решение."""
    task_id = await proposed_task(office, clock)
    moves = proposal_moves(task_id)
    results = await at_once(
        office, task_screen(task_id), (office.boss_id, moves[first][0]), (office.deputy_id, moves[second][0])
    )
    winner, refusal = split(results)
    won = (first, second)[winner]
    lost = (first, second)[1 - winner]
    _, status, event = moves[won]

    if lost != "cancel":
        expected_refusal = ALREADY_DECIDED
    else:  # подтверждённое поручение отменить можно, но не по устаревшему экрану
        expected_refusal = CHANGED_MEANWHILE if status == TaskStatus.ACTIVE else NOT_CANCELLABLE
    assert refusal.message == expected_refusal
    state = await db_state(office, task_id)
    assert state.status == status
    assert state.decision_events == [event]


# =====================================================================================================
# Отмена и сдача результата
# =====================================================================================================


async def test_two_managers_cancel_same_task(office: Office, clock) -> None:
    """Оба руководителя одновременно отменяют одну задачу: отмена одна, второй — «уже нельзя отменить»."""
    task_id = await active_task(office, clock)
    action: Action = lambda s, m: svc.cancel_task(s, task_id, m, "Неактуально")  # noqa: E731
    results = await at_once(office, task_screen(task_id), (office.boss_id, action), (office.deputy_id, action))
    _, refusal = split(results)
    assert refusal.message == NOT_CANCELLABLE
    state = await db_state(office, task_id)
    assert state.status == TaskStatus.CANCELLED
    assert state.decision_events == [EventType.CANCELLED]


async def test_submission_after_task_was_cancelled(office: Office, clock) -> None:
    """Иванов заполнял ответы для сдачи, а Петров тем временем отменил задачу. Нажатие «📤 Отправить»
    не «оживляет» отменённую задачу: «Задача не в работе — сдать результат нельзя», сдач нет."""
    task_id = await active_task(office, clock)
    async with screen(office, office.employee_id, task_id) as employee:
        async with office.sm() as s:
            boss = await s.get(User, office.boss_id)
            await svc.cancel_task(s, task_id, boss, "Неактуально")
            await s.commit()
        with pytest.raises(DomainError, match="^Задача не в работе — сдать результат нельзя$"):
            await svc.submit_result(employee.session, task_id, employee.user, fact_text="Проверено 100 договоров")
        await employee.session.commit()

    state = await db_state(office, task_id)
    assert state.status == TaskStatus.CANCELLED
    assert state.decisions == []
    assert EventType.SUBMITTED not in state.events


async def test_cancel_confirmed_after_employee_submitted(office: Office, clock) -> None:
    """Петров открыл «🚫 Отменить задачу?», а Иванов в это время сдал результат. Отмена по устаревшему
    экрану не проходит: «Задача только что изменилась…» — результат ждёт проверки."""
    task_id = await active_task(office, clock)
    async with screen(office, office.boss_id, task_id) as manager:
        await submit(office, task_id)
        with pytest.raises(DomainError) as refused:
            await svc.cancel_task(manager.session, task_id, manager.user, "Неактуально")
        assert refused.value.message == CHANGED_MEANWHILE
        await manager.session.commit()

    state = await db_state(office, task_id)
    assert state.status == TaskStatus.SUBMITTED
    assert EventType.CANCELLED not in state.events


async def test_submission_uses_deadline_changed_meanwhile(office: Office, clock) -> None:
    """Иванов открыл сдачу, руководитель тем временем перенёс срок на неделю вперёд. Сдача после
    старого срока, но до нового — не считается просроченной."""
    task_id = await active_task(office, clock)
    async with screen(office, office.employee_id, task_id) as employee:
        async with office.sm() as s:
            boss = await s.get(User, office.boss_id)
            await svc.update_task(s, task_id, boss, deadline=clock.now + timedelta(days=7))
            await s.commit()
        clock.advance(days=4)  # старый срок (3 дня) прошёл, новый — нет
        sub = await svc.submit_result(employee.session, task_id, employee.user, fact_text="Проверено 100 договоров")
        assert sub.is_late is False and sub.late_days == 0
        assert sub.deadline_at_submit == clock.now + timedelta(days=3)
        await employee.session.commit()


# =====================================================================================================
# Заявки на доступ
# =====================================================================================================


@pytest.mark.parametrize(("first", "second"), [("approve", "reject"), ("approve", "approve"), ("reject", "reject")])
async def test_two_managers_decide_registration_simultaneously(office: Office, first: str, second: str) -> None:
    """Заявка Кузнецовой пришла обоим руководителям, оба одновременно нажали кнопки в уведомлении.
    Срабатывает одно решение; второй получает «Заявка уже обработана» — сотрудница не может оказаться
    заблокированной сразу после сообщения «Доступ открыт»."""
    async with office.sm() as s:
        applicant = User(tg_id=2002, full_name="Кузнецова Мария Олеговна", status=UserStatus.PENDING)
        s.add(applicant)
        await s.commit()
        applicant_id = applicant.id
    moves: dict[str, tuple[Action, UserStatus]] = {
        "approve": (lambda s, m: users.approve_user(s, applicant_id, m), UserStatus.ACTIVE),
        "reject": (lambda s, m: users.reject_user(s, applicant_id, m), UserStatus.BLOCKED),
    }
    results = await at_once(
        office,
        lambda s: users.get_user(s, applicant_id),
        (office.boss_id, moves[first][0]),
        (office.deputy_id, moves[second][0]),
    )
    winner, refusal = split(results)
    assert refusal.message == "Заявка уже обработана"
    async with office.sm() as s:
        stored = await s.get(User, applicant_id)
        assert stored is not None and stored.status == moves[(first, second)[winner]][1]


async def test_two_managers_block_same_employee(office: Office) -> None:
    """Оба руководителя одновременно блокируют Иванова: блокировка одна, второй — «Пользователь уже заблокирован»."""
    action: Action = lambda s, m: users.block_user(s, office.employee_id, m)  # noqa: E731
    results = await at_once(
        office, lambda s: users.get_user(s, office.employee_id), (office.boss_id, action), (office.deputy_id, action)
    )
    _, refusal = split(results)
    assert refusal.message == "Пользователь уже заблокирован"


# =====================================================================================================
# Целиком через бота: два руководителя одновременно жмут «✅ Подтвердить»
# =====================================================================================================

BOSS, DEPUTY, EMP = 1001, 1002, 2001


def _release_bot_routers() -> None:
    """Отвязать модульные роутеры хендлеров от Dispatcher'а прошлого теста (aiogram не даёт подключить
    роутер ко второму родителю). То же делает фикстура ``app`` в tests/e2e/conftest.py."""
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
        router._parent_router = None


@pytest_asyncio.fixture
async def bot_app(tmp_path: Path) -> AsyncIterator[Any]:
    """Бот целиком на фейковом Telegram API, но с файловой БД — чтобы сессии были настоящими параллельными."""
    from e2e.fakebot import BotHarness, FakeSession

    from bot.main import build_dispatcher

    engine = make_engine(file_db_url(tmp_path / "bot.db"))
    try:
        await init_db(engine)
        sm = make_sessionmaker(engine)
        _release_bot_routers()
        dp = build_dispatcher(sm)
        bot = Bot("42:TEST", session=FakeSession(), default=DefaultBotProperties(parse_mode="HTML"))
        try:
            yield BotHarness(dp, bot, sm)
        finally:
            await bot.session.close()
            _release_bot_routers()
    finally:
        await engine.dispose()


async def test_bot_two_managers_press_confirm_at_the_same_time(bot_app: Any, monkeypatch) -> None:
    """Иванов сдал «Анализ договоров» (план 100, факт 110). Петров и Смирнова открыли «📝 На проверке»
    и одновременно нажали «✅ Подтвердить 110 %». Один видит «✅ Оценка подтверждена», другой — alert
    «Результат уже обработан». Иванов получает итоговую оценку один раз, в журнале одно подтверждение."""
    h = bot_app
    await h.seed_user(BOSS, "Петров Пётр Петрович", role="manager")
    await h.seed_user(DEPUTY, "Смирнова Ольга Игоревна", role="manager")
    await h.seed_user(EMP, "Иванов Иван Иванович")
    async with h.db() as s:
        boss = await users.get_by_tg(s, BOSS)
        employee = await users.get_by_tg(s, EMP)
        task = await svc.create_task(
            s, creator=boss, assignee_id=employee.id, title="Анализ договоров",
            expected_result="Проверить 100 договоров", deadline=utcnow() + timedelta(days=3), weight=20,
            plan_value=100, plan_unit="договоров",
        )
        sub = await svc.submit_result(s, task.id, employee, fact_text="Проверено 110 договоров", fact_value=110)
        await svc.record_evaluation(s, sub.id, score=110, rationale="План 100, факт 110", source="rules")
        await s.commit()
        task_id = task.id

    for manager in (BOSS, DEPUTY):
        await h.send_command(manager, "review")
        await h.press_button(manager, "Анализ договоров")
        assert h.has_button(manager, "Подтвердить 110 %")

    # Оба хендлера уже проверили, что результат ждёт решения; решения принимаются одновременно.
    barrier = asyncio.Barrier(2)
    original = svc.review_confirm

    async def confirm_together(*args: Any, **kwargs: Any) -> Any:
        async with asyncio.timeout(10):
            await barrier.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(svc, "review_confirm", confirm_together)
    employee_messages = len(h.sent_to(EMP))
    await asyncio.wait_for(
        asyncio.gather(h.press_button(BOSS, "Подтвердить 110 %"), h.press_button(DEPUTY, "Подтвердить 110 %")),
        timeout=30,
    )

    alerts = {manager: h.alerts_for(manager)[-1] for manager in (BOSS, DEPUTY)}
    assert sorted(alerts.values()) == sorted(["✅ Оценка подтверждена", ALREADY_REVIEWED])
    new_for_employee = h.sent_to(EMP)[employee_messages:]
    assert len(new_for_employee) == 1
    assert "Итоговая оценка: 110 %" in new_for_employee[0]

    async with h.db() as s:
        stored = await s.get(Task, task_id)
        assert stored is not None and (stored.status, stored.final_score) == (TaskStatus.DONE, 110)
        confirmed = list(
            await s.scalars(
                select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.type == EventType.SCORE_CONFIRMED)
            )
        )
        assert len(confirmed) == 1
