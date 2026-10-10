"""После того как сотрудник внёс поручение: подобрать вес (AI или по умолчанию) и уведомить начальников.

Общий конвейер для чата (``bot.handlers.task_propose``) и приложения (``bot.webapp.api``): обе дороги
после ``tasks.propose_task`` + commit делают одно и то же. Вес — подсказка начальнику при
подтверждении; если он не ответил за AUTO_CONFIRM_HOURS, с этим весом поручение принимается само
(``bot.scheduler.jobs.run_auto_decisions``).

AI и уведомления вызываются через атрибуты модулей (``weigh.suggest_weight``,
``notify.notify_proposal``): тесты подменяют именно их.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notify
from bot.ai import weigh
from bot.db.models import Task
from bot.services import tasks as tasks_svc
from bot.services.errors import DomainError
from bot.utils.text import fmt_num

__all__ = ["WEIGHT_BUDGET_SEC", "run_after_propose", "suggest_weight"]

log = logging.getLogger(__name__)

# Сотрудник в это время ждёт ответа «Отправлено»: дольше AI не ждём — берём вес по умолчанию.
WEIGHT_BUDGET_SEC = 8.0


async def suggest_weight(
    session: AsyncSession,
    task: Task,
    *,
    time_budget: float | None = WEIGHT_BUDGET_SEC,
    always_store: bool = False,
) -> weigh.WeightSuggestion:
    """Подобрать вес поручения и записать его в задачу (временный вес + событие WEIGHT_SUGGESTED, commit).

    AI выключен — вес по умолчанию без обращений к базе (у поручения он и так временный); с
    ``always_store`` (автоматическое принятие) вес записывается в любом случае. Поручение тем временем
    обработал начальник — вес не записывается. Никогда не бросает Exception.
    """
    fallback = weigh.rules_weight()
    if not always_store and not weigh.ai_available():
        return fallback
    task_id = task.id
    try:
        others = await tasks_svc.week_tasks(session, task.assignee_id, task.deadline, exclude_task_id=task_id)
        await session.commit()  # перед запросом к AI соединение — обратно в пул
        plan = f"{fmt_num(task.plan_value)} {task.plan_unit or ''}".strip() if task.plan_value is not None else None
        suggestion = await weigh.suggest_weight(
            title=task.title,
            expected_result=task.expected_result,
            plan=plan,
            week_tasks=others,
            time_budget=time_budget,
        )
    except Exception:  # noqa: BLE001 - поручение уже сохранено, начальник должен его получить
        log.exception("Не удалось подобрать вес поручения #%s — беру вес по умолчанию", task_id)
        suggestion = fallback
    try:
        await tasks_svc.set_proposal_weight(
            session, task_id, weight=suggestion.weight, source=suggestion.source, note=suggestion.note
        )
        await session.commit()
    except DomainError:
        await session.rollback()  # начальник уже принял решение по поручению
    except Exception:  # noqa: BLE001
        log.exception("Не удалось записать предложенный вес поручения #%s", task_id)
        await session.rollback()
    return suggestion


async def run_after_propose(bot: Bot, session: AsyncSession, task: Task) -> int:
    """Поручение уже сохранено (propose_task + commit): подобрать вес и уведомить начальников.

    -> скольким начальникам доставлено уведомление. Никогда не бросает Exception.
    """
    suggestion = await suggest_weight(session, task)
    try:
        return int(await notify.notify_proposal(bot, session, task, suggestion) or 0)
    except Exception:  # noqa: BLE001 - notify_* и так не бросают; подстраховка
        log.exception("Не удалось уведомить начальников о поручении #%s", task.id)
        return 0
