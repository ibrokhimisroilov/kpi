"""Жизненный цикл задачи: постановка, предложение сотрудника, правка, сдача результата, проверка.

Схема статусов — SPEC.md, раздел 2. Каждая мутация пишет TaskEvent (add_event),
делает flush, но не commit. Все связи моделей загружаются сразу (люди — JOIN, списки сдач и
файлов — selectin, см. bot.db.models); у объектов, созданных здесь, связи заполняются сразу при
создании, поэтому task.assignee, task.manager, task.submissions, sub.task и sub.attachments
доступны без дополнительных запросов.

Обмены с базой. Бот в облаке ходит в базу в другом регионе: каждый запрос — 130–190 мс. Поэтому
поля, которые меняет переход статуса, пишутся тем же условным UPDATE, что и сам переход
(``_claim_status(values=...)``, с RETURNING), а не отдельным UPDATE при flush; объекты, уже
загруженные в сессию, повторно не читаются.

Гонки. Апдейты разных пользователей бот обрабатывает параллельно, каждый — в своей сессии БД,
поэтому два начальника могут одновременно решать по одной сдаче или одному предложению.
Смена статуса (решение по предложению, проверка, отмена, сдача результата) делается атомарным
условным UPDATE — ``UPDATE tasks SET status=… WHERE id=? AND status=<ожидаемый>`` (см.
``_claim_status``): пройдёт только у первого, второй получит понятный DomainError
(«Результат уже обработан», «Предложение уже обработано»). Все проверки входных данных
выполняются ДО этого UPDATE — после него сервис не бросает DomainError, иначе хендлер,
поймавший ошибку и закоммитивший сессию, сохранил бы переход «наполовину».

На PostgreSQL второй такой UPDATE ждёт commit/rollback первого (блокировка строки) и затем
перепроверяет условие по свежей версии строки — результат тот же: rowcount == 0.

PostgreSQL строго соблюдает ограничения колонок (bot.services.dbsafe): тексты очищаются от
NUL-символов, длины полей файлов обрезаются по моделям, размер файла — в пределах INTEGER,
id из кнопок вне диапазона INTEGER дают «не найдено», отрицательные LIMIT/OFFSET работают как в SQLite,
а NULL в сортировках стоят там же, где их ставит SQLite (NULLS FIRST по возрастанию,
NULLS LAST по убыванию).
"""

from __future__ import annotations

import enum
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import ColumnElement, case, delete, false, func, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from bot.config import get_settings
from bot.db.models import (
    EXCLUDED_FROM_KPI,
    OPEN_STATUSES,
    Attachment,
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
)
from bot.services.dbsafe import (
    clamp_int,
    clean_text,
    clip,
    clip_file_name,
    column_length,
    finite_or_none,
    is_db_id,
    naive_utc,
    non_negative,
    sql_limit,
)
from bot.services.errors import DomainError
from bot.services.users import require_manager
from bot.utils.dates import days_between, to_local, to_utc, utcnow
from bot.utils.text import plural

_EDITABLE_FIELDS = frozenset(
    {"title", "expected_result", "description", "plan_value", "plan_unit", "deadline", "priority", "weight"}
)
_EDITABLE_STATUSES = (TaskStatus.PROPOSED, TaskStatus.ACTIVE, TaskStatus.REWORK)
_CANCELLABLE_STATUSES = (TaskStatus.PROPOSED, TaskStatus.ACTIVE, TaskStatus.REWORK, TaskStatus.SUBMITTED)
_PROPOSAL_WEIGHT = 10  # временный вес предложения; окончательный задаёт начальник
# Срок дальше — опечатка в годе (и даты у предела datetime ломают расчёт недель): не принимаем.
_MAX_DEADLINE_AHEAD = timedelta(days=5 * 366)
_MAX_TITLE_LEN = 255
_MAX_UNIT_LEN = 64
_MAX_MODEL_LEN = column_length(Submission.ai_model)
_MAX_FILE_ID_LEN = column_length(Attachment.file_id)
_MAX_FILE_UNIQUE_ID_LEN = column_length(Attachment.file_unique_id)
_MAX_FILE_NAME_LEN = column_length(Attachment.file_name)
_MAX_MIME_LEN = column_length(Attachment.mime_type)
_EVAL_SOURCES = ("ai", "rules")

# Тексты отказов, когда решение уже принято (в т.ч. другим начальником секундой раньше).
REVIEW_ALREADY_PROCESSED = "Результат уже обработан"
PROPOSAL_ALREADY_PROCESSED = "Предложение уже обработано"
AUTO_NOT_ALLOWED = "Эту оценку подтверждает только начальник"
REVISE_CLOSED = "Изменить можно только автоматически подтверждённую оценку и только в течение {days} после подтверждения"
_NOT_CANCELLABLE = "Отменить можно только незавершённую задачу"
_ALREADY_SUBMITTED = "Результат уже отправлен и ждёт проверки"
_NOT_OPEN_FOR_SUBMIT = "Задача не в работе — сдать результат нельзя"
_CHANGED_MEANWHILE = "Задача только что изменилась — откройте её заново и повторите действие"

logger = logging.getLogger(__name__)


@dataclass
class AttachmentIn:
    """Файл-подтверждение из Telegram, приложенный к сдаче результата."""

    kind: AttachmentKind
    file_id: str
    file_unique_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


# --- Журнал и чтение ------------------------------------------------------------------------


async def add_event(
    session: AsyncSession,
    task: Task,
    actor: User | None,
    type: EventType,  # noqa: A002 - имя из контракта
    **data: Any,
) -> TaskEvent:
    """Записать событие в журнал задачи. data приводится к JSON (даты — ISO, enum — значения)."""
    event = TaskEvent(task_id=task.id, actor=actor, type=type, data=_jsonable(data))
    session.add(event)
    await session.flush()
    return event


