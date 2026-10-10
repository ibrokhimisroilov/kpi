"""JSON-схемы приложения (docs/MINIAPP_SPEC.md §7) — чистые функции без обращений к базе.

Сериализаторы читают только уже загруженные поля и связи (жадные связи моделей, строки лёгких
чтений ``TaskRowData`` / ``HistoryRowData``): ленивых загрузок здесь быть не может. Подписи статусов,
сроков и событий берутся из того же ``bot.ui.render``, что и в чате, — без HTML.

Время: моменты — ISO-8601 UTC с «Z» (``iso``), рядом — готовые местные строки (Asia/Tashkent).
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bot import i18n
from bot.db.models import (
    OPEN_STATUSES,
    Attachment,
    EventType,
    Priority,
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
from bot.services import auto
from bot.services.kpi import KpiResult
from bot.services.periods import Period
from bot.ui import render
from bot.utils.dates import fmt_datetime, to_local
from bot.utils.text import fmt_num, fmt_pct

__all__ = [
    "HistoryRowData",
    "TaskRowData",
    "actions",
    "event",
    "history_item",
    "iso",
    "kpi_block",
    "kpi_short",
    "kpi_stats",
    "kpi_text",
    "me_user",
    "period",
    "short_name",
    "submission",
    "task_card",
    "task_detail",
    "task_row",
    "trend",
    "user_ref",
    "visible_events",
]

KPI_NO_DATA = "нет данных"
BOT_ACTOR = "🤖 Бот"
AI_LABEL = "🤖 AI предлагает"
RULES_LABEL = "📐 Расчёт по правилам (AI недоступен)"
DECISION_LABELS: dict[ReviewDecision, str] = {
    ReviewDecision.APPROVED: "✅ подтверждена",
    ReviewDecision.CHANGED: "✏️ изменена начальником",
    ReviewDecision.REWORK: "↩️ Возвращено на доработку",
}
AUTO_DECISION_LABEL = "⏱ подтверждена автоматически"  # оценку AI подтвердил бот (Submission.auto_confirmed)
_ATTACHMENT_NAMES = {"photo": "фото", "video": "видео", "document": "документ", "other": "файл"}
_PROPOSAL_EDIT_FIELDS = ("title", "expected_result", "plan", "deadline")
_TASK_EDIT_FIELDS = ("title", "expected_result", "plan", "deadline", "priority", "weight")
_EDITABLE = (TaskStatus.PROPOSED, TaskStatus.ACTIVE, TaskStatus.REWORK)
_TAG_RE = re.compile(r"<[^>]*>")


# --- Строки лёгких чтений (bot.webapp.api, раздел «Лёгкие чтения») ----------------------------------------


@dataclass(frozen=True)
class TaskRowData:
    """Строка списка задач: поля задачи и исполнителя одним запросом, без сдач и файлов."""

    id: int
    title: str
    status: TaskStatus
    priority: Priority
    weight: int
    source: TaskSource
    deadline: datetime
    accepted_at: datetime | None
    submitted_at: datetime | None
    completed_at: datetime | None
    final_score: float | None
    rework_count: int
    last_late: bool | None  # is_late последней сдачи (None — сдач нет)
    assignee_id: int
    assignee_full_name: str
    assignee_position: str | None
    assignee_role: Role
    assignee_status: UserStatus
    expected_result: str | None = None  # только в поиске (для совпадений)

    # Как у Task — чтобы подписи строились теми же функциями bot.ui.render, что и в чате.
    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def last_submission(self) -> None:
        return None  # время сдачи строки — submitted_at (его ставит каждая сдача)


@dataclass(frozen=True)
class HistoryRowData:
    """Оценённая задача для истории оценок."""

    task_id: int
    title: str
    weight: int
    completed_at: datetime | None
    ai_score: float | None  # task.ai_score или ai_score последней сдачи
    final_score: float | None
    rework_count: int
    decision: ReviewDecision | None  # решение по последней сдаче
    is_late: bool | None  # опоздание последней сдачи


# --- Общие хелперы ----------------------------------------------------------------------------------------


def iso(dt: datetime | None) -> str | None:
    """naive UTC из базы -> «2026-10-05T13:00:00Z»; None -> None."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return f"{dt:%Y-%m-%dT%H:%M:%S}Z"


