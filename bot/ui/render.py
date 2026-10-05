"""Тексты карточек, списков и отчётов.

Все функции возвращают HTML-строку для parse_mode=HTML; пользовательский текст экранируется.
Длинные поля обрезаются, итоговые сообщения укладываются в лимит Telegram (4096 символов).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.config import get_settings
from bot.db.models import (
    Attachment,
    AttachmentKind,
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
from bot.ui import texts
from bot.utils.dateparse import iso_to_deadline
from bot.utils.dates import fmt_deadline, to_local, utcnow
from bot.utils.text import bar, esc, fmt_num, fmt_pct, plural, truncate

if TYPE_CHECKING:
    from bot.services.kpi import KpiResult
    from bot.services.periods import Period

__all__ = [
    "PRIORITY_LABELS",
    "STATUS_LABELS",
    "status_label",
    "deadline_label",
    "plan_text",
    "task_line",
    "task_card",
    "task_summary_draft",
    "submission_text",
    "review_result_text",
    "events_text",
    "kpi_block",
    "employee_card",
    "team_dashboard",
    "history_text",
    "user_line",
    "help_text",
]

PRIORITY_LABELS: dict[Priority, str] = {
    Priority.HIGH: "🔴 Высокий",
    Priority.MEDIUM: "🟡 Средний",
    Priority.LOW: "🟢 Низкий",
}

STATUS_LABELS: dict[TaskStatus, str] = {
    TaskStatus.PROPOSED: "📥 На подтверждении",
    TaskStatus.ACTIVE: "🔄 В работе",
    TaskStatus.SUBMITTED: "📝 На проверке",
    TaskStatus.REWORK: "↩️ На доработке",
    TaskStatus.DONE: "✅ Выполнена",
    TaskStatus.CANCELLED: "🚫 Отменена",
    TaskStatus.REJECTED: "❌ Отклонена",
}

_OVERDUE_LABEL = "⏰ Просрочена"
_MSG_LIMIT = 4000          # запас до лимита Telegram 4096
_HISTORY_PAGE_SIZE = 10    # совпадает с keyboards.history_kb и services.tasks.evaluated_history
_MAX_LIST_ITEMS = 8        # сколько элементов показывать в коротких перечнях
_SYSTEM_ACTOR = "🤖 Бот"
_RULES_LABEL = "Расчёт по правилам"  # начало обоснования оценки без AI (SPEC 4.3)
# Если сообщение не влезает в лимит, длинные пользовательские поля сжимаются по этим ступеням —
# чтобы обрезался текст сотрудника, а не строки «Сдано … с опозданием», «AI предлагает», «Срок».
_SHRINK_STEPS = (1.0, 0.7, 0.45, 0.25)
_MIN_FIELD = 60

_ROLE_LABELS = {Role.MANAGER: "👔 руководитель", Role.EMPLOYEE: "👤 сотрудник"}
_USER_STATUS_ICONS = {UserStatus.ACTIVE: "🟢", UserStatus.PENDING: "⏳", UserStatus.BLOCKED: "⛔"}
_USER_STATUS_NOTES = {UserStatus.PENDING: "⏳ ждёт подтверждения", UserStatus.BLOCKED: "⛔ заблокирован"}
_ATTACHMENT_NAMES = {
    AttachmentKind.PHOTO: "фото",
    AttachmentKind.VIDEO: "видео",
    AttachmentKind.DOCUMENT: "документ",
    AttachmentKind.OTHER: "файл",
}
_FIELD_LABELS = {
    "title": "название",
    "expected_result": "ожидаемый результат",
    "description": "описание",
    "plan_value": "план",
    "plan_unit": "единица плана",
    "deadline": "срок",
    "priority": "приоритет",
    "weight": "вес",
    "new_deadline": "новый срок",
}
_REMINDER_KINDS = {
    "before_hours": "в день срока",
    "deadline_passed": "срок истёк",
    "overdue_manager": "руководителю о просрочке",
}


# --- Базовые хелперы ---------------------------------------------------------------------------


def _now(now: datetime | None) -> datetime:
    return now or utcnow()


def _cut(text: object, limit: int) -> str:
    """Обрезает «сырой» (ещё не экранированный) текст; короткие поля сворачивает в одну строку."""
    value = " ".join(str(text).split()) if limit <= 120 else str(text).strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _clip(text: object, limit: int) -> str:
    """Обрезать (до limit символов исходного текста) и экранировать пользовательский текст."""
    return esc(_cut(text, limit)) if text is not None else ""


def _clip_long(text: object, limit: int) -> str:
    """Длинное поле (факт, план, комментарий…): не длиннее limit символов уже в HTML.

    Иначе «&» и «<» (в HTML — «&amp;», «&lt;») раздували бы поле в 4–5 раз, и обрезка всего
    сообщения по лимиту Telegram вытесняла бы важные строки (срок, опоздание, оценку).
    Короткие поля (названия, ФИО) обрезаются по исходному тексту — там раздувание не страшно.
    """
    if text is None:
        return ""
    clipped = esc(_cut(text, limit))
    if len(clipped) <= limit:
        return clipped
    value = " ".join(str(text).split()) if limit <= 120 else str(text).strip()
    shortest, longest = 0, min(len(value), limit)
    while shortest < longest:  # самый длинный префикс, который вместе с «…» укладывается в limit
        middle = (shortest + longest + 1) // 2
        if len(esc(value[:middle].rstrip() + "…")) <= limit:
            shortest = middle
        else:
            longest = middle - 1
    return esc(value[:shortest].rstrip() + "…")


def _scaled(limit: int, scale: float) -> int:
    return limit if scale >= 1 else max(int(limit * scale), _MIN_FIELD)


def _fit(build: Callable[[float], list[str]]) -> str:
    """Собрать сообщение, при необходимости сжимая длинные поля (build(scale) -> строки)."""
    text = ""
    for scale in _SHRINK_STEPS:
        text = "\n".join(build(scale)).strip()
        if len(text) <= _MSG_LIMIT:
            return text
    return truncate(text, _MSG_LIMIT)


def _name(user: User | None) -> str:
    return esc(user.short_name) if user is not None else "—"


def _dm(dt_utc: datetime) -> str:
    return to_local(dt_utc).strftime("%d.%m")


def _dm_hm(dt_utc: datetime) -> str:
    return to_local(dt_utc).strftime("%d.%m %H:%M")


def _kpi_text(value: float | None) -> str:
    return fmt_pct(value) if value is not None else "нет данных"


def _span(delta_days: float) -> str:
    """Длительность для «осталось …» / «просрочено на …»: дни, часы или минуты."""
    if delta_days >= 1:
        return f"{math.floor(delta_days)} дн."
    hours = delta_days * 24
    if hours >= 1:
        return f"{math.floor(hours)} ч."
    minutes = math.floor(hours * 60)
    return f"{minutes} мин." if minutes >= 1 else "меньше минуты"


def _days(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 86400


def _is_overdue(task: Task, now: datetime) -> bool:
    return task.is_open and task.deadline < now


def _plan_amount(task: Task) -> str:
    """«100 договоров» (экранировано)."""
    return f"{fmt_num(task.plan_value)} {esc(task.plan_unit or '')}".strip()


def _late_text(sub: Submission) -> str:
    if not sub.is_late:
        return "в срок"
    days = sub.late_days or 0
    return f"с опозданием {fmt_num(days)} дн." if days >= 0.1 else "с опозданием"


def _join_limited(lines: Sequence[str], budget: int, forms: tuple[str, str, str]) -> list[str]:
    """Берёт строки, пока они помещаются в budget символов; остаток — строкой «… и ещё N …»."""
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        if used + len(line) + 1 > budget:
            kept.append(f"… и ещё {plural(len(lines) - index, *forms)}")
            break
        kept.append(line)
        used += len(line) + 1
    return kept


def _finish(lines: Iterable[str]) -> str:
    return truncate("\n".join(lines).strip(), _MSG_LIMIT)


# --- Статус и срок -----------------------------------------------------------------------------


def status_label(task: Task, now: datetime | None = None) -> str:
    """Подпись статуса; для просроченных открытых задач — «⏰ Просрочена»."""
    if _is_overdue(task, _now(now)):
        return _OVERDUE_LABEL
    return STATUS_LABELS.get(task.status, str(task.status))


def _status_icon(task: Task, now: datetime) -> str:
    return status_label(task, now).split(" ", 1)[0]


def deadline_label(task: Task, now: datetime | None = None) -> str:
    """«5 октября (вс), 18:00 · осталось 2 дн.» / «· просрочено на 1 дн.» / «· сдано в срок»."""
    now = _now(now)
    base = fmt_deadline(task.deadline)
    sub = task.last_submission
    if task.is_open or task.status == TaskStatus.PROPOSED:
        if task.deadline >= now:
            return f"{base} · осталось {_span(_days(task.deadline, now))}"
        return f"{base} · просрочено на {_span(_days(now, task.deadline))}"
    if task.status in (TaskStatus.SUBMITTED, TaskStatus.DONE) and sub is not None:
        return f"{base} · сдано {_late_text(sub)}"
    return base


def plan_text(task: Task) -> str:
    """Ожидаемый результат (+ «План: 100 договоров», если задано число)."""
    return _plan_html(task, 1500)


def _plan_html(task: Task, limit: int) -> str:
    text = _clip_long(task.expected_result, limit)
    if task.plan_value is not None:
        text += f"\n📊 План: <b>{_plan_amount(task)}</b>"
    return text


# --- Задача: строка списка и карточка ------------------------------------------------------------


def _due_short(deadline: datetime, now: datetime) -> str:
    """«до 05.10», а для сегодня и завтра — со временем: «сегодня до 18:00»."""
    local = to_local(deadline)
    days = (local.date() - to_local(now).date()).days
    if days == 0:
        return f"сегодня до {local:%H:%M}"
    if days == 1:
        return f"завтра до {local:%H:%M}"
    return f"до {local:%d.%m}"


def _line_tail(task: Task, now: datetime) -> str:
    if task.status == TaskStatus.DONE:
        return f"<b>{fmt_pct(task.final_score)}</b>"
    if task.status == TaskStatus.SUBMITTED:
        sub = task.last_submission
        submitted = task.submitted_at or (sub.created_at if sub else None)
        return f"сдано {_dm(submitted)}" if submitted else "на проверке"
    if task.status in (TaskStatus.CANCELLED, TaskStatus.REJECTED):
        return STATUS_LABELS[task.status].split(" ", 1)[1].lower()
    if _is_overdue(task, now):
        return f"просрочено на {_span(_days(now, task.deadline))}"
    return _due_short(task.deadline, now)


def task_line(task: Task, now: datetime | None = None, with_assignee: bool = False) -> str:
    """Одна строка для списков: «🔄 #12 Анализ договоров — до 05.10 · 👤 Иванов И. И.»."""
    now = _now(now)
    line = f"{_status_icon(task, now)} <b>#{task.id}</b> {_clip(task.title, 70)} — {_line_tail(task, now)}"
    if with_assignee:
        line += f" · 👤 {_name(task.assignee)}"
    return line