def is_overdue(task: Task, now: datetime | None = None) -> bool:
    """Задача в работе и срок уже прошёл."""
    return task.is_open and task.deadline < (now if now is not None else utcnow())


async def get_task(session: AsyncSession, task_id: int) -> Task | None:
    if not is_db_id(task_id):  # подделанная кнопка: id не поместится в INTEGER
        return None
    task = await session.get(Task, task_id)
    if task is not None:
        await _ensure_submissions_loaded(session, task)
    return task


async def get_submission(session: AsyncSession, sub_id: int) -> Submission | None:
    """Сдача вместе с задачей; у sub.task гарантированно загружен список сдач.

    Сдачи в сессии ещё нет — грузится её задача (``_task_of_submission``): задача с людьми, все её
    сдачи с файлами — 3 запроса, и сдача приходит уже в списке task.submissions. Загрузка самой
    сдачи (sub.task — JOIN) потребовала бы ещё и отдельной догрузки списка сдач задачи.

    Сдача могла попасть в сессию через task.submissions, а сама задача — уже уйти из памяти:
    тогда sub.task догружается явно, без ленивой загрузки (в async она падает с MissingGreenlet).
    """
    if not is_db_id(sub_id):
        return None
    sub = _in_session(session, Submission, sub_id)
    if sub is None:
        task = await _task_of_submission(session, sub_id)
        if task is None:
            return None
        # Обычно сдача уже пришла в task.submissions; нет — список сдач задачи в сессии устарел.
        sub = _in_session(session, Submission, sub_id) or await session.get(Submission, sub_id)
        if sub is None:
            return None
    if "task" in inspect(sub).unloaded:
        task = _in_session(session, Task, sub.task_id)
        if task is not None:
            set_committed_value(sub, "task", task)  # задача уже в сессии — без запроса
        else:
            await session.refresh(sub, attribute_names=["task"])
    await _ensure_submissions_loaded(session, sub.task)
    return sub


# --- Постановка и предложение ---------------------------------------------------------------


async def create_task(
    session: AsyncSession,
    *,
    creator: User,
    assignee_id: int,
    title: str,
    expected_result: str,
    deadline: datetime,
    weight: int,
    priority: Priority = Priority.MEDIUM,
    description: str | None = None,
    plan_value: float | None = None,
    plan_unit: str | None = None,
) -> Task:
    """Начальник ставит задачу активному сотруднику. Статус ACTIVE, событие CREATED."""
    require_manager(creator)
    assignee = await session.get(User, assignee_id) if is_db_id(assignee_id) else None
    if assignee is None or not _is_active_employee(assignee):
        raise DomainError("Исполнителем может быть только активный сотрудник")
    task = await _add_task(
        session,
        title=_normalize_field("title", title),
        expected_result=_normalize_field("expected_result", expected_result),
        description=_normalize_field("description", description),
        plan_value=_normalize_field("plan_value", plan_value),
        plan_unit=_normalize_field("plan_unit", plan_unit),
        deadline=_normalize_field("deadline", deadline),
        weight=_normalize_field("weight", weight),
        priority=_normalize_field("priority", priority),
        status=TaskStatus.ACTIVE,
        source=TaskSource.MANAGER,
        assignee=assignee,
        created_by=creator,
        manager=creator,
    )
    await add_event(
        session,
        task,
        creator,
        EventType.CREATED,
        assignee_id=assignee.id,
        deadline=task.deadline,
        weight=task.weight,
        priority=task.priority,
    )
    return task


async def propose_task(
    session: AsyncSession,
    *,
    employee: User,
    title: str,
    expected_result: str,
    deadline: datetime,
    description: str | None = None,
    plan_value: float | None = None,
    plan_unit: str | None = None,
) -> Task:
    """Сотрудник вносит устное поручение. Статус PROPOSED, вес временный, событие PROPOSED."""
    if employee is None or not _is_active_employee(employee):
        raise DomainError("Вносить поручения могут только активные сотрудники")
    task = await _add_task(
        session,
        title=_normalize_field("title", title),
        expected_result=_normalize_field("expected_result", expected_result),
        description=_normalize_field("description", description),
        plan_value=_normalize_field("plan_value", plan_value),
        plan_unit=_normalize_field("plan_unit", plan_unit),
        deadline=_normalize_field("deadline", deadline),
        weight=_PROPOSAL_WEIGHT,
        priority=Priority.MEDIUM,
        status=TaskStatus.PROPOSED,
        source=TaskSource.EMPLOYEE,
        assignee=employee,
        created_by=employee,
        manager=None,
    )
    await add_event(session, task, employee, EventType.PROPOSED, deadline=task.deadline)
    return task


async def approve_proposal(
    session: AsyncSession,
    task_id: int,
    manager: User,
    *,
    weight: int,
    priority: Priority = Priority.MEDIUM,
) -> Task:
    """PROPOSED -> ACTIVE. Задачу внёс сам сотрудник, поэтому она сразу считается принятой."""
    require_manager(manager)
    task = await _task_or_error(session, task_id)
    if task.status != TaskStatus.PROPOSED:
        raise DomainError(PROPOSAL_ALREADY_PROCESSED)
    weight = _normalize_field("weight", weight)
    priority = _normalize_field("priority", priority)
    if not _is_active_employee(task.assignee):
        raise DomainError("Сотрудник неактивен — подтвердить поручение нельзя")
    now = utcnow()
    if task.deadline <= now:
        raise DomainError("Срок поручения уже прошёл — сначала измените срок")

    approved = {
        "weight": weight,
        "priority": priority,
        "manager_id": manager.id,
        "approved_at": now,
        "accepted_at": now,  # сотрудник внёс поручение сам — оно уже принято
    }
    if not await _claim_status(session, task, TaskStatus.PROPOSED, TaskStatus.ACTIVE, values=approved):
        raise DomainError(PROPOSAL_ALREADY_PROCESSED)
    set_committed_value(task, "manager", manager)
    await add_event(session, task, manager, EventType.APPROVED, weight=weight, priority=priority)
    return task