def _dm(dt: datetime | None) -> str | None:
    return to_local(dt).strftime("%d.%m") if dt is not None else None


def _dm_hm(dt: datetime) -> str:
    return to_local(dt).strftime("%d.%m %H:%M")


def _plain(text: str | None) -> str | None:
    """HTML чата -> обычный текст: теги убираются, сущности раскрываются (пользовательский «<b>» —
    в чате уже «&lt;b&gt;» — снова становится текстом «<b>»)."""
    if text is None:
        return None
    # Сначала перевод (строки каталога — с тегами чата), потом теги убираются; метки снимает перевод.
    return html.unescape(_TAG_RE.sub("", i18n.tr(text)))


def _t(text: str | None) -> str | None:
    """Подпись бота без HTML — на язык запроса (SPEC.md §14); слова пользователя сюда не передаются."""
    return i18n.tr(text) if text else text


def _value(item: Any) -> Any:
    return getattr(item, "value", item)


def kpi_text(value: float | None) -> str:
    return fmt_pct(value) if value is not None else i18n.tr(KPI_NO_DATA)


def short_name(full_name: str) -> str:
    """«Иванов Иван Иванович» -> «Иванов И. И.» (как User.short_name)."""
    parts = full_name.split()
    if len(parts) <= 1:
        return full_name
    return parts[0] + " " + " ".join(p[0] + "." for p in parts[1:] if p)


# --- Люди -------------------------------------------------------------------------------------------------


def user_ref(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "full_name": user.full_name,
        "short_name": user.short_name,
        "position": user.position,
        "role": _value(user.role),
        "status": _value(user.status),
    }


def me_user(user: User) -> dict[str, Any]:
    return {**user_ref(user), "tg_id": user.tg_id}


def _row_assignee(row: TaskRowData) -> dict[str, Any]:
    return {
        "id": row.assignee_id,
        "full_name": row.assignee_full_name,
        "short_name": short_name(row.assignee_full_name),
        "position": row.assignee_position,
        "role": _value(row.assignee_role),
        "status": _value(row.assignee_status),
    }


# --- Задачи -----------------------------------------------------------------------------------------------


def task_row(task: Task | TaskRowData, now: datetime) -> dict[str, Any]:
    """TaskRow: строка списка — одинаково из строки лёгкого чтения и из ORM Task."""
    if isinstance(task, Task):
        assignee = user_ref(task.assignee)
        last = task.last_submission
        last_late: bool | None = bool(last.is_late) if last is not None else None
    else:
        assignee = _row_assignee(task)
        last_late = task.last_late
    status = TaskStatus(task.status)
    done = status == TaskStatus.DONE
    return {
        "id": task.id,
        "title": task.title,
        "status": status.value,
        "status_label": _t(render.status_label(task, now)),  # type: ignore[arg-type]
        "overdue": task.is_open and task.deadline < now,
        "priority": _value(task.priority),
        "priority_label": _t(render.PRIORITY_LABELS.get(Priority(task.priority), str(task.priority))),
        "weight": task.weight,
        "source": _value(task.source),
        "deadline": iso(task.deadline),
        "deadline_local": _t(fmt_datetime(task.deadline)),
        "tail": _plain(render._line_tail(task, now)),  # type: ignore[arg-type]  # noqa: SLF001 - подпись чата
        "assignee": assignee,
        "accepted": task.accepted_at is not None,
        "submitted_at": iso(task.submitted_at),
        "completed_at": iso(task.completed_at),
        "final_score": task.final_score if done else None,
        "final_score_text": fmt_pct(task.final_score) if done and task.final_score is not None else None,
        "rework_count": task.rework_count or 0,
        "last_late": last_late,
    }