def _people_lines(task: Task, show_assignee: bool) -> list[str]:
    lines = [f"👤 Исполнитель: {_name(task.assignee)}"] if show_assignee else []
    if task.source == TaskSource.EMPLOYEE:
        lines.append("✋ Внесена сотрудником (устное поручение)")
        if task.manager is not None:
            lines.append(f"🧑‍💼 Ответственный руководитель: {_name(task.manager)}")
        return lines
    lines.append(f"🧑‍💼 Постановщик: {_name(task.created_by)}")
    if task.manager is not None and task.manager_id != task.created_by_id:
        lines.append(f"🧑‍💼 Ответственный руководитель: {_name(task.manager)}")
    return lines


def _acceptance_line(task: Task, for_assignee: bool = False) -> str | None:
    """for_assignee — карточку читает сам исполнитель: обращаемся к нему, а не о нём в третьем лице."""
    if task.status != TaskStatus.ACTIVE:
        return None
    if task.accepted_at is None:
        return "⏳ Вы ещё не подтвердили получение" if for_assignee else "⏳ Исполнитель ещё не подтвердил получение"
    return f"✔️ Принята в работу: {_dm_hm(task.accepted_at)}"


def _attachment_name(att: Attachment) -> str:
    return att.file_name or _ATTACHMENT_NAMES.get(att.kind, "файл")