async def set_proposal_weight(
    session: AsyncSession, task_id: int, *, weight: int, source: str, note: str | None = None
) -> Task:
    """Записать предложенный вес поручения (AI или по умолчанию): временный вес задачи + событие
    WEIGHT_SUGGESTED. Начальник видит его как подсказку; с ним поручение принимается автоматически.
    Поручение уже не ждёт решения — DomainError."""
    if source not in _EVAL_SOURCES:
        raise ValueError(f"set_proposal_weight: source должен быть одним из {_EVAL_SOURCES}")
    task = await _task_or_error(session, task_id)
    weight = _normalize_field("weight", weight)
    note = _optional_text(note)
    if not await _claim_status(session, task, TaskStatus.PROPOSED, TaskStatus.PROPOSED, values={"weight": weight}):
        raise DomainError(PROPOSAL_ALREADY_PROCESSED)
    await add_event(session, task, None, EventType.WEIGHT_SUGGESTED, weight=weight, source=source, note=note)
    return task


async def suggested_weight(session: AsyncSession, task_id: int) -> tuple[int, str] | None:
    """Последний предложенный вес поручения: (вес, "ai" | "rules"); None — подсказки ещё не было."""
    stmt = (
        select(TaskEvent.data)
        .where(TaskEvent.task_id == task_id, TaskEvent.type == EventType.WEIGHT_SUGGESTED)
        .order_by(TaskEvent.id.desc())
        .limit(1)
    )
    data = await session.scalar(stmt)
    if not isinstance(data, dict) or isinstance(data.get("weight"), bool) or not isinstance(data.get("weight"), int):
        return None
    return data["weight"], str(data.get("source") or "rules")


async def auto_approve_proposal(session: AsyncSession, task_id: int) -> Task:
    """PROPOSED -> ACTIVE без начальника (он не ответил вовремя — bot.services.auto): вес — предложенный
    (временный вес поручения), приоритет средний. Ответственного начальника у задачи нет: результат
    получат все начальники."""
    task = await _task_or_error(session, task_id)
    if task.status != TaskStatus.PROPOSED:
        raise DomainError(PROPOSAL_ALREADY_PROCESSED)
    if not _is_active_employee(task.assignee):
        raise DomainError("Сотрудник неактивен — подтвердить поручение нельзя")
    now = utcnow()
    if task.deadline <= now:
        raise DomainError("Срок поручения уже прошёл — сначала измените срок")
    weight = _normalize_field("weight", task.weight)
    approved = {"weight": weight, "priority": Priority.MEDIUM, "approved_at": now, "accepted_at": now}
    if not await _claim_status(session, task, TaskStatus.PROPOSED, TaskStatus.ACTIVE, values=approved):
        raise DomainError(PROPOSAL_ALREADY_PROCESSED)
    await add_event(session, task, None, EventType.APPROVED, weight=weight, priority=Priority.MEDIUM, auto=True)
    return task


async def reject_proposal(
    session: AsyncSession, task_id: int, manager: User, reason: str | None = None
) -> Task:
    """PROPOSED -> REJECTED, событие REJECTED(reason)."""
    require_manager(manager)
    task = await _task_or_error(session, task_id)
    if task.status != TaskStatus.PROPOSED:
        raise DomainError(PROPOSAL_ALREADY_PROCESSED)
    reason = _optional_text(reason)
    if not await _claim_status(session, task, TaskStatus.PROPOSED, TaskStatus.REJECTED):
        raise DomainError(PROPOSAL_ALREADY_PROCESSED)
    await add_event(session, task, manager, EventType.REJECTED, reason=reason)
    return task


# --- Правка, принятие, отмена ---------------------------------------------------------------


async def update_task(
    session: AsyncSession, task_id: int, actor: User, **fields: Any
) -> tuple[Task, dict[str, tuple]]:
    """Изменить поля задачи (PROPOSED/ACTIVE/REWORK). Возвращает (task, {поле: (было, стало)}).

    В changes попадают только реально изменённые поля; без изменений событие не пишется.
    Новый срок должен быть в будущем; смена срока сбрасывает отправленные напоминания.
    """
    unknown = set(fields) - _EDITABLE_FIELDS
    if unknown:
        raise TypeError(f"update_task: недопустимые поля {sorted(unknown)}")
    require_manager(actor)
    task = await _task_or_error(session, task_id)
    if task.status not in _EDITABLE_STATUSES:
        raise DomainError("Изменить можно только задачу на подтверждении, в работе или на доработке")

    changes: dict[str, tuple] = {}
    for name, raw in fields.items():
        old = getattr(task, name)
        if name == "deadline" and isinstance(raw, datetime) and naive_utc(raw) == old:
            continue  # срок не меняется — проверка «в будущем» не нужна
        new = _normalize_field(name, raw)
        if new != old:
            changes[name] = (old, new)
    if not changes:
        return task, changes

    for name, (_, new) in changes.items():
        setattr(task, name, new)
    if "deadline" in changes:
        await _reset_reminders(session, task.id)
    await add_event(
        session,
        task,
        actor,
        EventType.EDITED,
        changes={name: [old, new] for name, (old, new) in changes.items()},
    )
    return task, changes