def actions(task: Task, viewer: User) -> dict[str, Any]:
    """Действия в карточке для этого зрителя — как keyboards.task_actions_kb в чате."""
    manager = viewer.is_manager
    assignee = viewer.id == task.assignee_id
    status = task.status
    last = task.last_submission
    review = manager and status == TaskStatus.SUBMITTED and last is not None and last.decision is None
    # Оценку подтвердил бот — начальник ещё может её изменить (bot.services.auto, AUTO_REVISE_DAYS).
    revise = manager and last is not None and auto.can_revise(task, last) and task.assignee_id != viewer.id
    revise_until = auto.revise_until(last) if revise else None
    if manager and status == TaskStatus.PROPOSED:
        fields: list[str] = list(_PROPOSAL_EDIT_FIELDS)
    elif manager and status in OPEN_STATUSES:
        fields = list(_TASK_EDIT_FIELDS)
    else:
        fields = []
    return {
        "accept": assignee and status == TaskStatus.ACTIVE and task.accepted_at is None,
        "submit": assignee and status in OPEN_STATUSES,
        "edit": manager and status in _EDITABLE,
        "edit_fields": fields,
        "cancel": manager and status in OPEN_STATUSES,
        "review": review,
        "review_submission_id": last.id if review and last is not None else None,
        "revise": revise,
        "revise_submission_id": last.id if revise and last is not None else None,
        # «03.10 в 12:00» — приложение вставляет это в свою фразу и переводит её целиком.
        "revise_until_text": render.auto_when(revise_until) if revise_until is not None else None,
        "approve": manager and status == TaskStatus.PROPOSED,
        "reject": manager and status == TaskStatus.PROPOSED,
    }


def _ai_hidden(task: Task, viewer: User) -> bool:
    """Исполнитель не видит оценку AI, пока по последней сдаче нет решения начальника."""
    last = task.last_submission
    return not viewer.is_manager and last is not None and last.decision is None


def _auto_note(task: Task, viewer: User) -> str | None:
    """Строка об автоподтверждении для начальника — те же слова, что в чате (render)."""
    if not viewer.is_manager:
        return None
    if task.status == TaskStatus.PROPOSED:
        plan = auto.proposal_due(task)
        if plan is None or task.deadline <= plan.due:
            return None
        return (
            f"⏱ Без вашего решения поручение будет принято автоматически {render.auto_when(plan.due)} "
            f"с весом {task.weight} % и средним приоритетом."
        )
    last = task.last_submission
    if task.status == TaskStatus.SUBMITTED and last is not None:
        return render._auto_note(task, last)  # noqa: SLF001 - строка чата
    return None


def task_detail(task: Task, viewer: User, now: datetime) -> dict[str, Any]:
    """TaskDetail: TaskRow + поля карточки (задача из ORM: сдачи уже загружены)."""
    last = task.last_submission
    expected = task.expected_result or ""
    description = task.description
    if description and description.strip() == expected.strip():
        description = None  # исходные слова — только если отличаются от измеримой формулировки
    plan_text = f"{fmt_num(task.plan_value)} {task.plan_unit or ''}".strip() if task.plan_value is not None else None
    rework_comment = None
    if last is not None and last.decision == ReviewDecision.REWORK and last.review_comment:
        rework_comment = last.review_comment
    return {
        **task_row(task, now),
        "expected_result": expected,
        "description": description or None,
        "plan_value": task.plan_value,
        "plan_unit": task.plan_unit,
        "plan_text": plan_text,
        "deadline_label": _t(render.deadline_label(task, now)),
        "created_at": iso(task.created_at),
        "updated_at": iso(task.updated_at),
        "accepted_at": iso(task.accepted_at),
        "approved_at": iso(task.approved_at),
        "created_by": user_ref(task.created_by),
        "manager": user_ref(task.manager) if task.manager is not None else None,
        "weight_pending": task.status == TaskStatus.PROPOSED,
        # Начальнику: когда поручение / оценка будут приняты без него или почему оценка его ждёт (None — нечего сказать).
        "auto_note": _t(_auto_note(task, viewer)),
        "ai_score": None if _ai_hidden(task, viewer) else task.ai_score,
        "attempts": len(task.submissions),
        "rework_comment": rework_comment,
        "actions": actions(task, viewer),
    }


