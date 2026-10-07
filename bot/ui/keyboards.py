"""Клавиатуры: главное меню (reply) и inline-кнопки с callback-фабриками из bot.ui.callbacks."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from bot.config import get_settings
from bot.db.models import OPEN_STATUSES, Priority, Role, Submission, Task, TaskStatus, User, UserStatus
from bot.ui import texts
from bot.ui.callbacks import ListCB, PeriodCB, PickCB, SubCB, TaskCB, UserCB
from bot.ui.render import PRIORITY_LABELS, status_label
from bot.utils.dateparse import quick_deadline_options
from bot.utils.dates import utcnow
from bot.utils.text import fmt_pct

if TYPE_CHECKING:
    from bot.services.kpi import KpiResult

__all__ = [
    "BTN_OPEN_APP",
    "main_menu",
    "open_app_kb",
    "cancel_kb",
    "skip_cancel_kb",
    "choose_user_kb",
    "deadline_kb",
    "priority_kb",
    "weight_kb",
    "ai_suggestion_kb",
    "confirm_kb",
    "edit_fields_kb",
    "score_kb",
    "files_kb",
    "task_actions_kb",
    "new_task_kb",
    "submit_kb",
    "proposal_kb",
    "review_kb",
    "registration_kb",
    "user_manage_kb",
    "period_kb",
    "team_kb",
    "employee_card_kb",
    "task_list_kb",
    "history_kb",
    "export_kb",
]

_Row = list[InlineKeyboardButton]

_CANCEL_TEXT = "✖️ Отмена"
# Inline-кнопка приложения в Telegram (Mini App) — после приветствия /start (только режим webhook).
BTN_OPEN_APP = "📱 Открыть приложение"
_WEIGHT_OPTIONS = (5, 10, 15, 20, 25, 30, 40, 50)
_SCORE_OPTIONS = (50, 70, 80, 90, 100, 110, 120)
_PERIOD_KINDS = (("week", "Неделя"), ("month", "Месяц"), ("quarter", "Квартал"), ("year", "Год"))
_TASK_LIST_TABS = (
    ("open", "В работе"),
    ("overdue", "Просрочены"),
    ("review", "На проверке"),
    ("done", "Выполнены"),
    ("all", "Все"),
)
_EXPORT_OPTIONS = (
    ("Эта неделя", "week", 0),
    ("Прошлая неделя", "week", -1),
    ("Этот месяц", "month", 0),
    ("Прошлый месяц", "month", -1),
    ("Квартал", "quarter", 0),
    ("Год", "year", 0),
)
# В этих списках задачи разных сотрудников — в подписи кнопки нужна фамилия исполнителя.
_SCOPES_WITH_ASSIGNEE = frozenset({"all", "review", "proposals"})
_BUTTON_TEXT_LIMIT = 48
_MAX_USER_BUTTONS = 60  # у Telegram есть предел на число кнопок в одном сообщении


# --- Хелперы -----------------------------------------------------------------------------------


def _btn(text: str, callback: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback.pack())


def _chunk(buttons: Sequence[InlineKeyboardButton], size: int) -> list[_Row]:
    return [list(buttons[i : i + size]) for i in range(0, len(buttons), size)]


def _cancel_row() -> _Row:
    return [_btn(_CANCEL_TEXT, PickCB(field="cancel"))]


def _markup(rows: Sequence[_Row]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def _short(text: str, limit: int = _BUTTON_TEXT_LIMIT) -> str:
    """Подпись кнопки в одну строку (inline-кнопки — простой текст, без HTML)."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _marked(label: str, selected: bool) -> str:
    return f"• {label}" if selected else label


def _pick(text: str, field: str, value: str) -> InlineKeyboardButton:
    return _btn(text, PickCB(field=field, value=value))


# --- Главное меню и общие кнопки диалогов ------------------------------------------------------


def main_menu(user: User | None) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    """Меню по роли; неактивному или неизвестному пользователю меню убирается."""
    if user is None or not user.is_active:
        return ReplyKeyboardRemove()
    layout = texts.MANAGER_MENU_LAYOUT if user.role == Role.MANAGER else texts.EMPLOYEE_MENU_LAYOUT
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in layout],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие в меню",
    )


def open_app_kb(url: str) -> InlineKeyboardMarkup:
    """[📱 Открыть приложение] — открывает Mini App по адресу url (Settings.webapp_url, только https)."""
    return _markup([[InlineKeyboardButton(text=BTN_OPEN_APP, web_app=WebAppInfo(url=url))]])