async def accept_task(session: AsyncSession, task_id: int, employee: User) -> Task:
    """Исполнитель подтверждает получение задачи. Повторный вызов ничего не меняет.

    Принять можно и из чата, и из приложения — одновременно: отметка пишется условным UPDATE
    (``accepted_at IS NULL`` и статус тот же), поэтому событие ACCEPTED пишет только первый.
    """
    task = await _task_or_error(session, task_id)
    _ensure_assignee(task, employee, "Принять задачу может только её исполнитель")
    if task.accepted_at is not None:
        return task
    if not task.is_open:
        raise DomainError("Принять можно только задачу в работе")
    accepted = {"accepted_at": utcnow()}
    if not await _claim_status(session, task, task.status, task.status, Task.accepted_at.is_(None), values=accepted):
        # Уже приняли (параллельный запрос) или статус сменился — задача перечитана из базы.
        if task.accepted_at is not None:
            return task
        if not task.is_open:
            raise DomainError("Принять можно только задачу в работе")
        raise DomainError(_CHANGED_MEANWHILE)
    await add_event(session, task, employee, EventType.ACCEPTED)
    return task


async def cancel_task(
    session: AsyncSession, task_id: int, manager: User, reason: str | None = None
) -> Task:
    """PROPOSED/ACTIVE/REWORK/SUBMITTED -> CANCELLED, событие CANCELLED(reason).

    Если статус задачи успел измениться (другой начальник отменил или проверил её, сотрудник
    сдал результат), отмена не выполняется — DomainError с объяснением.
    """
    require_manager(manager)
    task = await _task_or_error(session, task_id)
    _ensure_cancellable(task)
    reason = _optional_text(reason)
    previous = task.status
    if not await _claim_status(session, task, previous, TaskStatus.CANCELLED):
        _ensure_cancellable(task)  # задача уже свежая: отменена/выполнена — объяснить почему
        raise DomainError(_CHANGED_MEANWHILE)
    await add_event(session, task, manager, EventType.CANCELLED, reason=reason, previous_status=previous)
    return task


# --- Сдача результата и оценка --------------------------------------------------------------


async def submit_result(
    session: AsyncSession,
    task_id: int,
    employee: User,
    *,
    fact_text: str,
    result_text: str | None = None,
    fact_value: float | None = None,
    attachments: Sequence[AttachmentIn] = (),
) -> Submission:
    """Исполнитель сдаёт результат (ACTIVE/REWORK -> SUBMITTED). Просрочка — по текущему сроку задачи."""
    task = await _task_or_error(session, task_id)
    _ensure_assignee(task, employee, "Сдать результат может только исполнитель задачи")
    _ensure_submittable(task)
    fact = _required_text(fact_text, "Что фактически сделано")
    result = _optional_text(result_text)
    value = _fact_value(fact_value)
    items = [att for att in map(_attachment, attachments) if att is not None]

    # Начальник мог отменить задачу, пока сотрудник заполнял ответы: сдача не должна её «оживить».
    # Поля сдачи у задачи пишет тот же UPDATE; срок мог измениться в другой сессии уже после чтения
    # задачи — RETURNING возвращает актуальный, просрочку считаем по нему.
    now = utcnow()
    submitted = {
        "submitted_at": now,
        "ai_score": None,  # оценка прошлой попытки к новой сдаче не относится
        "accepted_at": func.coalesce(Task.accepted_at, now),  # не принята явно — сдача и есть принятие
    }
    if not await _claim_status(
        session, task, task.status, TaskStatus.SUBMITTED, values=submitted, returning=("deadline",)
    ):
        _ensure_submittable(task)
        raise DomainError(_CHANGED_MEANWHILE)
    sub = Submission(
        task=task,
        attempt=max((s.attempt for s in task.submissions), default=0) + 1,
        fact_text=fact,
        result_text=result,
        fact_value=value,
        created_at=now,
        deadline_at_submit=task.deadline,
        is_late=now > task.deadline,
        late_days=_late_days(now, task.deadline),
        reviewer=None,
        attachments=items,
    )
    session.add(sub)
    await session.flush()
    await add_event(
        session,
        task,
        employee,
        EventType.SUBMITTED,
        submission_id=sub.id,
        attempt=sub.attempt,
        is_late=sub.is_late,
        late_days=sub.late_days,
        files=len(sub.attachments),
    )
    return sub


async def record_evaluation(
    session: AsyncSession,
    sub_id: int,
    *,
    score: float,
    rationale: str,
    source: str,
    model: str | None = None,
) -> Submission:
    """Сохранить предварительную оценку AI/правил: ограничить [0, max_score] и округлить до целого."""
    if source not in _EVAL_SOURCES:
        raise ValueError(f"record_evaluation: source должен быть одним из {_EVAL_SOURCES}")
    if math.isnan(score):
        raise ValueError("record_evaluation: score не может быть NaN")
    sub = await _submission_or_error(session, sub_id)
    value = _round_half_up(min(max(float(score), 0.0), float(get_settings().max_score)))

    model = clip(model, _MAX_MODEL_LEN) or None
    sub.ai_score = value
    sub.ai_rationale = clean_text(rationale or "").strip()
    sub.ai_source = source
    sub.ai_model = model
    task = sub.task
    if task.last_submission is sub:
        task.ai_score = value
    await add_event(
        session,
        task,
        None,
        EventType.AI_EVALUATED,
        submission_id=sub.id,
        attempt=sub.attempt,
        score=value,
        source=source,
        model=model,
    )
    return sub


async def review_confirm(session: AsyncSession, sub_id: int, manager: User) -> Task:
    """Начальник подтверждает оценку AI: задача -> DONE с final_score = sub.ai_score."""
    task, sub = await _reviewable(session, sub_id, manager)
    if sub.ai_score is None:
        raise DomainError("Предварительной оценки нет — введите оценку вручную")
    score = sub.ai_score
    now = utcnow()
    await _claim_review(session, task, sub, TaskStatus.DONE, values={"final_score": score, "completed_at": now})
    _complete(sub, manager, score, ReviewDecision.APPROVED, comment=None, now=now)
    await add_event(session, task, manager, EventType.SCORE_CONFIRMED, submission_id=sub.id, score=score)
    return task