# --- Сдачи и журнал ---------------------------------------------------------------------------------------


def _attachment(att: Attachment) -> dict[str, Any]:
    kind = _value(att.kind)
    return {
        "id": att.id,
        "kind": kind,
        "name": att.file_name or _t(_ATTACHMENT_NAMES.get(kind, "файл")),
        "mime_type": att.mime_type,
        "size": att.file_size,
    }


def _ai(sub: Submission, *, full: bool) -> dict[str, Any]:
    rules = sub.ai_source == "rules"
    rationale: str | None = None
    if full and sub.ai_rationale:
        # У расчёта по правилам подпись уже в label — в обосновании её не повторяем (как в чате).
        # Расчёт по правилам — текст бота: переводится по предложениям. Слова AI остаются как написаны.
        rationale = _t(render.rules_sentences(render._rules_head(sub.ai_rationale)[1])) if rules else sub.ai_rationale  # noqa: SLF001
    return {
        "score": sub.ai_score,
        "score_text": fmt_pct(sub.ai_score),
        "source": "rules" if rules else "ai",
        "label": _t(RULES_LABEL if rules else AI_LABEL),
        "rationale": rationale or None,
        "model": sub.ai_model if full else None,
    }


def submission(task: Task, sub: Submission, *, manager_view: bool) -> dict[str, Any]:
    """Submission. Начальник видит всё; исполнитель — оценку AI только после решения и без обоснования."""
    ai: dict[str, Any] | None = None
    ai_pending = False
    ai_hidden = False
    last = task.last_submission
    if manager_view:
        if sub.ai_score is not None:
            ai = _ai(sub, full=True)
        else:
            is_last = last is not None and last.id == sub.id
            ai_pending = sub.decision is None and task.status == TaskStatus.SUBMITTED and is_last
    elif sub.decision is None:
        ai_hidden = True
    elif sub.ai_score is not None:
        ai = _ai(sub, full=False)
    decision = ReviewDecision(sub.decision) if sub.decision is not None else None
    return {
        "id": sub.id,
        "attempt": sub.attempt,
        "created_at": iso(sub.created_at),
        "created_local": _dm_hm(sub.created_at),
        "fact_text": sub.fact_text,
        "result_text": sub.result_text,
        "fact_value": sub.fact_value,
        "fact_line": _plain(render._fact_value_line(task, sub)),  # noqa: SLF001 - строка чата
        "deadline_at_submit": iso(sub.deadline_at_submit),
        "is_late": bool(sub.is_late),
        "late_days": float(sub.late_days or 0.0),
        "late_text": _t(render._late_text(sub)),  # noqa: SLF001
        "attachments": [_attachment(att) for att in sub.attachments],
        "ai": ai,
        "ai_pending": ai_pending,
        "ai_hidden": ai_hidden,
        "decision": decision.value if decision is not None else None,
        "decision_label": _t(
            AUTO_DECISION_LABEL if sub.auto_confirmed else DECISION_LABELS.get(decision) if decision is not None else None
        ),
        "auto_confirmed": sub.auto_confirmed,
        "final_score": sub.final_score,
        "final_score_text": fmt_pct(sub.final_score) if sub.final_score is not None else None,
        "review_comment": sub.review_comment,
        "reviewer": user_ref(sub.reviewer) if sub.reviewer is not None else None,
        "reviewed_at": iso(sub.reviewed_at),
    }