def cancel_kb() -> InlineKeyboardMarkup:
    return _markup([_cancel_row()])


def skip_cancel_kb(field: str = "skip") -> InlineKeyboardMarkup:
    """[⏭ Пропустить] = PickCB(field=field, value="skip") и [✖️ Отмена]."""
    return _markup([[_pick("⏭ Пропустить", field, "skip"), *_cancel_row()]])


def choose_user_kb(users: list[User], field: str = "assignee") -> InlineKeyboardMarkup:
    """Выбор сотрудника: PickCB(field, str(user.id)), по 2 в ряд."""
    buttons = [_pick(_short(user.short_name, 32), field, str(user.id)) for user in users]
    return _markup([*_chunk(buttons, 2), _cancel_row()])


def deadline_kb() -> InlineKeyboardMarkup:
    """Быстрый выбор срока: PickCB("deadline", "YYYY-MM-DD")."""
    buttons = [_pick(label, "deadline", iso) for label, iso in quick_deadline_options()]
    return _markup([*_chunk(buttons, 2), _cancel_row()])


def priority_kb() -> InlineKeyboardMarkup:
    buttons = [_pick(PRIORITY_LABELS[priority], "prio", priority.value) for priority in Priority]
    return _markup([buttons, _cancel_row()])


def weight_kb(load: int | None = None) -> InlineKeyboardMarkup:
    """Вес задачи. load — уже набранный вес на неделе: варианты сверх 100 % помечаются «⚠️»."""
    buttons = []
    for weight in _WEIGHT_OPTIONS:
        warn = " ⚠️" if load is not None and load + weight > 100 else ""
        buttons.append(_pick(f"{weight} %{warn}", "weight", str(weight)))
    return _markup([*_chunk(buttons, 4), _cancel_row()])


def ai_suggestion_kb() -> InlineKeyboardMarkup:
    return _markup([
        [_pick("✅ Принять", "ai", "accept"), _pick("🔁 Другой вариант", "ai", "retry")],
        [_pick("✏️ Свой вариант", "ai", "manual"), _pick("📝 Оставить как написал", "ai", "raw")],
        _cancel_row(),
    ])


def confirm_kb(yes_text: str = "✅ Создать", edit: bool = True) -> InlineKeyboardMarkup:
    second = [_pick("✏️ Изменить", "confirm", "edit")] if edit else []
    return _markup([[_pick(yes_text, "confirm", "yes")], [*second, *_cancel_row()]])


