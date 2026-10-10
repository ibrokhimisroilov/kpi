"""Автоподтверждение: решение принимается само, если начальник не ответил за AUTO_CONFIRM_HOURS (24 ч).

Что подтверждается само (bot.scheduler.jobs.run_auto_decisions):

* **оценка результата** — оценка AI становится итоговой. Ждут начальника сколько потребуется: оценка,
  рассчитанная по правилам (AI был недоступен — она строится на цифрах самого сотрудника), и оценка выше
  AUTO_CONFIRM_MAX_SCORE (100 %) — ``score_block``;
* **поручение сотрудника** — принимается с весом, который предложил AI (``bot.ai.weigh``; AI недоступен —
  AUTO_PROPOSAL_WEIGHT), и средним приоритетом. Поручение с истёкшим сроком само не принимается.

Заявки на доступ к боту всегда подтверждает начальник.

Время (``plan``). Отсчёт — от сдачи результата / внесения поручения, но не раньше момента, когда
автоподтверждение впервые заработало в этой базе (``feature_start``): иначе в день обновления всё давно
ожидающее подтвердилось бы разом. Выходные считаются. Срок в тихие часы переносится на их конец (8:00).
За AUTO_CONFIRM_REMIND_HOURS до срока начальник получает напоминание; если это время попадает в тихие
часы — напоминание уходит вечером накануне, за час до их начала.

Автоматически подтверждённую оценку начальник может изменить в течение AUTO_REVISE_DAYS (7 дней) —
``revise_until`` / ``can_revise`` (сервис — ``bot.services.tasks.review_revise_auto``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.config import get_settings
from bot.db.models import Submission, Task, TaskStatus
from bot.utils.dates import to_local, to_utc, utcnow

__all__ = [
    "BLOCK_HIGH",
    "BLOCK_NO_SCORE",
    "BLOCK_RULES",
    "AutoPlan",
    "can_revise",
    "enabled",
    "plan",
    "proposal_due",
    "revise_until",
    "score_block",
    "score_due",
]

BLOCK_NO_SCORE = "no_score"  # предварительной оценки ещё нет
BLOCK_RULES = "rules"        # оценка рассчитана по правилам, без AI
BLOCK_HIGH = "high"          # оценка выше AUTO_CONFIRM_MAX_SCORE


@dataclass(frozen=True)
class AutoPlan:
    """Когда решение будет принято само и когда напомнить об этом начальнику (naive UTC)."""

    due: datetime
    remind_at: datetime | None  # None — напоминание не нужно (выключено или отсчёт только что начался)


def enabled() -> bool:
    return get_settings().auto_confirm_hours > 0


def plan(started: datetime | None, feature_start: datetime | None = None) -> AutoPlan | None:
    """План автоподтверждения для события, случившегося в ``started``. None — автоподтверждение выключено
    (или время события неизвестно: объект ещё не сохранён в базе)."""
    settings = get_settings()
    if settings.auto_confirm_hours <= 0 or started is None:
        return None
    if feature_start is not None and feature_start > started:
        started = feature_start
    due = _leave_quiet_forward(started + timedelta(hours=settings.auto_confirm_hours))
    remind_at: datetime | None = None
    if settings.auto_confirm_remind_hours > 0:
        remind_at = _leave_quiet_backward(due - timedelta(hours=settings.auto_confirm_remind_hours))
        if remind_at <= started:  # отсчёт короче напоминания: начальник только что получил само уведомление
            remind_at = None
    return AutoPlan(due=due, remind_at=remind_at)


def score_block(sub: Submission | None) -> str | None:
    """Почему оценку этой сдачи нельзя подтвердить автоматически (BLOCK_*); None — можно."""
    if sub is None or sub.ai_score is None:
        return BLOCK_NO_SCORE
    if sub.ai_source != "ai":
        return BLOCK_RULES
    if sub.ai_score > get_settings().auto_confirm_max_score:
        return BLOCK_HIGH
    return None


def score_due(task: Task, sub: Submission | None, feature_start: datetime | None = None) -> AutoPlan | None:
    """План для сдачи, которая ждёт решения и может быть подтверждена сама; иначе None."""
    if sub is None or task.status != TaskStatus.SUBMITTED or sub.decision is not None:
        return None
    last = task.last_submission
    if last is None or last.id != sub.id or score_block(sub) is not None:
        return None
    return plan(sub.created_at, feature_start)


def proposal_due(task: Task, feature_start: datetime | None = None) -> AutoPlan | None:
    """План для поручения, которое ждёт решения начальника; иначе None."""
    if task.status != TaskStatus.PROPOSED:
        return None
    return plan(task.created_at, feature_start)


def revise_until(sub: Submission | None) -> datetime | None:
    """До какого момента начальник может изменить автоматически подтверждённую оценку; None — нельзя."""
    days = get_settings().auto_revise_days
    if sub is None or not sub.auto_confirmed or sub.reviewed_at is None or days <= 0:
        return None
    return sub.reviewed_at + timedelta(days=days)


def can_revise(task: Task, sub: Submission | None, now: datetime | None = None) -> bool:
    """Оценку подтвердил бот, задача выполнена, сдача последняя и срок на изменение не вышел."""
    until = revise_until(sub)
    if until is None or sub is None or task.status != TaskStatus.DONE:
        return False
    last = task.last_submission
    return last is not None and last.id == sub.id and (now or utcnow()) <= until


# --- Тихие часы -----------------------------------------------------------------------------------


def _is_quiet(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _leave_quiet_forward(moment: datetime) -> datetime:
    """Момент в тихие часы -> ближайший их конец (например, 23:10 -> 8:00 следующего дня)."""
    settings = get_settings()
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    local = to_local(moment)
    if not _is_quiet(local.hour, start, end):
        return moment
    target = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return to_utc(target)


def _leave_quiet_backward(moment: datetime) -> datetime:
    """Момент в тихие часы -> за час до их начала (например, 5:00 -> 20:00 накануне)."""
    settings = get_settings()
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    local = to_local(moment)
    if not _is_quiet(local.hour, start, end):
        return moment
    target = local.replace(hour=start, minute=0, second=0, microsecond=0) - timedelta(hours=1)
    if target > local:
        target -= timedelta(days=1)
    return to_utc(target)