def _files_line(attachments: Sequence[Attachment]) -> str:
    if not attachments:
        return "📎 Файлов нет"
    names = [_clip(_attachment_name(att), 40) for att in attachments[:5]]
    if len(attachments) > 5:
        names.append(f"… и ещё {len(attachments) - 5}")
    return f"📎 Файлы ({len(attachments)}): {', '.join(names)}"


def _rules_head(rationale: str) -> tuple[str, str]:
    """«Расчёт по правилам (AI недоступен): текст» -> («Расчёт по правилам (AI недоступен)», «текст»).

    Подпись переносится в строку с оценкой, чтобы не повторять её в обосновании.
    """
    head, sep, rest = rationale.strip().partition(": ")
    if sep and head.startswith(_RULES_LABEL) and len(head) <= 60:
        return head, rest
    return _RULES_LABEL, rationale


def _ai_lines(sub: Submission, *, with_rationale: bool, scale: float = 1.0, task: Task | None = None) -> list[str]:
    if sub.ai_score is None:
        if task is not None and task.status == TaskStatus.SUBMITTED and sub.decision is None:
            # AI ещё думает (до ~2,5 мин): результат с оценкой придёт руководителю отдельным сообщением.
            return ["⏳ Предварительная оценка ещё не рассчитана — пришлю её отдельным сообщением"]
        return ["⏳ Предварительная оценка ещё не рассчитана"]
    rationale = sub.ai_rationale or ""
    if sub.ai_source == "rules":
        label, rationale = _rules_head(rationale)
        lines = [f"📐 {esc(label)}: <b>{fmt_pct(sub.ai_score)}</b>"]
    else:
        lines = [f"🤖 AI предлагает: <b>{fmt_pct(sub.ai_score)}</b>"]
    if with_rationale and rationale:
        lines.append(f"<i>{_clip_long(rationale, _scaled(1200, scale))}</i>")
    return lines


def _decision_lines(sub: Submission, scale: float = 1.0) -> list[str]:
    if sub.decision is None:
        return []
    if sub.decision == ReviewDecision.REWORK:
        lines = ["↩️ Возвращено на доработку"]
    else:
        how = "подтверждена" if sub.decision == ReviewDecision.APPROVED else "изменена руководителем"
        lines = [f"🏁 Итоговая оценка: <b>{fmt_pct(sub.final_score)}</b> — {how}"]
    if sub.reviewer is not None and sub.reviewed_at is not None:
        lines.append(f"🧑‍💼 Проверка: {_name(sub.reviewer)}, {_dm_hm(sub.reviewed_at)}")
    if sub.review_comment:
        lines.append(f"💬 Комментарий руководителя: {_clip_long(sub.review_comment, _scaled(800, scale))}")
    return lines