def visible_events(task: Task, events: Sequence[TaskEvent], viewer: User) -> list[TaskEvent]:
    """Исполнитель не видит предварительную оценку AI, пока начальник не принял решение
    (как task_view._visible_events): скрываются AI_EVALUATED после последней сдачи без решения."""
    if viewer.is_manager:
        return list(events)
    sub = task.last_submission
    if sub is None or sub.decision is not None:
        return list(events)
    last_submit = max((i for i, ev in enumerate(events) if ev.type == EventType.SUBMITTED), default=-1)
    return [ev for i, ev in enumerate(events) if not (ev.type == EventType.AI_EVALUATED and i > last_submit)]


def event(ev: TaskEvent) -> dict[str, Any]:
    actor = ev.actor
    return {
        "id": ev.id,
        "type": _value(ev.type),
        "at": iso(ev.created_at),
        "at_local": _dm_hm(ev.created_at),
        "actor": user_ref(actor) if actor is not None else None,
        "actor_name": actor.short_name if actor is not None else _t(BOT_ACTOR),
        "text": _plain(render._event_phrase(ev)),  # noqa: SLF001 - фраза журнала чата
    }


def task_card(task: Task, events: Sequence[TaskEvent], viewer: User, now: datetime) -> dict[str, Any]:
    """TaskCard: задача, сдачи (старые первыми) и журнал — с правилами видимости оценки AI."""
    manager_view = viewer.is_manager
    subs = sorted(task.submissions, key=lambda s: (s.attempt, s.id))
    return {
        "task": task_detail(task, viewer, now),
        "submissions": [submission(task, sub, manager_view=manager_view) for sub in subs],
        "events": [event(ev) for ev in visible_events(task, events, viewer)],
        "viewer": "manager" if manager_view else "assignee",
    }


# --- KPI --------------------------------------------------------------------------------------------------


def period(p: Period, max_back: int) -> dict[str, Any]:
    return {
        "kind": p.kind,
        "offset": p.offset,
        "label": _t(p.label),
        "short": _t(p.short),
        "start": iso(p.start),
        "end": iso(p.end),
        "has_prev": p.offset > -max_back,
        "has_next": p.offset < 0,
    }


def kpi_stats(res: KpiResult) -> dict[str, Any]:
    return {
        "total": res.total,
        "done": res.done,
        "done_on_time": res.done_on_time,
        "done_late": res.done_late,
        "overdue_open": res.overdue_open,
        "overdue_total": res.overdue_total,
        "on_review": res.on_review,
        "on_review_late": res.on_review_late,
        "in_progress": res.in_progress,
        "overperformed": res.overperformed,
        "self_initiated": res.self_initiated,
        "avg_score": res.avg_score,
        "on_time_pct": res.on_time_pct,
    }


def kpi_short(res: KpiResult) -> dict[str, Any]:
    return {"kpi": res.kpi, "kpi_text": kpi_text(res.kpi)}


def kpi_block(res: KpiResult) -> dict[str, Any]:
    return {
        **kpi_short(res),
        "stats": kpi_stats(res),
        "items": [
            {
                "task_id": item.task_id,
                "title": item.title,
                "weight": item.weight,
                "score": item.score,
                "zero_overdue": item.zero_overdue,
            }
            for item in res.items
        ],
    }


def trend(points: Sequence[tuple[Period, float | None]]) -> dict[str, Any]:
    """Тренд по неделям (от старой к текущей): местный понедельник «28.09» и KPI недели."""
    return {
        "weeks": len(points),
        "points": [{"start": iso(p.start), "label": _dm(p.start), "kpi": value} for p, value in points],
    }


def history_item(row: HistoryRowData) -> dict[str, Any]:
    decision = ReviewDecision(row.decision) if row.decision is not None else None
    return {
        "task_id": row.task_id,
        "title": row.title,
        "weight": row.weight,
        "completed_at": iso(row.completed_at),
        "completed_local": _dm(row.completed_at),
        "ai_score": row.ai_score,
        "final_score": row.final_score,
        "final_score_text": fmt_pct(row.final_score),
        "decision": decision.value if decision in (ReviewDecision.APPROVED, ReviewDecision.CHANGED) else None,
        "is_late": None if row.is_late is None else bool(row.is_late),
        "rework_count": row.rework_count or 0,
    }