def edit_fields_kb(fields: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Выбор поля для правки: PickCB("field", key) по списку (key, подпись)."""
    buttons = [_pick(_short(label, 32), "field", key) for key, label in fields]
    return _markup([*_chunk(buttons, 2), _cancel_row()])


def score_kb(suggested: float | None = None) -> InlineKeyboardMarkup:
    """Быстрые оценки; предложенная AI оценка отмечена «🤖» (и добавляется, если её нет в списке)."""
    options = list(_SCORE_OPTIONS)
    marked: int | None = None
    if suggested is not None:
        marked = max(0, min(get_settings().max_score, math.floor(suggested + 0.5)))
        if marked not in options:
            options = sorted([*options, marked])
    buttons = [_pick(f"🤖 {s} %" if s == marked else f"{s} %", "score", str(s)) for s in options]
    return _markup([*_chunk(buttons, 4), _cancel_row()])


def files_kb(count: int) -> InlineKeyboardMarkup:
    """Пока файлов нет — [📭 Без файлов]; после первого файла — [✅ Готово (N)]. И [✖️ Отмена]."""
    if count > 0:
        main = _pick(f"✅ Готово ({count})", "files", "done")
    else:
        main = _pick("📭 Без файлов", "files", "none")
    return _markup([[main], _cancel_row()])


# --- Задачи --------------------------------------------------------------------------------------


def _task_btn(text: str, action: str, task: Task) -> InlineKeyboardButton:
    return _btn(text, TaskCB(action=action, task_id=task.id))


def _proposal_rows(task: Task) -> list[_Row]:
    return [
        [_task_btn("✅ Подтвердить", "approve", task)],
        [_task_btn("✏️ Изменить", "pedit", task), _task_btn("❌ Отклонить", "reject", task)],
    ]


def _manager_task_rows(task: Task) -> list[_Row]:
    if task.status == TaskStatus.PROPOSED:
        return _proposal_rows(task)
    if task.status in OPEN_STATUSES:
        return [[_task_btn("✏️ Изменить", "edit", task), _task_btn("🚫 Отменить", "cancel", task)]]
    if task.status == TaskStatus.SUBMITTED:
        return [[_task_btn("🔍 Проверить", "review", task)]]
    return []


def _assignee_task_rows(task: Task) -> list[_Row]:
    rows: list[_Row] = []
    if task.status == TaskStatus.ACTIVE and task.accepted_at is None:
        rows.append([_task_btn("✅ Принял в работу", "accept", task)])
    if task.status in OPEN_STATUSES:
        rows.append([_task_btn("📤 Сдать результат", "submit", task)])
    return rows


def task_actions_kb(task: Task, viewer: User) -> InlineKeyboardMarkup:
    """Действия в карточке задачи по роли смотрящего; «📜 История» — всегда."""
    rows: list[_Row] = []
    if viewer.is_manager:
        rows += _manager_task_rows(task)
    if viewer.id == task.assignee_id:
        rows += _assignee_task_rows(task)
    rows.append([_task_btn("📜 История", "history", task)])
    return _markup(rows)


def new_task_kb(task: Task) -> InlineKeyboardMarkup:
    return _markup([[_task_btn("✅ Принял в работу", "accept", task), _task_btn("📋 Открыть", "open", task)]])


def submit_kb(task: Task) -> InlineKeyboardMarkup:
    return _markup([[_task_btn("📤 Сдать результат", "submit", task), _task_btn("📋 Открыть", "open", task)]])


def proposal_kb(task: Task) -> InlineKeyboardMarkup:
    return _markup(_proposal_rows(task))


def review_kb(sub: Submission) -> InlineKeyboardMarkup:
    """[✅ Подтвердить 110 %] (если есть оценка AI) / [✏️ Изменить оценку] [↩ На доработку] / [📎 Файлы (N)]."""
    rows: list[_Row] = []
    if sub.ai_score is not None:
        rows.append([_btn(f"✅ Подтвердить {fmt_pct(sub.ai_score)}", SubCB(action="ok", sub_id=sub.id))])
    rows.append([
        _btn("✏️ Изменить оценку", SubCB(action="change", sub_id=sub.id)),
        _btn("↩ На доработку", SubCB(action="rework", sub_id=sub.id)),
    ])
    if sub.attachments:
        rows.append([_btn(f"📎 Файлы ({len(sub.attachments)})", SubCB(action="files", sub_id=sub.id))])
    return _markup(rows)


# --- Пользователи --------------------------------------------------------------------------------


def _user_btn(text: str, action: str, user: User) -> InlineKeyboardButton:
    return _btn(text, UserCB(action=action, user_id=user.id))


def registration_kb(user: User) -> InlineKeyboardMarkup:
    return _markup([[_user_btn("✅ Подтвердить", "approve", user), _user_btn("❌ Отклонить", "reject", user)]])


def user_manage_kb(target: User, viewer: User) -> InlineKeyboardMarkup:
    """Кнопки по статусу и роли сотрудника. Себя заблокировать или понизить нельзя — кнопок нет."""
    is_self = target.id == viewer.id
    if target.status == UserStatus.PENDING:
        return registration_kb(target)
    if target.status == UserStatus.BLOCKED:
        rows_blocked: list[_Row] = []
        if target.role == Role.EMPLOYEE:
            # Ушедший сотрудник: его прошлые оценки и история нужны и после блокировки.
            rows_blocked.append([_user_btn("📊 Карточка", "card", target)])
        rows_blocked.append([_user_btn("🔓 Разблокировать", "unblock", target)])
        return _markup(rows_blocked)
    rows: list[_Row] = []
    if target.role == Role.EMPLOYEE:
        rows.append([_user_btn("📊 Карточка", "card", target)])
        rows.append([_user_btn("👔 Сделать руководителем", "role_mgr", target)])
    elif not is_self:
        rows.append([_user_btn("👤 Сделать сотрудником", "role_emp", target)])
    if not is_self:
        rows.append([_user_btn("⛔ Заблокировать", "block", target)])
    return _markup(rows)


# --- Периоды и отчёты ----------------------------------------------------------------------------


def _period_rows(scope: str, kind: str, offset: int, user_id: int = 0) -> list[_Row]:
    kinds = [
        _btn(_marked(label, key == kind), PeriodCB(scope=scope, kind=key, offset=0, user_id=user_id))
        for key, label in _PERIOD_KINDS
    ]
    nav = [_btn("◀ Раньше", PeriodCB(scope=scope, kind=kind, offset=offset - 1, user_id=user_id))]
    if offset < 0:
        nav.append(_btn("Позже ▶", PeriodCB(scope=scope, kind=kind, offset=offset + 1, user_id=user_id)))
    return [kinds, nav]


def period_kb(scope: str, kind: str, offset: int, user_id: int = 0) -> InlineKeyboardMarkup:
    """Неделя/Месяц/Квартал/Год (выбранный отмечен «•») и листание ◀ ▶ (▶ — только для прошлых периодов)."""
    return _markup(_period_rows(scope, kind, offset, user_id))


def team_kb(rows: list[tuple[User, KpiResult]], kind: str, offset: int) -> InlineKeyboardMarkup:
    """Переключатель периода команды + кнопка на каждого сотрудника.

    Кнопка открывает карточку сотрудника за тот же период, что на дашборде (PeriodCB("emp")):
    «👤 Иванов — 110 %» из сводки за прошлую неделю показывает именно прошлую неделю, а «◀ К команде»
    в карточке возвращает к ней же.
    """
    people = [
        [_btn(_short(f"👤 {user.short_name}" + (f" — {fmt_pct(res.kpi)}" if res.kpi is not None else "")),
              PeriodCB(scope="emp", kind=kind, offset=offset, user_id=user.id))]
        for user, res in rows[:_MAX_USER_BUTTONS]
    ]
    return _markup([*_period_rows("team", kind, offset), *people])


def employee_card_kb(user: User, kind: str, offset: int, back_to_team: bool) -> InlineKeyboardMarkup:
    rows = _period_rows("emp", kind, offset, user.id)
    rows.append([
        _user_btn("📜 История оценок", "history", user),
        _btn("📋 Задачи", ListCB(scope="emp", status="all", user_id=user.id)),
    ])
    if back_to_team:
        rows.append([_btn("◀ К команде", PeriodCB(scope="team", kind=kind, offset=offset))])
    return _markup(rows)


def export_kb() -> InlineKeyboardMarkup:
    buttons = [
        _btn(label, PeriodCB(scope="export", kind=kind, offset=offset)) for label, kind, offset in _EXPORT_OPTIONS
    ]
    return _markup(_chunk(buttons, 2))


# --- Списки с пагинацией -------------------------------------------------------------------------


def _task_button_text(task: Task, scope: str) -> str:
    icon = status_label(task, utcnow()).split(" ", 1)[0]
    who = ""
    if scope in _SCOPES_WITH_ASSIGNEE and task.assignee is not None:
        surname = (task.assignee.full_name.split() or [""])[0]
        who = f"{surname} · " if surname else ""
    return _short(f"{icon} #{task.id} {who}{task.title}")


def _pager(prev_cb: CallbackData | None, next_cb: CallbackData | None) -> _Row:
    row: _Row = []
    if prev_cb is not None:
        row.append(_btn("◀ Назад", prev_cb))
    if next_cb is not None:
        row.append(_btn("Вперёд ▶", next_cb))
    return row


def task_list_kb(
    tasks: list[Task],
    scope: str,
    status: str,
    page: int,
    total: int,
    user_id: int = 0,
    page_size: int = 8,
    status_tabs: bool = True,
) -> InlineKeyboardMarkup:
    """Кнопка на каждую задачу, пагинация и вкладки статусов (активная отмечена «•»).

    В очереди проверки (scope="review") кнопка сразу открывает проверку — TaskCB("review"),
    в остальных списках — карточку TaskCB("open").
    """
    action = "review" if scope == "review" else "open"
    rows: list[_Row] = [[_task_btn(_task_button_text(task, scope), action, task)] for task in tasks]

    def page_cb(target: int) -> ListCB:
        return ListCB(scope=scope, status=status, page=target, user_id=user_id)

    has_next = (page + 1) * page_size < total
    rows.append(_pager(page_cb(page - 1) if page > 0 else None, page_cb(page + 1) if has_next else None))
    if status_tabs:
        tabs = [
            _btn(_marked(label, key == status), ListCB(scope=scope, status=key, page=0, user_id=user_id))
            for key, label in _TASK_LIST_TABS
        ]
        rows += [tabs[:3], tabs[3:]]
    return _markup(rows)


def history_kb(user_id: int, page: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup:
    """Листание истории оценок (UserCB("history", page=…)) и возврат к карточке сотрудника."""

    def page_cb(target: int) -> UserCB:
        return UserCB(action="history", user_id=user_id, page=target)

    has_next = (page + 1) * page_size < total
    return _markup([
        _pager(page_cb(page - 1) if page > 0 else None, page_cb(page + 1) if has_next else None),
        [_btn("📊 К карточке", UserCB(action="card", user_id=user_id))],
    ])