def _fact_value_line(task: Task, sub: Submission) -> str | None:
    """«🔢 План: 100 договоров → Факт: 110 договоров (110 %)»."""
    if sub.fact_value is None:
        if task.plan_value is None:
            return None
        return f"🔢 План: <b>{_plan_amount(task)}</b> · факт числом не указан"
    fact = f"{fmt_num(sub.fact_value)} {esc(task.plan_unit or '')}".strip()
    if task.plan_value is None:
        return f"🔢 Факт: <b>{fact}</b>"
    ratio = f" ({fmt_pct(sub.fact_value / task.plan_value * 100)})" if task.plan_value > 0 else ""
    return f"🔢 План: <b>{_plan_amount(task)}</b> → Факт: <b>{fact}</b>{ratio}"


def _submission_lines(task: Task, sub: Submission, *, show_ai: bool, scale: float = 1.0) -> list[str]:
    lines = [
        f"📤 <b>Сдача результата</b> (попытка {sub.attempt}) · {_dm_hm(sub.created_at)} — {_late_text(sub)}",
        f"✅ Факт: {_clip_long(sub.fact_text, _scaled(800, scale))}",
    ]
    if sub.result_text:
        lines.append(f"📈 Результат: {_clip_long(sub.result_text, _scaled(600, scale))}")
    if (fact_line := _fact_value_line(task, sub)) is not None:
        lines.append(fact_line)
    if sub.attachments:
        lines.append(f"📎 Файлов: {len(sub.attachments)}")
    # Исполнитель видит предложение AI только после решения руководителя.
    ai_visible = show_ai or sub.decision is not None
    if ai_visible and (sub.ai_score is not None or sub.decision is None):
        lines += _ai_lines(sub, with_rationale=False, task=task)
    return lines + _decision_lines(sub, scale)


def task_card(task: Task, now: datetime | None = None, *, show_assignee: bool = True) -> str:
    """Карточка задачи.

    show_assignee=False — вид для исполнителя: без строки «Исполнитель» и без предварительной
    оценки AI, пока руководитель не принял решение.
    """
    now = _now(now)
    return _fit(lambda scale: _task_card_lines(task, now, show_assignee, scale))


def _task_card_lines(task: Task, now: datetime, show_assignee: bool, scale: float) -> list[str]:
    lines = [f"📌 <b>Задача #{task.id}</b>: {_clip(task.title, 255)}", ""]
    lines += _people_lines(task, show_assignee)
    lines += ["", "🎯 <b>Ожидаемый результат:</b>", _plan_html(task, _scaled(1500, scale))]
    if task.description and task.description.strip() != task.expected_result.strip():
        lines.append(f"💬 Описание: {_clip_long(task.description, _scaled(600, scale))}")
    lines += ["", f"📅 Срок: {deadline_label(task, now)}"]
    if task.status == TaskStatus.PROPOSED:
        # В БД у предложения временные вес и приоритет — не выдавать их за выбор сотрудника.
        lines.append("⚖️ Вес и приоритет: назначит руководитель при подтверждении")
    else:
        lines += [
            f"⚡ Приоритет: {PRIORITY_LABELS.get(task.priority, str(task.priority))}",
            f"⚖️ Вес: {task.weight} %",
        ]
    lines.append(f"📍 Статус: {status_label(task, now)}")
    if (accepted := _acceptance_line(task, for_assignee=not show_assignee)) is not None:
        lines.append(accepted)
    if task.rework_count:
        lines.append(f"↩️ Возвратов на доработку: {task.rework_count}")
    sub = task.last_submission
    if sub is not None:
        lines += ["", *_submission_lines(task, sub, show_ai=show_assignee, scale=scale)]
    elif task.status == TaskStatus.DONE and task.final_score is not None:
        lines += ["", f"🏁 Итоговая оценка: <b>{fmt_pct(task.final_score)}</b>"]
    return lines


# --- Черновик задачи (FSM) -----------------------------------------------------------------------


def _draft_assignee(data: dict[str, Any]) -> str | None:
    for key in ("assignee_name", "assignee"):
        value = data.get(key)
        if isinstance(value, User):
            return value.full_name
        if isinstance(value, str) and value.strip():
            return value
    return None