async def review_auto_confirm(session: AsyncSession, sub_id: int) -> Task:
    """Оценка AI подтверждается без начальника (он не ответил вовремя — bot.services.auto): задача -> DONE.

    Только оценка AI не выше AUTO_CONFIRM_MAX_SCORE: расчёт по правилам и оценку выше подтверждает
    начальник (DomainError AUTO_NOT_ALLOWED). Проверяющего у сдачи нет — признак Submission.auto_confirmed.
    """
    sub = await _submission_or_error(session, sub_id)
    task = sub.task
    if task.status != TaskStatus.SUBMITTED or task.last_submission is not sub or sub.decision is not None:
        raise DomainError(REVIEW_ALREADY_PROCESSED)
    score = sub.ai_score
    if score is None or sub.ai_source != "ai" or score > get_settings().auto_confirm_max_score:
        raise DomainError(AUTO_NOT_ALLOWED)
    now = utcnow()
    await _claim_review(session, task, sub, TaskStatus.DONE, values={"final_score": score, "completed_at": now})
    _complete(sub, None, score, ReviewDecision.APPROVED, comment=None, now=now)
    await add_event(session, task, None, EventType.SCORE_CONFIRMED, submission_id=sub.id, score=score, auto=True)
    return task


async def review_revise_auto(
    session: AsyncSession, sub_id: int, manager: User, score: float, comment: str | None = None
) -> Task:
    """Начальник меняет оценку, подтверждённую автоматически (в течение AUTO_REVISE_DAYS): задача остаётся
    DONE, итоговая оценка — новая, решение сдачи — CHANGED (дальше она как выставленная начальником).

    Занимается условным UPDATE: задача DONE, сдача последняя и всё ещё без проверяющего — из двух
    начальников оценку изменит первый.
    """
    require_manager(manager)
    sub = await _submission_or_error(session, sub_id)
    task = sub.task
    days = get_settings().auto_revise_days
    closed = DomainError(REVISE_CLOSED.format(days=plural(max(days, 0), "дня", "дней", "дней")))
    now = utcnow()
    if task.status != TaskStatus.DONE or task.last_submission is not sub or not sub.auto_confirmed:
        raise closed
    if days <= 0 or sub.reviewed_at is None or now > sub.reviewed_at + timedelta(days=days):
        raise closed
    if task.assignee_id == manager.id:
        raise DomainError("Нельзя оценивать результат собственной задачи")
    max_score = get_settings().max_score
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= max_score:
        raise DomainError(f"Оценка должна быть от 0 до {max_score} %")
    final = _round_half_up(float(score))
    comment = _optional_text(comment)
    previous = sub.final_score
    still_auto = (
        select(Submission.id)
        .where(
            Submission.id == sub.id,
            Submission.decision == ReviewDecision.APPROVED,
            Submission.reviewer_id.is_(None),
        )
        .exists()
    )
    newer_exists = select(Submission.id).where(Submission.task_id == task.id, Submission.id > sub.id).exists()
    if not await _claim_status(
        session, task, TaskStatus.DONE, TaskStatus.DONE, still_auto, ~newer_exists, values={"final_score": final}
    ):
        raise closed
    _complete(sub, manager, final, ReviewDecision.CHANGED, comment=comment, now=now)
    await add_event(
        session,
        task,
        manager,
        EventType.SCORE_CHANGED,
        submission_id=sub.id,
        ai_score=sub.ai_score,
        score=final,
        comment=comment,
        previous=previous,
        after_auto=True,
    )
    return task


async def review_set_score(
    session: AsyncSession, sub_id: int, manager: User, score: float, comment: str | None = None
) -> Task:
    """Начальник ставит свою оценку (0..max_score): задача -> DONE, решение CHANGED.

    Оценка округляется до целого (половина — вверх), как предварительная: везде показываются целые
    проценты, и KPI должен сходиться с тем, что видно (одно правило для чата и приложения).
    """
    task, sub = await _reviewable(session, sub_id, manager)
    max_score = get_settings().max_score
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= max_score:
        raise DomainError(f"Оценка должна быть от 0 до {max_score} %")
    final = _round_half_up(float(score))
    comment = _optional_text(comment)
    now = utcnow()
    await _claim_review(session, task, sub, TaskStatus.DONE, values={"final_score": final, "completed_at": now})
    _complete(sub, manager, final, ReviewDecision.CHANGED, comment=comment, now=now)
    await add_event(
        session,
        task,
        manager,
        EventType.SCORE_CHANGED,
        submission_id=sub.id,
        ai_score=sub.ai_score,
        score=final,
        comment=comment,
    )
    return task


async def review_rework(
    session: AsyncSession,
    sub_id: int,
    manager: User,
    comment: str,
    new_deadline: datetime | None = None,
) -> Task:
    """Вернуть на доработку: SUBMITTED -> REWORK; при новом сроке — сбросить напоминания."""
    task, sub = await _reviewable(session, sub_id, manager)
    comment = _required_text(comment, "Что нужно доработать")
    deadline = _future_deadline(new_deadline) if new_deadline is not None else None

    old_deadline = task.deadline
    changes: dict[str, Any] = {"rework_count": Task.rework_count + 1}
    if deadline is not None and deadline != old_deadline:
        changes["deadline"] = deadline
    await _claim_review(session, task, sub, TaskStatus.REWORK, values=changes)
    sub.decision = ReviewDecision.REWORK
    sub.review_comment = comment
    sub.reviewer = manager
    sub.reviewed_at = utcnow()
    if "deadline" in changes:
        await _reset_reminders(session, task.id)
    await add_event(
        session,
        task,
        manager,
        EventType.REWORK,
        submission_id=sub.id,
        comment=comment,
        new_deadline=deadline,
        old_deadline=old_deadline if deadline is not None else None,
    )
    return task


