"""bot.services.users и bot.services.tasks: жизненный цикл задачи, права, журнал (SPEC 3.1, 3.2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from bot.db.models import (
    AttachmentKind,
    EventType,
    Priority,
    ReminderLog,
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
from bot.services import tasks as svc
from bot.services import users
from bot.services.errors import DomainError
from bot.services.tasks import AttachmentIn
from bot.utils.dates import to_utc

pytestmark = pytest.mark.usefixtures("clock")


def in_days(clock, days: float) -> datetime:
    return clock.now + timedelta(days=days)


async def event_types(session, task_id: int) -> list[EventType]:
    return [event.type for event in await svc.task_events(session, task_id)]


async def reminder_count(session, task_id: int) -> int:
    stmt = select(func.count()).select_from(ReminderLog).where(ReminderLog.task_id == task_id)
    return int(await session.scalar(stmt) or 0)


async def new_task(session, clock, manager: User, assignee: User, **overrides) -> Task:
    values = {
        "creator": manager,
        "assignee_id": assignee.id,
        "title": "Анализ договоров",
        "expected_result": "Проверить 100 договоров и представить отчёт",
        "deadline": in_days(clock, 3),
        "weight": 20,
        "plan_value": 100,
        "plan_unit": "договоров",
    }
    values.update(overrides)
    return await svc.create_task(session, **values)


# =====================================================================================================
# Пользователи
# =====================================================================================================


async def test_register_admin_becomes_active_manager(session) -> None:
    user, created = await users.register_or_get(session, 1001, "boss", "  Петров   Пётр ")
    assert created
    assert user.role == Role.MANAGER and user.status == UserStatus.ACTIVE
    assert user.full_name == "Петров Пётр"

    # Даже если права «потеряны», повторный /start снова делает активным начальником.
    user.role, user.status = Role.EMPLOYEE, UserStatus.BLOCKED
    again, created_again = await users.register_or_get(session, 1001, "boss2", "Петров Пётр")
    assert again is user and not created_again
    assert again.is_manager
    assert again.username == "boss2"


async def test_registration_flow(session, manager: User) -> None:
    user, created = await users.register_or_get(session, 5001, "new", "New User")
    assert created
    assert (user.role, user.status, user.full_name) == (Role.EMPLOYEE, UserStatus.PENDING, "")
    assert await users.list_pending(session) == []  # регистрация не завершена

    await users.complete_registration(session, user, "  Новиков   Николай  Николаевич ", "Аналитик")
    assert user.full_name == "Новиков Николай Николаевич"
    assert user.position == "Аналитик"
    assert user.status == UserStatus.PENDING
    assert await users.list_pending(session) == [user]

    await users.approve_user(session, user.id, manager)
    assert user.status == UserStatus.ACTIVE and user.role == Role.EMPLOYEE
    assert await users.list_pending(session) == []
    with pytest.raises(DomainError):
        await users.approve_user(session, user.id, manager)  # уже обработана
    assert await users.get_by_tg(session, 5001) is user
    assert await users.get_user(session, user.id) is user


async def test_reject_and_unblock(session, manager: User, user_factory) -> None:
    pending = await user_factory(5002, "Петрова Мария", status=UserStatus.PENDING)
    await users.reject_user(session, pending.id, manager)
    assert pending.status == UserStatus.BLOCKED
    with pytest.raises(DomainError):
        await users.reject_user(session, pending.id, manager)
    await users.unblock_user(session, pending.id, manager)
    assert pending.status == UserStatus.ACTIVE


async def test_block_rules(session, manager: User, employee: User) -> None:
    with pytest.raises(DomainError):
        await users.block_user(session, manager.id, manager)  # себя нельзя
    await users.block_user(session, employee.id, manager)
    assert employee.status == UserStatus.BLOCKED
    with pytest.raises(DomainError):
        await users.block_user(session, 999_999, manager)  # нет такого пользователя


async def test_actor_must_be_active_manager(session, manager: User, employee: User, employee2: User) -> None:
    pending_manager = User(id=777, tg_id=777, full_name="Х", role=Role.MANAGER, status=UserStatus.PENDING)
    for actor in (employee, pending_manager):
        with pytest.raises(DomainError):
            await users.block_user(session, employee2.id, actor)
        with pytest.raises(DomainError):
            await users.approve_user(session, employee2.id, actor)
        with pytest.raises(DomainError):
            await users.set_role(session, employee2.id, Role.MANAGER, actor)
        with pytest.raises(DomainError):
            await users.unblock_user(session, employee2.id, actor)
        with pytest.raises(DomainError):
            await users.reject_user(session, employee2.id, actor)


async def test_set_role_rules(session, manager: User, employee: User, user_factory) -> None:
    with pytest.raises(DomainError):
        await users.set_role(session, manager.id, Role.EMPLOYEE, manager)  # понизить себя нельзя

    await users.set_role(session, employee.id, Role.MANAGER, manager)
    assert employee.is_manager
    assert set(await users.list_managers(session)) == {manager, employee}
    # Начальников двое — второго (не из ADMIN_IDS) можно понизить.
    await users.set_role(session, employee.id, Role.EMPLOYEE, manager)
    assert employee.role == Role.EMPLOYEE


async def test_last_manager_is_protected(session, user_factory) -> None:
    only_manager = await user_factory(3001, "Единственный Начальник", role=Role.MANAGER)
    # Действующее лицо — начальник, которого в БД уже нет как активного (устаревший объект).
    stale_actor = User(id=99_999, tg_id=99_999, full_name="Устаревший", role=Role.MANAGER, status=UserStatus.ACTIVE)
    with pytest.raises(DomainError, match="последний"):
        await users.set_role(session, only_manager.id, Role.EMPLOYEE, stale_actor)
    with pytest.raises(DomainError, match="последний"):
        await users.block_user(session, only_manager.id, stale_actor)
    assert only_manager.is_manager


async def test_user_lists_sorting(session, manager: User, user_factory) -> None:
    b = await user_factory(4001, "Борисов Борис")
    a = await user_factory(4002, "Алексеев Алексей")
    e = await user_factory(4003, "ёлкин Егор")
    pending = await user_factory(4004, "Ждущий Жора", status=UserStatus.PENDING)
    blocked = await user_factory(4005, "Блокированный Блок", status=UserStatus.BLOCKED)
    second_manager = await user_factory(4006, "Абрамов Начальник", role=Role.MANAGER)

    assert await users.list_employees(session) == [a, b, e]
    assert await users.list_managers(session) == [second_manager, manager]
    everyone = await users.list_all(session)
    assert everyone[0] is pending                     # сначала заявки
    assert everyone[1:3] == [second_manager, manager]  # затем активные: начальники выше
    assert everyone[3:6] == [a, b, e]
    assert everyone[-1] is blocked


# =====================================================================================================
# Задачи: постановка
# =====================================================================================================


async def test_create_task(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee, priority=Priority.HIGH, title="  Анализ  ")
    assert task.id is not None
    assert task.status == TaskStatus.ACTIVE
    assert task.source == TaskSource.MANAGER
    assert task.manager_id == manager.id and task.created_by_id == manager.id
    assert task.assignee_id == employee.id
    assert task.title == "Анализ"
    assert task.priority == Priority.HIGH
    assert task.plan_value == 100 and task.plan_unit == "договоров"
    assert task.accepted_at is None
    events = await svc.task_events(session, task.id)
    assert [e.type for e in events] == [EventType.CREATED]
    assert events[0].actor_id == manager.id


async def test_create_task_accepts_aware_deadline(session, clock, manager: User, employee: User) -> None:
    aware = (clock.now + timedelta(days=2)).replace(tzinfo=UTC)
    task = await new_task(session, clock, manager, employee, deadline=aware)
    assert task.deadline == clock.now + timedelta(days=2)
    assert task.deadline.tzinfo is None


@pytest.mark.parametrize("weight", [0, 101, -5, 10.5, True, "20"])
async def test_create_task_weight_bounds(session, clock, manager: User, employee: User, weight) -> None:
    with pytest.raises(DomainError):
        await new_task(session, clock, manager, employee, weight=weight)


@pytest.mark.parametrize("weight", [1, 100])
async def test_create_task_weight_edges_ok(session, clock, manager: User, employee: User, weight: int) -> None:
    assert (await new_task(session, clock, manager, employee, weight=weight)).weight == weight


async def test_create_task_deadline_must_be_future(session, clock, manager: User, employee: User) -> None:
    with pytest.raises(DomainError, match="будущем"):
        await new_task(session, clock, manager, employee, deadline=clock.now - timedelta(minutes=1))
    with pytest.raises(DomainError):
        await new_task(session, clock, manager, employee, deadline=clock.now)


async def test_create_task_validation(session, clock, manager: User, employee: User, user_factory) -> None:
    with pytest.raises(DomainError):
        await new_task(session, clock, manager, employee, title="   ")
    with pytest.raises(DomainError):
        await new_task(session, clock, manager, employee, expected_result="")
    with pytest.raises(DomainError):
        await new_task(session, clock, manager, employee, title="x" * 256)
    with pytest.raises(DomainError):
        await new_task(session, clock, manager, employee, plan_value=-1)
    with pytest.raises(DomainError):
        await new_task(session, clock, manager, employee, priority="urgent")


async def test_create_task_permissions(session, clock, manager: User, employee: User, employee2: User, user_factory):
    with pytest.raises(DomainError):
        await new_task(session, clock, employee, employee2, creator=employee)  # сотрудник не ставит задачи
    pending = await user_factory(5003, "Ждущий", status=UserStatus.PENDING)
    blocked = await user_factory(5004, "Блок", status=UserStatus.BLOCKED)
    for bad_assignee in (manager, pending, blocked):
        with pytest.raises(DomainError):
            await new_task(session, clock, manager, bad_assignee)
    with pytest.raises(DomainError):
        await svc.create_task(
            session, creator=manager, assignee_id=123_456, title="x", expected_result="y",
            deadline=in_days(clock, 1), weight=10,
        )


# =====================================================================================================
# Предложения сотрудника
# =====================================================================================================


async def test_propose_and_approve(session, clock, manager: User, employee: User) -> None:
    task = await svc.propose_task(
        session, employee=employee, title="Устное поручение", expected_result="Подготовить справку",
        deadline=in_days(clock, 4),
    )
    assert task.status == TaskStatus.PROPOSED
    assert task.source == TaskSource.EMPLOYEE
    assert task.assignee_id == employee.id and task.created_by_id == employee.id
    assert task.weight == 10
    assert await svc.list_proposals(session) == [task]

    with pytest.raises(DomainError):
        await svc.approve_proposal(session, task.id, employee, weight=20)  # не начальник

    approved = await svc.approve_proposal(session, task.id, manager, weight=25, priority=Priority.LOW)
    assert approved.status == TaskStatus.ACTIVE
    assert approved.weight == 25 and approved.priority == Priority.LOW
    assert approved.manager_id == manager.id
    assert approved.approved_at == clock.now and approved.accepted_at == clock.now
    assert await event_types(session, task.id) == [EventType.PROPOSED, EventType.APPROVED]

    with pytest.raises(DomainError):
        await svc.approve_proposal(session, task.id, manager, weight=25)  # уже обработано
    with pytest.raises(DomainError):
        await svc.reject_proposal(session, task.id, manager)


async def test_propose_validation(session, clock, manager: User, employee: User) -> None:
    with pytest.raises(DomainError):
        await svc.propose_task(
            session, employee=manager, title="x", expected_result="y", deadline=in_days(clock, 1)
        )
    with pytest.raises(DomainError):
        await svc.propose_task(
            session, employee=employee, title="x", expected_result="y", deadline=clock.now - timedelta(hours=1)
        )


async def test_approve_proposal_weight_bounds(session, clock, manager: User, employee: User) -> None:
    task = await svc.propose_task(
        session, employee=employee, title="x", expected_result="y", deadline=in_days(clock, 2)
    )
    for weight in (0, 101):
        with pytest.raises(DomainError):
            await svc.approve_proposal(session, task.id, manager, weight=weight)
    assert task.status == TaskStatus.PROPOSED


async def test_reject_proposal(session, clock, manager: User, employee: User) -> None:
    task = await svc.propose_task(
        session, employee=employee, title="x", expected_result="y", deadline=in_days(clock, 2)
    )
    with pytest.raises(DomainError):
        await svc.reject_proposal(session, task.id, employee, "нет")
    rejected = await svc.reject_proposal(session, task.id, manager, "  Не входит в план  ")
    assert rejected.status == TaskStatus.REJECTED
    events = await svc.task_events(session, task.id)
    assert events[-1].type == EventType.REJECTED
    assert events[-1].data["reason"] == "Не входит в план"
    with pytest.raises(DomainError):
        await svc.update_task(session, task.id, manager, title="новое")  # отклонённую не правят


# =====================================================================================================
# Правка, принятие, отмена
# =====================================================================================================


async def test_update_task_changes_and_event(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee)
    old_deadline = task.deadline
    new_deadline = in_days(clock, 5)
    session.add(ReminderLog(task_id=task.id, kind="before_3d"))
    await session.flush()

    task, changes = await svc.update_task(
        session, task.id, manager, title="Анализ договоров", weight=30, deadline=new_deadline
    )
    assert changes == {"weight": (20, 30), "deadline": (old_deadline, new_deadline)}  # title не изменился
    assert task.weight == 30 and task.deadline == new_deadline
    assert await reminder_count(session, task.id) == 0  # смена срока сбрасывает напоминания

    events = await svc.task_events(session, task.id)
    assert events[-1].type == EventType.EDITED
    assert events[-1].data["changes"]["weight"] == [20, 30]
    assert events[-1].data["changes"]["deadline"] == [old_deadline.isoformat(), new_deadline.isoformat()]
    json.dumps(events[-1].data)  # данные журнала — JSON


async def test_update_task_without_changes_writes_no_event(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee)
    _, changes = await svc.update_task(session, task.id, manager, weight=20, title="Анализ договоров")
    assert changes == {}
    assert await event_types(session, task.id) == [EventType.CREATED]


async def test_update_task_rules(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee)
    with pytest.raises(DomainError):
        await svc.update_task(session, task.id, employee, title="Моё")  # только начальник
    with pytest.raises(DomainError):
        await svc.update_task(session, task.id, manager, deadline=clock.now - timedelta(days=1))
    with pytest.raises(DomainError):
        await svc.update_task(session, task.id, manager, weight=0)
    with pytest.raises(TypeError):
        await svc.update_task(session, task.id, manager, status=TaskStatus.DONE)
    await svc.cancel_task(session, task.id, manager)
    with pytest.raises(DomainError):
        await svc.update_task(session, task.id, manager, title="После отмены")


async def test_accept_task(session, clock, manager: User, employee: User, employee2: User) -> None:
    task = await new_task(session, clock, manager, employee)
    with pytest.raises(DomainError):
        await svc.accept_task(session, task.id, employee2)
    with pytest.raises(DomainError):
        await svc.accept_task(session, task.id, manager)
    accepted = await svc.accept_task(session, task.id, employee)
    assert accepted.accepted_at == clock.now
    clock.advance(hours=1)
    again = await svc.accept_task(session, task.id, employee)  # повторно — без ошибки и без нового события
    assert again.accepted_at == clock.now - timedelta(hours=1)
    assert await event_types(session, task.id) == [EventType.CREATED, EventType.ACCEPTED]


async def test_cancel_task(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee)
    with pytest.raises(DomainError):
        await svc.cancel_task(session, task.id, employee)
    cancelled = await svc.cancel_task(session, task.id, manager, "Неактуально")
    assert cancelled.status == TaskStatus.CANCELLED
    events = await svc.task_events(session, task.id)
    assert events[-1].type == EventType.CANCELLED and events[-1].data["reason"] == "Неактуально"
    with pytest.raises(DomainError):
        await svc.cancel_task(session, task.id, manager)  # уже отменена
    with pytest.raises(DomainError):
        await svc.submit_result(session, task.id, employee, fact_text="Сделал")
    with pytest.raises(DomainError):
        await svc.cancel_task(session, 999_999, manager)


async def test_cancel_proposal(session, clock, manager: User, employee: User) -> None:
    task = await svc.propose_task(
        session, employee=employee, title="x", expected_result="y", deadline=in_days(clock, 2)
    )
    assert (await svc.cancel_task(session, task.id, manager)).status == TaskStatus.CANCELLED


# =====================================================================================================
# Сдача результата и проверка
# =====================================================================================================


async def test_full_lifecycle(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee)
    await svc.accept_task(session, task.id, employee)

    clock.advance(days=1)
    files = [
        AttachmentIn(AttachmentKind.DOCUMENT, "file-1", "u1", "Analysis.xlsx", "application/vnd.ms-excel", 1024),
        AttachmentIn(AttachmentKind.PHOTO, "file-2"),
    ]
    sub = await svc.submit_result(
        session, task.id, employee, fact_text="  Проверено 110 договоров ", result_text="Отчёт",
        fact_value=110, attachments=files,
    )
    assert sub.attempt == 1
    assert sub.fact_text == "Проверено 110 договоров"
    assert sub.is_late is False and sub.late_days == 0
    assert sub.deadline_at_submit == task.deadline
    assert sub.created_at == clock.now
    assert [a.file_name for a in sub.attachments] == ["Analysis.xlsx", None]
    assert task.status == TaskStatus.SUBMITTED and task.submitted_at == clock.now
    assert await svc.list_for_review(session) == [task]

    with pytest.raises(DomainError):
        await svc.submit_result(session, task.id, employee, fact_text="Ещё раз")  # уже на проверке
    with pytest.raises(DomainError):
        await svc.review_confirm(session, sub.id, manager)  # оценки AI ещё нет

    await svc.record_evaluation(session, sub.id, score=110, rationale="План 100, факт 110", source="ai", model="m")
    assert sub.ai_score == 110 and task.ai_score == 110
    assert sub.ai_source == "ai" and sub.ai_model == "m"

    with pytest.raises(DomainError):
        await svc.review_confirm(session, sub.id, employee)  # сотрудник не проверяет

    clock.advance(hours=2)
    done = await svc.review_confirm(session, sub.id, manager)
    assert done.status == TaskStatus.DONE
    assert done.final_score == 110 and sub.final_score == 110
    assert sub.decision == ReviewDecision.APPROVED
    assert sub.reviewer_id == manager.id and sub.reviewed_at == clock.now
    assert done.completed_at == clock.now

    events = await svc.task_events(session, task.id)
    assert [e.type for e in events] == [
        EventType.CREATED,
        EventType.ACCEPTED,
        EventType.SUBMITTED,
        EventType.AI_EVALUATED,
        EventType.SCORE_CONFIRMED,
    ]
    ai_event = events[3]
    assert ai_event.actor_id is None and ai_event.data["score"] == 110
    assert events[2].data["files"] == 2
    for event in events:
        json.dumps(event.data)

    with pytest.raises(DomainError, match="уже обработан"):
        await svc.review_confirm(session, sub.id, manager)
    with pytest.raises(DomainError):
        await svc.review_set_score(session, sub.id, manager, 100)
    with pytest.raises(DomainError):
        await svc.cancel_task(session, task.id, manager)  # выполненную не отменяют


async def test_submit_permissions_and_status(session, clock, manager: User, employee: User, employee2: User) -> None:
    task = await new_task(session, clock, manager, employee)
    with pytest.raises(DomainError):
        await svc.submit_result(session, task.id, employee2, fact_text="Чужая задача")
    with pytest.raises(DomainError):
        await svc.submit_result(session, task.id, manager, fact_text="Начальник")
    with pytest.raises(DomainError):
        await svc.submit_result(session, task.id, employee, fact_text="   ")  # «что сделано» обязательно
    with pytest.raises(DomainError):
        await svc.submit_result(session, task.id, employee, fact_text="ok", fact_value=-1)

    proposal = await svc.propose_task(
        session, employee=employee, title="x", expected_result="y", deadline=in_days(clock, 2)
    )
    with pytest.raises(DomainError):
        await svc.submit_result(session, proposal.id, employee, fact_text="Не подтверждена")


async def test_submit_fills_accepted_at(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee)
    assert task.accepted_at is None
    await svc.submit_result(session, task.id, employee, fact_text="Готово")
    assert task.accepted_at == clock.now


async def test_late_submission(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee, deadline=in_days(clock, 1))
    clock.set(task.deadline + timedelta(days=1, hours=12))
    sub = await svc.submit_result(session, task.id, employee, fact_text="Сдал с опозданием")
    assert sub.is_late is True
    assert sub.late_days == pytest.approx(1.5)
    with pytest.raises(DomainError):
        await svc.review_confirm(session, sub.id, manager)  # без оценки AI — только ручная

    await svc.record_evaluation(session, sub.id, score=95, rationale="С опозданием", source="rules")
    done = await svc.review_set_score(session, sub.id, manager, 90, comment="Поздно")
    assert done.status == TaskStatus.DONE and done.final_score == 90
    assert sub.decision == ReviewDecision.CHANGED and sub.review_comment == "Поздно"
    last = (await svc.task_events(session, task.id))[-1]
    assert last.type == EventType.SCORE_CHANGED
    assert (last.data["ai_score"], last.data["score"], last.data["comment"]) == (95, 90, "Поздно")


@pytest.mark.parametrize(("raw", "expected"), [(180, 150), (-5, 0), (109.5, 110), (99.4, 99), (150, 150)])
async def test_record_evaluation_clamps_and_rounds(session, clock, manager, employee, raw, expected) -> None:
    task = await new_task(session, clock, manager, employee)
    sub = await svc.submit_result(session, task.id, employee, fact_text="Готово")
    await svc.record_evaluation(session, sub.id, score=raw, rationale="r", source="ai")
    assert sub.ai_score == expected
    assert task.ai_score == expected


@pytest.mark.parametrize("score", [-1, 151, 1000])
async def test_review_set_score_bounds(session, clock, manager: User, employee: User, score: float) -> None:
    task = await new_task(session, clock, manager, employee)
    sub = await svc.submit_result(session, task.id, employee, fact_text="Готово")
    with pytest.raises(DomainError):
        await svc.review_set_score(session, sub.id, manager, score)
    assert task.status == TaskStatus.SUBMITTED


async def test_review_set_score_edges(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee)
    sub = await svc.submit_result(session, task.id, employee, fact_text="Готово")
    done = await svc.review_set_score(session, sub.id, manager, 150)
    assert done.final_score == 150 and sub.decision == ReviewDecision.CHANGED


async def test_rework_then_resubmit(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee)
    first = await svc.submit_result(session, task.id, employee, fact_text="Первая попытка", fact_value=60)
    await svc.record_evaluation(session, first.id, score=60, rationale="60 %", source="rules")
    session.add(ReminderLog(task_id=task.id, kind="before_1d"))
    await session.flush()

    with pytest.raises(DomainError):
        await svc.review_rework(session, first.id, manager, "   ")  # комментарий обязателен
    with pytest.raises(DomainError):
        await svc.review_rework(session, first.id, manager, "Доделать", clock.now - timedelta(days=1))
    with pytest.raises(DomainError):
        await svc.review_rework(session, first.id, employee, "Доделать")

    new_deadline = in_days(clock, 7)
    reworked = await svc.review_rework(session, first.id, manager, "Доделать 40 договоров", new_deadline)
    assert reworked.status == TaskStatus.REWORK
    assert reworked.rework_count == 1
    assert reworked.deadline == new_deadline
    assert first.decision == ReviewDecision.REWORK and first.review_comment == "Доделать 40 договоров"
    assert first.reviewer_id == manager.id
    assert await reminder_count(session, task.id) == 0
    rework_event = (await svc.task_events(session, task.id))[-1]
    assert rework_event.type == EventType.REWORK
    assert rework_event.data["comment"] == "Доделать 40 договоров"
    assert rework_event.data["new_deadline"] == new_deadline.isoformat()

    # REWORK ведёт себя как ACTIVE: можно сдать снова — это попытка 2.
    with pytest.raises(DomainError):
        await svc.review_confirm(session, first.id, manager)
    clock.advance(days=1)
    second = await svc.submit_result(session, task.id, employee, fact_text="Вторая попытка", fact_value=100)
    assert second.attempt == 2
    assert task.status == TaskStatus.SUBMITTED
    assert task.last_submission is second
    assert task.ai_score is None  # оценка первой попытки к новой не относится

    # Старую (не последнюю) сдачу проверять нельзя; её переоценка не трогает задачу.
    with pytest.raises(DomainError, match="уже обработан"):
        await svc.review_set_score(session, first.id, manager, 100)
    await svc.record_evaluation(session, first.id, score=10, rationale="старая", source="rules")
    assert task.ai_score is None

    await svc.record_evaluation(session, second.id, score=100, rationale="100 %", source="rules")
    done = await svc.review_confirm(session, second.id, manager)
    assert done.status == TaskStatus.DONE and done.final_score == 100
    assert await svc.evaluated_history(session, employee.id) == [task]


async def test_rework_without_new_deadline_keeps_deadline(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee)
    deadline = task.deadline
    sub = await svc.submit_result(session, task.id, employee, fact_text="Готово")
    await svc.review_rework(session, sub.id, manager, "Добавьте отчёт")
    assert task.deadline == deadline and task.status == TaskStatus.REWORK


async def test_relationships_load_in_fresh_session(sessionmaker, clock) -> None:
    async with sessionmaker() as s:
        manager, _ = await users.register_or_get(s, 1001, "boss", "Петров Пётр")
        employee = User(tg_id=2001, full_name="Иванов Иван", role=Role.EMPLOYEE, status=UserStatus.ACTIVE)
        s.add(employee)
        await s.flush()
        task = await new_task(s, clock, manager, employee)
        sub = await svc.submit_result(
            s, task.id, employee, fact_text="Готово", attachments=[AttachmentIn(AttachmentKind.DOCUMENT, "f1")]
        )
        task_id, sub_id = task.id, sub.id
        await s.commit()

    async with sessionmaker() as s:
        loaded = await svc.get_task(s, task_id)
        assert loaded is not None
        assert loaded.assignee.full_name == "Иванов Иван"
        assert loaded.submissions[-1].attachments[0].file_id == "f1"
        fresh_sub = await svc.get_submission(s, sub_id)
        assert fresh_sub is not None and fresh_sub.task.last_submission is fresh_sub
        assert await svc.get_task(s, 999_999) is None
        assert await svc.get_submission(s, 999_999) is None


# =====================================================================================================
# Списки, загрузка недели, история
# =====================================================================================================


async def test_list_tasks_and_counts(session, clock, manager: User, employee: User, employee2: User, make_task):
    far = await make_task(employee, deadline=in_days(clock, 10), title="Далеко")
    near = await make_task(employee, deadline=in_days(clock, 1), title="Близко")
    overdue = await make_task(employee, deadline=in_days(clock, -1), title="Просрочена")
    done_old = await make_task(employee, deadline=in_days(clock, -5), status=TaskStatus.DONE, final_score=100,
                               submitted_at=in_days(clock, -6))
    done_new = await make_task(employee, deadline=in_days(clock, -3), status=TaskStatus.DONE, final_score=90,
                               submitted_at=in_days(clock, -4))
    other = await make_task(employee2, deadline=in_days(clock, 2))

    mine = await svc.list_tasks(session, assignee_id=employee.id)
    assert mine == [overdue, near, far, done_new, done_old]  # открытые по сроку, затем DONE по completed_at desc
    assert await svc.count_tasks(session, assignee_id=employee.id) == 5
    assert await svc.list_tasks(session, overdue_only=True) == [overdue]
    assert await svc.count_tasks(session, overdue_only=True) == 1
    assert await svc.list_tasks(session, statuses=[TaskStatus.DONE]) == [done_new, done_old]
    assert await svc.list_tasks(session, statuses=[]) == []
    assert await svc.list_tasks(session, assignee_id=employee.id, limit=2, offset=1) == [near, far]
    assert other in await svc.list_tasks(session)

    assert svc.is_overdue(overdue, clock.now) is True
    assert svc.is_overdue(near, clock.now) is False
    assert svc.is_overdue(done_old, clock.now) is False  # закрытая задача не просрочена
    assert svc.is_overdue(overdue) is True  # без now — текущее время (clock)


async def test_weight_load(session, clock, employee: User, employee2: User, make_task) -> None:
    def local(day: int, hour: int, minute: int = 0) -> datetime:
        return to_utc(datetime(2026, 10, day, hour, minute))

    # Неделя пн 05.10 – вс 11.10 по местному времени.
    a = await make_task(employee, deadline=local(5, 0, 30), weight=20)
    await make_task(employee, deadline=local(11, 23, 30), weight=30, status=TaskStatus.DONE, final_score=100)
    await make_task(employee, deadline=local(8, 18), weight=15, status=TaskStatus.SUBMITTED)
    await make_task(employee, deadline=local(8, 18), weight=40, status=TaskStatus.CANCELLED)
    await make_task(employee, deadline=local(8, 18), weight=40, status=TaskStatus.PROPOSED)
    await make_task(employee, deadline=local(8, 18), weight=40, status=TaskStatus.REJECTED)
    await make_task(employee, deadline=local(4, 23, 59), weight=50)   # прошлая неделя (вс)
    await make_task(employee, deadline=local(12, 0, 0), weight=50)    # следующая неделя (пн)
    await make_task(employee2, deadline=local(7, 18), weight=70)      # другой сотрудник

    probe = local(7, 18)
    assert await svc.weight_load(session, employee.id, probe) == 65
    assert await svc.weight_load(session, employee.id, probe, exclude_task_id=a.id) == 45
    assert await svc.weight_load(session, employee.id, local(12, 10)) == 50
    assert await svc.weight_load(session, employee2.id, probe) == 70
    aware = probe.replace(tzinfo=UTC)
    assert await svc.weight_load(session, employee.id, aware) == 65


async def test_evaluated_history(session, clock, employee: User, employee2: User, make_task) -> None:
    tasks = []
    for days_ago in (9, 3, 6, 1):
        tasks.append(
            await make_task(employee, deadline=in_days(clock, -days_ago - 1), status=TaskStatus.DONE,
                            final_score=100, submitted_at=in_days(clock, -days_ago))
        )
    await make_task(employee, deadline=in_days(clock, 2))                      # не оценена
    await make_task(employee2, deadline=in_days(clock, -2), status=TaskStatus.DONE, final_score=50)

    newest_first = sorted(tasks, key=lambda t: t.completed_at, reverse=True)
    assert await svc.evaluated_history(session, employee.id) == newest_first
    assert await svc.evaluated_history(session, employee.id, limit=2) == newest_first[:2]
    assert await svc.evaluated_history(session, employee.id, limit=2, offset=2) == newest_first[2:]
    assert await svc.evaluated_history(session, 999_999) == []


async def test_events_are_ordered_and_have_actor(session, clock, manager: User, employee: User) -> None:
    task = await new_task(session, clock, manager, employee)
    await svc.accept_task(session, task.id, employee)
    events = await svc.task_events(session, task.id)
    assert [e.actor_id for e in events] == [manager.id, employee.id]
    assert all(isinstance(e, TaskEvent) for e in events)
    stored = await session.scalar(select(func.count()).select_from(Submission))
    assert stored == 0