def _coerce_deadline(value: object) -> datetime | None:
    """Срок из FSM: datetime (naive UTC), ISO-дата «2026-10-05» или ISO-время."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value) == 10:
            return iso_to_deadline(value)
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def _coerce_float(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _priority_label(value: object) -> str:
    try:
        return PRIORITY_LABELS[Priority(value)]
    except ValueError:
        return esc(value)


def task_summary_draft(data: dict[str, Any]) -> str:
    """Сводка черновика задачи перед сохранением (ключи как у аргументов create_task/propose_task).

    Дополнительно понимает assignee_name (или assignee: User | str) — имя исполнителя.
    Отсутствующие поля пропускаются (у поручения сотрудника нет веса и приоритета).
    """
    lines = ["📋 <b>Проверьте задачу</b>", ""]
    if (assignee := _draft_assignee(data)) is not None:
        lines.append(f"👤 Исполнитель: {_clip(assignee, 200)}")
    lines.append(f"📌 Задача: {_clip(data.get('title') or '—', 255)}")
    expected = data.get("expected_result") or "—"
    lines += ["🎯 <b>Ожидаемый результат:</b>", _clip(expected, 1500)]
    if (plan_value := _coerce_float(data.get("plan_value"))) is not None:
        amount = f"{fmt_num(plan_value)} {esc(data.get('plan_unit') or '')}".strip()
        lines.append(f"📊 План: <b>{amount}</b>")
    description = data.get("description")
    if description and str(description).strip() != str(expected).strip():
        lines.append(f"💬 Описание: {_clip(description, 600)}")
    lines.append("")
    deadline = _coerce_deadline(data.get("deadline"))
    lines.append(f"📅 Срок: {fmt_deadline(deadline) if deadline else '—'}")
    if data.get("priority") is not None:
        lines.append(f"⚡ Приоритет: {_priority_label(data['priority'])}")
    if data.get("weight") is not None:
        lines.append(f"⚖️ Вес: {esc(data['weight'])} %")
    return _finish(lines)


# --- Сдача и проверка ------------------------------------------------------------------------------


def submission_text(task: Task, sub: Submission) -> str:
    """Для руководителя: План ↔ Факт, когда сдано, файлы, предложение AI с обоснованием."""
    return _fit(lambda scale: _submission_text_lines(task, sub, scale))


def _submission_text_lines(task: Task, sub: Submission, scale: float) -> list[str]:
    lines = [
        f"📝 <b>Результат по задаче #{task.id}</b>",
        f"<b>{_clip(task.title, 255)}</b>",
        f"👤 {_name(task.assignee)} · попытка {sub.attempt} · вес {task.weight} %",
        "",
        f"🎯 <b>План:</b> {_clip_long(task.expected_result, _scaled(1000, scale))}",
        f"✅ <b>Факт:</b> {_clip_long(sub.fact_text, _scaled(1000, scale))}",
    ]
    if sub.result_text:
        lines.append(f"📈 <b>Результат:</b> {_clip_long(sub.result_text, _scaled(700, scale))}")
    if (fact_line := _fact_value_line(task, sub)) is not None:
        lines.append(fact_line)
    lines += [
        "",
        f"📅 Срок: {fmt_deadline(sub.deadline_at_submit)}",
        f"📤 Сдано: {_dm_hm(sub.created_at)} — {_late_text(sub)}",
        _files_line(sub.attachments),
        "",
        *_ai_lines(sub, with_rationale=True, scale=scale, task=task),
    ]
    if sub.decision is None:
        lines.append("Окончательное решение — за руководителем.")
    else:
        lines += ["", *_decision_lines(sub, scale)]
    return lines


def review_result_text(task: Task, sub: Submission) -> str:
    """Для сотрудника: итог проверки — оценка, решение, комментарий руководителя."""
    title = f"<b>{_clip(task.title, 255)}</b>"
    comment = [f"💬 Комментарий руководителя: {_clip_long(sub.review_comment, 1500)}"] if sub.review_comment else []
    if sub.decision == ReviewDecision.REWORK:
        return _finish([
            f"↩️ <b>Задача #{task.id} возвращена на доработку</b>",
            title,
            "",
            *comment,
            f"📅 Срок: {deadline_label(task)}",
            "Доработайте результат и сдайте его снова.",
        ])
    if sub.decision is None:
        return _finish([f"⏳ <b>Результат по задаче #{task.id} на проверке</b>", title])
    score = sub.final_score if sub.final_score is not None else task.final_score
    verdict = (
        "✅ Руководитель подтвердил предварительную оценку."
        if sub.decision == ReviewDecision.APPROVED
        else "✏️ Оценку выставил руководитель."
    )
    return _finish([
        f"🏁 <b>Результат по задаче #{task.id} оценён</b>",
        title,
        "",
        f"Итоговая оценка: <b>{fmt_pct(score)}</b>",
        verdict,
        *comment,
    ])


# --- Журнал событий --------------------------------------------------------------------------------


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def _fmt_value(field: str, value: object) -> str:
    """Значение поля из журнала в читаемом виде (экранировано)."""
    if value is None or value == "":
        return "—"
    if field in ("deadline", "new_deadline"):
        parsed = _parse_dt(value)
        return _dm_hm(parsed) if parsed else _clip(value, 40)
    if field == "priority":
        return _priority_label(value)
    if field == "weight":
        return f"{esc(value)} %"
    if field in ("plan_value", "score", "ai_score", "final_score"):
        number = _coerce_float(value)
        if number is None:
            return _clip(value, 40)
        return fmt_pct(number) if field != "plan_value" else fmt_num(number)
    return f"«{_clip(value, 60)}»"


def _details(data: dict[str, Any], *fields: str) -> str:
    """« · срок 05.10 18:00 · вес 20 %» по имеющимся в data полям."""
    parts = [
        f"{_FIELD_LABELS.get(field, field)} {_fmt_value(field, data[field])}"
        for field in fields
        if data.get(field) not in (None, "")
    ]
    return "".join(f" · {part}" for part in parts)


def _reason(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if data.get(key):
            return f": {_clip(data[key], 200)}"
    return ""


def _edited_phrase(data: dict[str, Any]) -> str:
    changes = data.get("changes")
    if not isinstance(changes, dict) or not changes:
        return "изменены данные задачи"
    parts = []
    for field, change in changes.items():
        if isinstance(change, (list, tuple)) and len(change) == 2:
            old, new = change
        else:
            old, new = None, change
        label = _FIELD_LABELS.get(field, esc(field))
        parts.append(f"{label}: {_fmt_value(field, old)} → {_fmt_value(field, new)}")
    return "изменено — " + "; ".join(parts)


def _submitted_phrase(data: dict[str, Any]) -> str:
    phrase = "результат сдан"
    if data.get("attempt"):
        phrase += f" (попытка {esc(data['attempt'])})"
    if data.get("is_late"):
        days = _coerce_float(data.get("late_days"))
        phrase += f", с опозданием {fmt_num(days)} дн." if days else ", с опозданием"
    if data.get("files"):
        phrase += f", файлов: {esc(data['files'])}"
    return phrase


def _ai_phrase(data: dict[str, Any]) -> str:
    score = _fmt_value("score", data.get("score", data.get("ai_score")))
    if data.get("source") == "rules":
        return f"расчёт по правилам: {score}"
    return f"предварительная оценка AI: {score}"


def _reminder_phrase(data: dict[str, Any]) -> str:
    kind = str(data.get("kind") or "")
    if kind.startswith("before_") and kind.endswith("d"):
        detail = f"за {kind[len('before_'):-1]} дн. до срока"
    elif kind.startswith("overdue_"):
        detail = _REMINDER_KINDS.get(kind, "о просрочке")
    elif kind.startswith("review_"):
        detail = "руководителю о непроверенном результате"
    else:
        detail = _REMINDER_KINDS.get(kind, "")
    return f"напоминание ({detail})" if detail else "напоминание"


def _event_phrase(event: TaskEvent) -> str:
    data = event.data if isinstance(event.data, dict) else {}
    match event.type:
        case EventType.CREATED:
            return "задача поставлена" + _details(data, "deadline", "weight", "priority")
        case EventType.PROPOSED:
            return "поручение внесено сотрудником" + _details(data, "deadline")
        case EventType.APPROVED:
            return "поручение подтверждено" + _details(data, "weight", "priority")
        case EventType.REJECTED:
            return "поручение отклонено" + _reason(data, "reason", "comment")
        case EventType.ACCEPTED:
            return "задача принята в работу"
        case EventType.EDITED:
            return _edited_phrase(data)
        case EventType.SUBMITTED:
            return _submitted_phrase(data)
        case EventType.AI_EVALUATED:
            return _ai_phrase(data)
        case EventType.SCORE_CONFIRMED:
            return f"оценка подтверждена: {_fmt_value('score', data.get('score', data.get('final_score')))}"
        case EventType.SCORE_CHANGED:
            old = _fmt_value("ai_score", data.get("ai_score"))
            new = _fmt_value("score", data.get("score", data.get("final_score")))
            comment = f" · комментарий: {_clip(data['comment'], 200)}" if data.get("comment") else ""
            return f"оценка изменена: {old} → {new}{comment}"
        case EventType.REWORK:
            return "возвращено на доработку" + _reason(data, "comment") + _details(data, "new_deadline")
        case EventType.CANCELLED:
            return "задача отменена" + _reason(data, "reason", "comment")
        case EventType.REMINDER:
            return _reminder_phrase(data)
    return esc(event.type)


def _event_line(event: TaskEvent) -> str:
    actor = _name(event.actor) if event.actor is not None else _SYSTEM_ACTOR
    return f"▫️ {_dm_hm(event.created_at)} — {actor}: {_event_phrase(event)}"


def events_text(task: Task, events: list[TaskEvent]) -> str:
    """История задачи: «04.10 18:20 — Иванов И. И.: результат сдан (попытка 1)». Новые — внизу."""
    header = [f"📜 <b>История задачи #{task.id}</b>", _clip(task.title, 200), ""]
    if not events:
        return _finish([*header, "Событий пока нет."])
    lines = [_event_line(event) for event in events]
    budget = _MSG_LIMIT - sum(len(line) + 1 for line in header) - 60
    kept: list[str] = []
    used = 0
    for line in reversed(lines):  # при нехватке места оставляем самые свежие события
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    skipped = len(lines) - len(kept)
    if skipped:
        kept.insert(0, f"… ранее ещё {plural(skipped, 'событие', 'события', 'событий')}")
    return _finish([*header, *kept])


# --- KPI ---------------------------------------------------------------------------------------------


def _kpi_stat_lines(res: KpiResult) -> list[str]:
    """Показатели периода — как в примере ТЗ."""
    if not res.total:
        return ["Задач в этом периоде нет."]
    return [
        f"✅ Выполнено задач: {res.done} из {res.total}",
        f"⏰ Просрочено: {res.overdue_total}",
        f"🎯 Выполнение в срок: {fmt_pct(res.on_time_pct)}",
        f"🚀 Перевыполнено: {res.overperformed}",
        f"✋ Внесено самостоятельно: {res.self_initiated}",
        f"🔄 В работе: {res.in_progress} · 📝 На проверке: {res.on_review}",
    ]


def kpi_block(title: str, res: KpiResult) -> str:
    """«Неделя: 102 %» + показатели; при kpi None — «нет оценённых задач»."""
    if res.kpi is None:
        head = f"<b>{esc(title)}:</b> нет оценённых задач"
    else:
        head = f"<b>{esc(title)}: {fmt_pct(res.kpi)}</b>\n{bar(res.kpi)}"
    return "\n".join([head, *_kpi_stat_lines(res)])


def _kpi_items_lines(res: KpiResult) -> list[str]:
    """Что вошло в расчёт: «• Анализ договоров — вес 20 % × 110 %»."""
    if not res.items:
        return []
    lines: list[str] = []
    for item in res.items[:_MAX_LIST_ITEMS]:
        mark = " ⏰ просрочена" if item.zero_overdue else ""
        lines.append(f"• {_clip(item.title, 40)} — вес {item.weight} % × {fmt_pct(item.score)}{mark}")
    if len(res.items) > _MAX_LIST_ITEMS:
        lines.append(f"… и ещё {plural(len(res.items) - _MAX_LIST_ITEMS, 'задача', 'задачи', 'задач')}")
    return ["", "🧮 <b>Вошли в расчёт:</b>", *lines]


def employee_card(
    user: User,
    week: KpiResult,
    month: KpiResult,
    period: Period | None = None,
    current: KpiResult | None = None,
) -> str:
    """Карточка эффективности сотрудника: KPI выбранного периода, показатели, неделя и месяц."""
    selected = current if current is not None else week
    lines = [f"👤 <b>{_clip(user.full_name, 200)} — {_kpi_text(selected.kpi)}</b>"]
    if user.position:
        lines.append(f"💼 {_clip(user.position, 200)}")
    lines.append(f"📅 {esc(period.label) if period is not None else 'Текущая неделя'}")
    if selected.kpi is not None:
        lines.append(bar(selected.kpi))
    lines += ["", *_kpi_stat_lines(selected), *_kpi_items_lines(selected)]
    lines += ["", f"📊 Неделя: <b>{fmt_pct(week.kpi)}</b> · Месяц: <b>{fmt_pct(month.kpi)}</b>"]
    return _finish(lines)


def _team_entry(index: int, user: User, res: KpiResult) -> str:
    head = f"{index}. {_name(user)} — <b>{_kpi_text(res.kpi)}</b>"
    if not res.total:
        return f"{head}\n      задач нет"
    counters = f"✅ {res.done} · ⏰ {res.overdue_total} · 🔄 {res.in_progress} · 📝 {res.on_review}"
    gauge = f"{bar(res.kpi, 8)}  " if res.kpi is not None else ""
    return f"{head}\n      {gauge}{counters}"


def team_dashboard(period: Period, rows: list[tuple[User, KpiResult]], team_value: float | None) -> str:
    """Дашборд команды: KPI команды, итоги и строка на каждого сотрудника."""
    lines = [
        f"📊 <b>Команда · {esc(period.label)}</b>",
        f"Эффективность команды: <b>{_kpi_text(team_value)}</b>",
    ]
    if rows:
        done, overdue, in_progress, on_review = (
            sum(getattr(res, name) for _, res in rows)
            for name in ("done", "overdue_total", "in_progress", "on_review")
        )
        lines.append(f"Итого: ✅ {done} · ⏰ {overdue} · 🔄 {in_progress} · 📝 {on_review}")
    lines.append("")
    if not rows:
        lines.append("Активных сотрудников пока нет.")
    else:
        entries = [_team_entry(index, user, res) for index, (user, res) in enumerate(rows, start=1)]
        lines += _join_limited(entries, _MSG_LIMIT - 400, ("сотрудник", "сотрудника", "сотрудников"))
    lines += ["", "<i>✅ выполнено · ⏰ просрочено · 🔄 в работе · 📝 на проверке</i>"]
    return _finish(lines)


def history_text(user: User, tasks: list[Task], page: int, total: int) -> str:
    """История оценок сотрудника: оценка AI → итоговая оценка руководителя, решение."""
    lines = [f"📜 <b>История оценок · {_name(user)}</b>"]
    if not tasks or not total:
        return _finish([*lines, "", "Оценённых задач пока нет."])
    pages = max(1, math.ceil(total / _HISTORY_PAGE_SIZE))
    lines.append(f"Оценено задач: {total} · стр. {min(page + 1, pages)} из {pages}")
    for task in tasks:
        sub = task.last_submission
        ai_score = task.ai_score if task.ai_score is not None else (sub.ai_score if sub else None)
        done_at = task.completed_at or (sub.reviewed_at if sub else None)
        meta = [_dm(done_at) if done_at else "", f"вес {task.weight} %"]
        if sub is not None and sub.is_late:
            meta.append("⚠️ с опозданием")
        if task.rework_count:
            meta.append(f"↩️ доработок: {task.rework_count}")
        verdict = ""
        if sub is not None and sub.decision == ReviewDecision.APPROVED:
            verdict = " ✅ подтверждена"
        elif sub is not None and sub.decision == ReviewDecision.CHANGED:
            verdict = " ✏️ изменена"
        lines += [
            "",
            f"<b>#{task.id}</b> {_clip(task.title, 80)}",
            f"   {' · '.join(part for part in meta if part)}",
            f"   🤖 {fmt_pct(ai_score)} → 🏁 <b>{fmt_pct(task.final_score)}</b>{verdict}",
        ]
    return _finish(lines)


# --- Пользователи и справка ------------------------------------------------------------------------


def user_line(user: User) -> str:
    """«🟢 Иванов Иван Иванович · сотрудник · Специалист · @ivanov»."""
    parts = [
        f"{_USER_STATUS_ICONS.get(user.status, '•')} <b>{_clip(user.full_name or '—', 120)}</b>",
        _ROLE_LABELS.get(user.role, esc(user.role)),
    ]
    if user.position:
        parts.append(_clip(user.position, 80))
    if user.username:
        parts.append(f"@{esc(user.username)}")
    if user.status in _USER_STATUS_NOTES:
        parts.append(_USER_STATUS_NOTES[user.status])
    return " · ".join(parts)


_CYCLE = (
    "<b>ПОРУЧЕНИЕ → ПЛАНОВЫЙ РЕЗУЛЬТАТ → СРОК → ФАКТИЧЕСКИЙ РЕЗУЛЬТАТ → "
    "ПРОВЕРКА → ОЦЕНКА → КОЭФФИЦИЕНТ ЭФФЕКТИВНОСТИ</b>"
)


def _kpi_help() -> list[str]:
    lines = [
        "🧮 <b>Коэффициент эффективности</b>",
        "KPI = Σ(вес × оценка) ÷ Σ(вес) по оценённым задачам периода.",
        "Пример: веса 30/20/20/30 % и оценки 100/110/90/105 % → <b>102 %</b>.",
        "Считается за неделю, месяц, квартал и год по одной методике.",
    ]
    if get_settings().overdue_counts_as_zero:
        lines.append("Просроченная и не сданная задача входит в расчёт как 0 %.")
    return lines


def _manager_help() -> list[str]:
    return [
        "<b>Ваши кнопки</b>",
        f"{texts.BTN_NEW_TASK} — сотрудник → задача → ожидаемый результат → срок → приоритет → вес. "
        "Бот поможет сформулировать измеримый результат.",
        f"{texts.BTN_PROPOSALS} — поручения, внесённые сотрудниками: подтвердить, изменить или отклонить.",
        f"{texts.BTN_REVIEW} — сданные результаты: план ↔ факт, файлы и предварительная оценка AI. "
        "Можно подтвердить, изменить оценку или вернуть на доработку.",
        f"{texts.BTN_TASKS} — все задачи: в работе, просроченные, на проверке, выполненные.",
        f"{texts.BTN_TEAM} — эффективность каждого сотрудника и команды.",
        f"{texts.BTN_STAFF} — заявки на доступ, роли, блокировка.",
        f"{texts.BTN_EXPORT} — отчёт в Excel за неделю, месяц, квартал или год.",
        "",
        "🤖 AI только предлагает оценку — окончательное решение всегда за вами.",
    ]


def _employee_help() -> list[str]:
    return [
        "<b>Ваши кнопки</b>",
        f"{texts.BTN_MY_TASKS} — ваши задачи, сроки и статусы. Подтвердите получение новой задачи.",
        f"{texts.BTN_PROPOSE} — внести поручение, полученное устно: руководитель подтвердит его.",
        f"{texts.BTN_SUBMIT} — что фактически сделано, какой получен результат, файлы-подтверждения.",
        f"{texts.BTN_MY_KPI} — ваш коэффициент за неделю, месяц, квартал и год.",
        "",
        "⏰ Бот напомнит о приближении срока, а после срока попросит сдать результат.",
        "Оценку ставит руководитель; AI лишь помогает сравнить план и факт.",
    ]


def help_text(user: User | None) -> str:
    """Справка: цикл задачи, кнопки по роли, формула KPI, команды."""
    lines = [
        "ℹ️ <b>Как работает бот</b>",
        "",
        "Каждая задача проходит один цикл:",
        _CYCLE,
        "",
        "Оценивается не количество сообщений или часов, а <b>конечный результат</b>.",
        "",
    ]
    if user is None or not user.is_active:
        if user is not None and user.status == UserStatus.BLOCKED:
            access = "⛔ Доступ закрыт руководителем — по вопросам обратитесь к нему."
        elif user is not None and user.full_name:
            # Заявка уже отправлена: не предлагать «отправить заявку» ещё раз.
            access = "⏳ Заявка на рассмотрении у руководителя — после подтверждения здесь появится меню."
        else:
            access = "Чтобы начать, нажмите /start и отправьте заявку — доступ откроет руководитель."
        lines += [access, "", *_kpi_help()]
        return _finish(lines)
    lines += _manager_help() if user.role == Role.MANAGER else _employee_help()
    lines += [
        "",
        *_kpi_help(),
        "",
        "⌨️ Команды: /menu — меню, /cancel — отменить действие, /help — справка.",
    ]
    return _finish(lines)
