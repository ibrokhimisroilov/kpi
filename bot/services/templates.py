"""Личные шаблоны задач и поручений (docs/MINIAPP_SPEC.md §16).

Шаблон — заготовка формы «Новая задача» (начальник) или «Поручение» (сотрудник). Он личный: все функции
работают только с шаблонами владельца — чужой шаблон для них не существует. Шаблон ничего не создаёт сам:
задача или поручение появляются обычным путём (tasks.create_task / tasks.propose_task) со всеми проверками.

Поля начальника (приоритет, вес, исполнитель) сохраняются только владельцу-начальнику: у сотрудника они
всегда пустые, что бы ни прислал клиент.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Priority, Role, Task, TaskTemplate, User
from bot.services.dbsafe import is_db_id
from bot.services.errors import DomainError
from bot.utils.dates import utcnow

__all__ = [
    "LIMIT",
    "MANAGER_FIELDS",
    "TEXT_FIELDS",
    "create_template",
    "delete_template",
    "from_task",
    "get_own",
    "list_templates",
    "update_template",
]

LIMIT = 50  # шаблонов на человека
TEXT_FIELDS = ("title", "expected_result", "plan_value", "plan_unit")
MANAGER_FIELDS = ("priority", "weight", "assignee_id")

TXT_LIMIT = "Шаблонов уже {limit} — удалите ненужные, чтобы сохранить новый"
TXT_ASSIGNEE = "Исполнителем может быть только активный сотрудник"


async def list_templates(session: AsyncSession, owner: User) -> list[TaskTemplate]:
    """Шаблоны владельца: недавно изменённые — первыми."""
    query = (
        select(TaskTemplate)
        .where(TaskTemplate.owner_id == owner.id)
        .order_by(TaskTemplate.updated_at.desc(), TaskTemplate.id.desc())
        .limit(LIMIT)
    )
    return list(await session.scalars(query))


async def get_own(session: AsyncSession, owner: User, template_id: int) -> TaskTemplate | None:
    """Шаблон владельца по номеру; чужого или несуществующего нет (None) — разницы снаружи не видно."""
    if not is_db_id(template_id):
        return None
    query = select(TaskTemplate).where(TaskTemplate.id == template_id, TaskTemplate.owner_id == owner.id)
    return (await session.scalars(query)).first()


async def create_template(session: AsyncSession, owner: User, **fields: Any) -> TaskTemplate:
    """Новый шаблон из полей формы (уже проверенных по виду и длине)."""
    await _ensure_room(session, owner)
    template = TaskTemplate(owner_id=owner.id, created_at=utcnow(), updated_at=utcnow())
    await _apply(session, owner, template, fields)
    if owner.is_manager and template.priority is None:
        template.priority = Priority.MEDIUM
    session.add(template)
    await session.flush()
    return template


async def from_task(session: AsyncSession, owner: User, task: Task) -> TaskTemplate:
    """«Сохранить как шаблон» из задачи: переносятся только поля формы. Исполнитель — если он ещё активный
    сотрудник. Задача не меняется; право видеть задачу проверяет вызывающий."""
    fields: dict[str, Any] = {
        "title": task.title,
        "expected_result": task.expected_result,
        "plan_value": task.plan_value,
        "plan_unit": task.plan_unit if task.plan_value is not None else None,
    }
    if owner.is_manager:
        assignee = task.assignee
        fields.update(
            priority=Priority(task.priority),
            weight=task.weight,
            assignee_id=assignee.id if assignee is not None and _is_active_employee(assignee) else None,
        )
    return await create_template(session, owner, **fields)


async def update_template(session: AsyncSession, owner: User, template: TaskTemplate, **fields: Any) -> TaskTemplate:
    """Правка: только присланные поля."""
    await _apply(session, owner, template, fields)
    template.updated_at = utcnow()
    await session.flush()
    return template


async def delete_template(session: AsyncSession, template: TaskTemplate) -> None:
    await session.delete(template)
    await session.flush()


async def _ensure_room(session: AsyncSession, owner: User) -> None:
    count = await session.scalar(select(func.count()).select_from(TaskTemplate).where(TaskTemplate.owner_id == owner.id))
    if (count or 0) >= LIMIT:
        raise DomainError(TXT_LIMIT.format(limit=LIMIT))


async def _apply(session: AsyncSession, owner: User, template: TaskTemplate, fields: dict[str, Any]) -> None:
    for name in TEXT_FIELDS:
        if name in fields:
            setattr(template, name, fields[name])
    if template.plan_value is None:
        template.plan_unit = None  # без числа единица не нужна
    if not owner.is_manager:
        return  # поля начальника сотруднику не сохраняются
    if "priority" in fields:
        template.priority = fields["priority"]
    if "weight" in fields:
        template.weight = fields["weight"]
    if "assignee_id" in fields:
        assignee_id = fields["assignee_id"]
        if assignee_id is not None:
            assignee = await session.get(User, assignee_id) if is_db_id(assignee_id) else None
            if assignee is None or not _is_active_employee(assignee):
                raise DomainError(TXT_ASSIGNEE)
            template.assignee = assignee
        else:
            template.assignee = None
        template.assignee_id = assignee_id


def _is_active_employee(user: User) -> bool:
    return user.is_active and user.role == Role.EMPLOYEE