# --- Списки ---------------------------------------------------------------------------------


async def list_tasks(
    session: AsyncSession,
    *,
    assignee_id: int | None = None,
    statuses: Sequence[TaskStatus] | None = None,
    overdue_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[Task]:
    """Задачи по фильтрам: сначала незавершённые по сроку (ближайшие сверху), затем DONE по completed_at desc."""
    is_done = Task.status == TaskStatus.DONE
    stmt = (
        select(Task)
        .where(*_task_filters(assignee_id, statuses, overdue_only))
        .order_by(
            case((is_done, 1), else_=0),
            case((~is_done, Task.deadline)).nulls_first(),
            Task.completed_at.desc().nulls_last(),
            Task.id,
        )
        .offset(non_negative(offset))
    )
    if limit is not None:
        stmt = stmt.limit(sql_limit(limit))
    return list(await session.scalars(stmt))


async def count_tasks(
    session: AsyncSession,
    *,
    assignee_id: int | None = None,
    statuses: Sequence[TaskStatus] | None = None,
    overdue_only: bool = False,
) -> int:
    stmt = select(func.count()).select_from(Task).where(*_task_filters(assignee_id, statuses, overdue_only))
    return int(await session.scalar(stmt) or 0)


async def list_proposals(session: AsyncSession) -> list[Task]:
    """Предложения сотрудников, старые сверху."""
    stmt = select(Task).where(Task.status == TaskStatus.PROPOSED).order_by(Task.created_at, Task.id)
    return list(await session.scalars(stmt))


async def list_for_review(session: AsyncSession) -> list[Task]:
    """Задачи на проверке, по времени сдачи (давние сверху)."""
    stmt = (
        select(Task)
        .where(Task.status == TaskStatus.SUBMITTED)
        .order_by(Task.submitted_at.nulls_first(), Task.id)
    )
    return list(await session.scalars(stmt))


async def task_events(session: AsyncSession, task_id: int) -> list[TaskEvent]:
    stmt = select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.created_at, TaskEvent.id)
    return list(await session.scalars(stmt))


async def weight_load(
    session: AsyncSession, assignee_id: int, deadline: datetime, exclude_task_id: int | None = None
) -> int:
    """Сумма весов задач сотрудника со сроком в той же местной неделе (пн–вс), что и deadline."""
    start, end = _local_week_bounds(naive_utc(deadline))
    stmt = select(func.coalesce(func.sum(Task.weight), 0)).where(
        Task.assignee_id == assignee_id,
        Task.status.not_in(EXCLUDED_FROM_KPI),
        Task.deadline >= start,
        Task.deadline < end,
    )
    if exclude_task_id is not None:
        stmt = stmt.where(Task.id != exclude_task_id)
    return int(await session.scalar(stmt) or 0)


async def week_tasks(
    session: AsyncSession, assignee_id: int, deadline: datetime, exclude_task_id: int | None = None
) -> list[tuple[str, int]]:
    """(название, вес) задач сотрудника со сроком в той же местной неделе, что и deadline, — те же задачи,
    что считает weight_load. Нужны AI, чтобы предложить вес нового поручения (bot.ai.weigh)."""
    start, end = _local_week_bounds(naive_utc(deadline))
    stmt = (
        select(Task.title, Task.weight)
        .where(
            Task.assignee_id == assignee_id,
            Task.status.not_in(EXCLUDED_FROM_KPI),
            Task.deadline >= start,
            Task.deadline < end,
        )
        .order_by(Task.deadline, Task.id)
    )
    if exclude_task_id is not None:
        stmt = stmt.where(Task.id != exclude_task_id)
    return [(title, int(weight)) for title, weight in await session.execute(stmt)]


async def evaluated_history(
    session: AsyncSession, assignee_id: int, *, limit: int = 10, offset: int = 0
) -> list[Task]:
    """Оценённые задачи сотрудника (DONE), последние сверху."""
    stmt = (
        select(Task)
        .where(Task.assignee_id == assignee_id, Task.status == TaskStatus.DONE)
        .order_by(Task.completed_at.desc().nulls_last(), Task.id.desc())
        .limit(sql_limit(limit))
        .offset(non_negative(offset))
    )
    return list(await session.scalars(stmt))


# --- Приватные хелперы ----------------------------------------------------------------------


async def _add_task(session: AsyncSession, **values: Any) -> Task:
    """Создать задачу с заполненными связями (без ленивых загрузок) и получить id."""
    values.setdefault("created_at", utcnow())  # те же часы, что у остальных отметок времени сервиса
    task = Task(rework_count=0, submissions=[], **values)
    session.add(task)
    await session.flush()
    return task


def _in_session[T](session: AsyncSession, cls: type[T], pk: int) -> T | None:
    """Объект, уже загруженный в сессию (identity map), — без запроса к базе; иначе None
    (в том числе для устаревшего или удалённого объекта: его, как и session.get, надо читать из базы)."""
    obj = session.sync_session.identity_map.get(session.sync_session.identity_key(cls, pk))
    if not isinstance(obj, cls):
        return None
    state = inspect(obj)
    if state.expired or state.deleted or state.was_deleted:
        return None
    return obj


async def _task_of_submission(session: AsyncSession, sub_id: int) -> Task | None:
    """Задача сдачи sub_id со всеми связями (сдачи задачи — вместе с sub_id) или None."""
    task_id = select(Submission.task_id).where(Submission.id == sub_id).scalar_subquery()
    return (await session.scalars(select(Task).where(Task.id == task_id))).first()


