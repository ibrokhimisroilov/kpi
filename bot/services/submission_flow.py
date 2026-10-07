"""Общий конвейер после сдачи результата: предварительная оценка (AI или правила) и уведомление руководителю.

Им пользуются и чат (``bot.handlers.task_submit``), и приложение в Telegram (``bot.webapp.api``, в фоне):
обе дороги делают после ``tasks.submit_result`` + commit одно и то же. Модуль ничего не знает про
диалоги FSM и сообщения сотруднику — что ответить сотруднику, решает вызывающий код по ``FlowResult``.

Порядок (как раньше в task_submit):

1. ``evaluate_and_record`` — оценка AI (файлы скачиваются, только если AI включён) с общим сроком
   ``budget_sec``; AI нет, упал или не уложился — расчёт по правилам (``RULES_PREFIX``). Оценка
   записывается ``tasks.record_evaluation`` + commit; запись оценки AI упала (ошибка базы) — откат,
   перечитывание и правила.
2. ``fresh`` — свежие задача и сдача из базы: пока AI думал, руководитель мог уже решить по сдаче
   или отменить задачу.
3. Сдача ещё ждёт решения — ``notify.notify_submission`` (руководителю: план ↔ факт, кнопки, файлы).

Сервисы и уведомления вызываются через атрибуты модулей (``tasks_svc.record_evaluation``,
``notify.notify_submission``, ``ai_evaluate.evaluate_submission``…): тесты подменяют именно их.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notify
from bot.ai import evaluate as ai_evaluate
from bot.ai import evidence as ai_evidence
from bot.ai import provider as ai_provider
from bot.db.models import Submission, Task, TaskStatus
from bot.services import tasks as tasks_svc

__all__ = [
    "AI_BUDGET_MARGIN_SEC",
    "RULES_PREFIX",
    "FlowResult",
    "awaits_review",
    "default_budget_sec",
    "evaluate",
    "evaluate_and_record",
    "fresh",
    "reload",
    "result_with_notes",
    "run_after_submit",
]

log = logging.getLogger(__name__)

RULES_PREFIX = "Расчёт по правилам (AI недоступен): "
AI_BUDGET_MARGIN_SEC = 5  # запас до общего срока оценки: AI заканчивает раньше, чем его прервут


@dataclass(frozen=True)
class FlowResult:
    """Чем закончился конвейер после сдачи."""

    task_id: int
    submission_id: int
    status: TaskStatus | None  # свежий статус задачи после оценки; None — перечитать не удалось
    source: str | None  # "ai" | "rules" — чем оценено; None — оценку записать не удалось
    notified: bool  # notify_submission вызван (сдача ещё ждала решения)


def result_with_notes(result: str | None, notes: Sequence[str]) -> str | None:
    """«Какой результат» + текстовое описание подтверждающих материалов (если было)."""
    parts = [result] if result else []
    if notes:
        parts.append("Подтверждающие материалы: " + "; ".join(notes))
    return "\n\n".join(parts) or None


def default_budget_sec() -> float:
    """Сколько ждать AI целиком (скачивание файлов + перебор моделей), потом — правила.

    Если бот остановят посреди оценки (обновление на хостинге, сбой), сдачу без оценки позже найдут
    задания по расписанию (bot.scheduler.jobs.recover_stalled_evaluations): оценят по правилам и
    передадут руководителю.
    """
    return ai_evaluate.evaluation_budget_sec()


async def evaluate(
    bot: Bot, task: Task, sub: Submission, *, budget_sec: float, use_ai: bool
) -> ai_evaluate.Evaluation | None:
    """Оценка AI (или правил — решает evaluate_submission). None — упало или не уложилось во время.

    Никогда не бросает Exception (отмена задачи — CancelledError — пробрасывается).
    """
    loop = asyncio.get_running_loop()
    ends_at = loop.time() + budget_sec

    async def run() -> ai_evaluate.Evaluation:
        # Без AI файлы скачивать незачем: evaluate_submission всё равно посчитает по правилам.
        evidence = await ai_evidence.collect_evidence(bot, list(sub.attachments)) if use_ai else []
        # Перебор моделей — только в оставшееся после скачивания файлов время (с запасом), чтобы AI успел
        # ответить или отказаться сам, а не был прерван по общему сроку ниже.
        left = ends_at - loop.time() - AI_BUDGET_MARGIN_SEC
        return await ai_evaluate.evaluate_submission(task, sub, evidence, time_budget=left)

    try:
        return await asyncio.wait_for(run(), timeout=budget_sec)
    except TimeoutError:
        log.warning("Оценка сдачи #%s не уложилась в %.0f с — считаю по правилам", sub.id, budget_sec)
    except Exception:  # noqa: BLE001 - оценка должна быть всегда
        log.exception("Ошибка оценки сдачи #%s — считаю по правилам", sub.id)
    return None


async def evaluate_and_record(
    bot: Bot, session: AsyncSession, task: Task, sub: Submission, *, budget_sec: float, use_ai: bool
) -> tuple[Task, Submission]:
    """Оценить сдачу и сохранить оценку (с commit). При любой проблеме с AI — rules_score."""
    task_id, sub_id = task.id, sub.id
    plan_value, fact_value, late_days = task.plan_value, sub.fact_value, float(sub.late_days or 0.0)

    evaluation = await evaluate(bot, task, sub, budget_sec=budget_sec, use_ai=use_ai)
    if evaluation is not None:
        try:
            await tasks_svc.record_evaluation(
                session,
                sub_id,
                score=evaluation.score,
                rationale=evaluation.rationale,
                source=evaluation.source,
                model=evaluation.model,
            )
            await session.commit()
            return task, sub
        except Exception:  # noqa: BLE001 - например, некорректный ответ модели; пробуем правила
            log.exception("Не удалось сохранить оценку AI для сдачи #%s — считаю по правилам", sub_id)
            task, sub = await reload(session, task_id, sub_id)

    score, explanation = ai_evaluate.rules_score(plan_value, fact_value, late_days)
    await tasks_svc.record_evaluation(
        session, sub_id, score=score, rationale=RULES_PREFIX + explanation, source="rules", model=None
    )
    await session.commit()
    return task, sub


async def reload(session: AsyncSession, task_id: int, sub_id: int) -> tuple[Task, Submission]:
    """Откатить неудачную транзакцию и заново загрузить задачу и сдачу (старые объекты протухли)."""
    await session.rollback()
    session.expunge_all()
    task = await tasks_svc.get_task(session, task_id)
    sub = next((s for s in task.submissions if s.id == sub_id), None) if task is not None else None
    if task is None or sub is None:
        raise RuntimeError(f"Сдача #{sub_id} задачи #{task_id} не найдена после отката")
    return task, sub


async def fresh(
    session: AsyncSession, task_id: int, sub_id: int, fallback: tuple[Task, Submission]
) -> tuple[Task, Submission]:
    """Свежие задача и сдача из БД (изменения других пользователей видны); при сбое — fallback."""
    try:
        return await reload(session, task_id, sub_id)
    except Exception:  # noqa: BLE001 - уведомить руководителя важнее, чем идеально свежие данные
        log.exception("Не удалось перечитать сдачу #%s перед уведомлением", sub_id)
        return fallback


def awaits_review(task: Task, sub: Submission) -> bool:
    """Сдача ещё ждёт решения: задача на проверке, сдача последняя и без решения."""
    last = task.last_submission
    return task.status == TaskStatus.SUBMITTED and sub.decision is None and last is not None and last.id == sub.id


async def run_after_submit(
    bot: Bot,
    session: AsyncSession,
    task: Task,
    sub: Submission,
    *,
    budget_sec: float | None = None,
    use_ai: bool | None = None,
) -> FlowResult:
    """Сдача уже сохранена (submit_result + commit): оценить её и уведомить руководителя.

    budget_sec None — ``default_budget_sec()``; use_ai None — ``provider.ai_available()``.
    Никогда не бросает Exception: сдача уже сохранена, а не дооценённую сдачу позже подберёт
    ``jobs.recover_stalled_evaluations``. Отмена (CancelledError) пробрасывается.
    """
    task_id, sub_id = task.id, sub.id
    try:
        budget = default_budget_sec() if budget_sec is None else budget_sec
        ai_on = ai_provider.ai_available() if use_ai is None else use_ai
        return await _run(bot, session, task, sub, budget_sec=budget, use_ai=ai_on)
    except Exception:  # noqa: BLE001 - конвейер не должен ломать того, кто его вызвал
        log.exception("Конвейер после сдачи #%s завершился ошибкой", sub_id)
        return FlowResult(task_id=task_id, submission_id=sub_id, status=None, source=None, notified=False)


async def _run(
    bot: Bot, session: AsyncSession, task: Task, sub: Submission, *, budget_sec: float, use_ai: bool
) -> FlowResult:
    task_id, sub_id = task.id, sub.id
    source: str | None = None
    current: tuple[Task, Submission] | None = (task, sub)
    try:
        current = await evaluate_and_record(bot, session, task, sub, budget_sec=budget_sec, use_ai=use_ai)
        source = current[1].ai_source
    except Exception:  # noqa: BLE001 - сдача уже сохранена, руководитель должен её получить
        log.exception("Не удалось сохранить предварительную оценку сдачи #%s", sub_id)
        try:
            current = await reload(session, task_id, sub_id)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось перечитать сдачу #%s", sub_id)
            current = None
    if current is not None:
        # Пока AI думал (до пары минут), руководитель мог уже решить по сдаче из «📝 На проверке»
        # или отменить задачу — сессия этого не видит, поэтому перечитываем свежее состояние.
        current = await fresh(session, task_id, sub_id, current)
    notified = False
    if current is not None and awaits_review(*current):
        notified = True
        try:
            await notify.notify_submission(bot, session, *current)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось уведомить руководителя о сдаче #%s", sub_id)
    elif current is not None:
        log.info("Сдача #%s уже не ждёт проверки (%s) — уведомление руководителю не нужно", sub_id, current[0].status)
    status = current[0].status if current is not None else None
    return FlowResult(task_id=task_id, submission_id=sub_id, status=status, source=source, notified=notified)