async def _ensure_submissions_loaded(session: AsyncSession, task: Task) -> None:
    """Догрузить task.submissions, если задача пришла через sub.task.

    Жадная загрузка не идёт по циклу Submission -> Task -> submissions, и без этого
    обращение к task.last_submission вызвало бы ленивую загрузку (MissingGreenlet).
    """
    if "submissions" in inspect(task).unloaded:
        await session.refresh(task, attribute_names=["submissions"])


async def _task_or_error(session: AsyncSession, task_id: int) -> Task:
    task = await get_task(session, task_id)
    if task is None:
        raise DomainError("Задача не найдена")
    return task


async def _submission_or_error(session: AsyncSession, sub_id: int) -> Submission:
    sub = await get_submission(session, sub_id)
    if sub is None:
        raise DomainError("Результат не найден")
    return sub


async def _reviewable(session: AsyncSession, sub_id: int, manager: User) -> tuple[Task, Submission]:
    """Проверяемая сдача: задача SUBMITTED, сдача последняя и ещё без решения."""
    require_manager(manager)
    sub = await _submission_or_error(session, sub_id)
    task = sub.task
    if task.status != TaskStatus.SUBMITTED or task.last_submission is not sub or sub.decision is not None:
        raise DomainError(REVIEW_ALREADY_PROCESSED)
    if task.assignee_id == manager.id:
        raise DomainError("Нельзя оценивать результат собственной задачи")
    return task, sub


async def _claim_review(
    session: AsyncSession, task: Task, sub: Submission, new: TaskStatus, *, values: dict[str, Any] | None = None
) -> None:
    """Атомарно занять решение по сдаче: задача ещё SUBMITTED, сдача — последняя и без решения.

    Условие проверяется одним UPDATE, поэтому из двух начальников, нажавших кнопки
    одновременно, решение примет только первый; второй получит «Результат уже обработан».
    ``values`` — поля задачи, которые решение меняет вместе со статусом (тем же UPDATE).
    """
    newer_exists = (
        select(Submission.id).where(Submission.task_id == task.id, Submission.id > sub.id).exists()
    )
    undecided = select(Submission.id).where(Submission.id == sub.id, Submission.decision.is_(None)).exists()
    claimed = await _claim_status(session, task, TaskStatus.SUBMITTED, new, ~newer_exists, undecided, values=values)
    if not claimed:
        raise DomainError(REVIEW_ALREADY_PROCESSED)


async def _claim_status(
    session: AsyncSession,
    task: Task,
    expected: TaskStatus,
    new: TaskStatus,
    *conditions: ColumnElement[bool],
    values: dict[str, Any] | None = None,
    returning: Sequence[str] = (),
) -> bool:
    """Атомарный переход статуса: ``UPDATE tasks SET status=new WHERE id=? AND status=expected [AND …]``.

    True — переход выполнен (в памяти у task уже новый статус). False — задачу успели изменить
    в другой сессии (rowcount == 0): объект задачи перечитан из БД, чтобы вызывающий код
    объяснил отказ по актуальному статусу. В SQLite UPDATE берёт блокировку записи до commit,
    поэтому параллельная сессия дождётся коммита первой и увидит уже новый статус.

    ``values`` — остальные поля задачи, которые меняет этот же переход (значения или SQL-выражения
    вроде ``Task.rework_count + 1``): они пишутся тем же UPDATE, а не отдельным при flush. Каждый
    обмен с облачной базой стоит 130–190 мс. ``returning`` — поля, которые нужно заодно перечитать
    (свежий срок для сдачи результата). Записанные и перечитанные значения попадают в объект task
    без пометки «изменено» (UPDATE … RETURNING; без RETURNING — отдельный SELECT).
    """
    changes: dict[str, Any] = {"status": new, "updated_at": utcnow(), **(values or {})}
    names = list(dict.fromkeys([*changes, *returning]))
    stmt = (
        update(Task)
        .where(Task.id == task.id, Task.status == expected, *conditions)
        .values(**changes)
        .execution_options(synchronize_session=False)
    )
    columns = [getattr(Task, name) for name in names]
    if session.get_bind().dialect.update_returning:
        row = (await session.execute(stmt.returning(*columns))).first()
    else:  # СУБД без UPDATE … RETURNING
        result = await session.execute(stmt)
        row = None
        if result.rowcount == 1:
            row = (await session.execute(select(*columns).where(Task.id == task.id))).one()
    if row is None:
        await _reload_task(session, task.id)
        return False
    for name, value in zip(names, row, strict=True):
        set_committed_value(task, name, value)
    return True


async def _reload_task(session: AsyncSession, task_id: int) -> None:
    """Перечитать задачу и её сдачи из БД поверх устаревших объектов сессии."""
    stmt = select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
    await session.execute(stmt)


def _ensure_cancellable(task: Task) -> None:
    if task.status not in _CANCELLABLE_STATUSES:
        raise DomainError(_NOT_CANCELLABLE)


def _ensure_submittable(task: Task) -> None:
    if task.status == TaskStatus.SUBMITTED:
        raise DomainError(_ALREADY_SUBMITTED)
    if not task.is_open:
        raise DomainError(_NOT_OPEN_FOR_SUBMIT)


def _complete(
    sub: Submission,
    manager: User | None,
    score: float,
    decision: ReviewDecision,
    *,
    comment: str | None,
    now: datetime,
) -> None:
    """Зафиксировать решение в сдаче (статус DONE, итоговую оценку и время у задачи уже записал
    тот же UPDATE, что занял решение, — _claim_review). manager None — оценку подтвердил бот."""
    sub.final_score = score
    sub.decision = decision
    sub.review_comment = comment
    sub.reviewer = manager
    sub.reviewed_at = now


async def _reset_reminders(session: AsyncSession, task_id: int) -> None:
    """Удалить отметки отправленных напоминаний (после смены срока они должны прийти заново)."""
    await session.execute(delete(ReminderLog).where(ReminderLog.task_id == task_id))


def _task_filters(
    assignee_id: int | None, statuses: Sequence[TaskStatus] | None, overdue_only: bool
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if assignee_id is not None:
        conditions.append(Task.assignee_id == assignee_id if is_db_id(assignee_id) else false())
    if statuses is not None:
        conditions.append(Task.status.in_(list(statuses)))
    if overdue_only:
        conditions.append(Task.status.in_(OPEN_STATUSES))
        conditions.append(Task.deadline < utcnow())
    return conditions


def _local_week_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """Границы местной календарной недели (пн 00:00 — следующий пн 00:00) в naive UTC."""
    try:
        local_day = to_local(moment).date()
        monday = local_day - timedelta(days=local_day.weekday())
        start = to_utc(datetime.combine(monday, time.min))
        end = to_utc(datetime.combine(monday + timedelta(days=7), time.min))
    except OverflowError:  # срок у предела календаря (старые данные): неделя «до конца времён»
        return moment - timedelta(days=7), datetime.max
    return start, end


def _is_active_employee(user: User) -> bool:
    return user.is_active and user.role == Role.EMPLOYEE


def _ensure_assignee(task: Task, user: User | None, message: str) -> None:
    if user is None or task.assignee_id != user.id:
        raise DomainError(message)
    if not user.is_active:
        raise DomainError("Ваша учётная запись неактивна")


def _attachment(item: AttachmentIn) -> Attachment | None:
    """Файл для записи в БД: поля в пределах колонок (PostgreSQL длиннее не примет).

    file_id нельзя обрезать — по обрезанному Telegram файл не отдаст. Такой длины у Telegram
    не бывает, но если придёт — файл пропускается с предупреждением в лог, а сдача сохраняется.
    Размер файла больше 2^31-1 (Telegram допускает до 4 ГБ) записывается как 2^31-1:
    «очень большой» для проверок размера (AI его всё равно не читает).
    """
    file_id = clean_text(item.file_id or "")
    if not file_id or len(file_id) > _MAX_FILE_ID_LEN:
        logger.warning("Файл-подтверждение пропущен: file_id длиной %s не помещается в базу", len(file_id))
        return None
    return Attachment(
        kind=AttachmentKind(item.kind),
        file_id=file_id,
        file_unique_id=clip(item.file_unique_id, _MAX_FILE_UNIQUE_ID_LEN) or None,
        file_name=clip_file_name(item.file_name, _MAX_FILE_NAME_LEN) or None,
        mime_type=clip(item.mime_type, _MAX_MIME_LEN) or None,
        file_size=clamp_int(item.file_size),
    )


def _late_days(now: datetime, deadline: datetime) -> float:
    """Дни просрочки, округлённые до 0.1; 0 — если сдано в срок."""
    if now <= deadline:
        return 0.0
    return round(days_between(now, deadline), 1)


def _round_half_up(value: float) -> float:
    return float(math.floor(value + 0.5))


# --- Нормализация и проверка полей ----------------------------------------------------------


def _normalize_field(name: str, value: Any) -> Any:
    """Проверить и привести значение редактируемого поля задачи."""
    match name:
        case "title":
            return _required_text(value, "Задача", _MAX_TITLE_LEN)
        case "expected_result":
            return _required_text(value, "Ожидаемый результат")
        case "description":
            return _optional_text(value)
        case "plan_value":
            return _plan_value(value)
        case "plan_unit":
            return _optional_text(value, "Единица плана", _MAX_UNIT_LEN)
        case "deadline":
            return _future_deadline(value)
        case "priority":
            return _priority(value)
        case "weight":
            return _weight(value)
    raise TypeError(f"Неизвестное поле задачи: {name!r}")


def _required_text(value: str | None, label: str, max_len: int | None = None) -> str:
    cleaned = _optional_text(value, label, max_len)
    if cleaned is None:
        raise DomainError(f"Поле «{label}» не может быть пустым")
    return cleaned


def _optional_text(value: str | None, label: str = "Текст", max_len: int | None = None) -> str | None:
    cleaned = clean_text(value or "").strip()
    if max_len is not None and len(cleaned) > max_len:
        raise DomainError(f"Поле «{label}» слишком длинное (до {max_len} символов)")
    return cleaned or None


def _weight(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise DomainError("Вес задачи — целое число от 1 до 100 %")
    return value


def _priority(value: Any) -> Priority:
    try:
        return Priority(value)
    except ValueError as exc:
        raise DomainError("Неизвестный приоритет") from exc


def _plan_value(value: float | None) -> float | None:
    number = _number(value)
    if number is not None and number <= 0:
        raise DomainError("Плановое значение должно быть положительным числом")
    return number


def _fact_value(value: float | None) -> float | None:
    number = _number(value)
    if number is not None and number < 0:
        raise DomainError("Фактическое значение не может быть отрицательным")
    return number


def _number(value: float | None) -> float | None:
    """None или конечное число; иначе DomainError."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("Ожидается число") from exc
    if not math.isfinite(number):
        raise DomainError("Ожидается число")
    return number


def _future_deadline(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise DomainError("Укажите срок выполнения")
    deadline = naive_utc(value)
    now = utcnow()
    if deadline <= now:
        raise DomainError("Срок должен быть в будущем")
    if deadline - now > _MAX_DEADLINE_AHEAD:
        raise DomainError("Срок слишком далёкий — проверьте год (не дальше 5 лет вперёд)")
    return deadline


def _jsonable(value: Any) -> Any:
    """Привести данные события к JSON: datetime -> ISO-строка, enum -> значение."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, float):
        return finite_or_none(value)  # NaN/Infinity — недопустимый JSON для PostgreSQL
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
