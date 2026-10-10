"""Списки задач, карточка задачи, принятие в работу, история, правка и отмена (SPEC 7.5).

* «📋 Мои задачи» /my (сотрудник) и «📋 Задачи» /tasks (начальник) -> ListCB("my"|"all", "open").
* ListCB scope my | all | emp, статусы open | overdue | review | done | all, по 8 задач на страницу.
* TaskCB open | accept | history | edit | cancel.
* Правка задачи — FSM EditTaskSG, отмена задачи — FSM CancelTaskSG.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Awaitable
from datetime import datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notify
from bot.db.models import OPEN_STATUSES, EventType, Priority, Task, TaskEvent, TaskStatus, User
from bot.filters import IsActiveUser, TextInput
from bot.handlers import common
from bot.services import tasks as tasks_svc
from bot.services import users as users_svc
from bot.services.errors import DomainError
from bot.ui import keyboards, render
from bot.ui.callbacks import ListCB, PickCB, SubCB, TaskCB, UserCB
from bot.ui.texts import BTN_MY_TASKS, BTN_TASKS
from bot.utils import dateparse
from bot.utils.dateparse import iso_to_deadline, parse_deadline
from bot.utils.dates import fmt_deadline, to_local, utcnow
from bot.utils.text import esc, fmt_num, parse_number, plural, truncate

log = logging.getLogger(__name__)

router = Router(name="task_view")

PAGE_SIZE = 8
_MSG_LIMIT = 4000
_MAX_REASON = 1000
_MAX_RESULT = 2000  # ожидаемый результат при правке — как у поручений (task_propose.RESULT_MAX)
_MAX_PLAN = 1e15  # больше — явная опечатка (и fmt_num такие числа не покажет)

_LIST_SCOPES = ("my", "all", "emp")
_STATUS_TITLES = {
    "open": "В работе",
    "overdue": "Просрочены",
    "review": "На проверке",
    "done": "Выполнены",
    "all": "Все",
}
# «Все» для сотрудника — без отклонённых и отменённых; начальник видит и отменённые.
_ALL_FOR_EMPLOYEE = (
    TaskStatus.PROPOSED,
    TaskStatus.ACTIVE,
    TaskStatus.REWORK,
    TaskStatus.SUBMITTED,
    TaskStatus.DONE,
)
_ALL_FOR_MANAGER = (*_ALL_FOR_EMPLOYEE, TaskStatus.CANCELLED)

_EDITABLE = (TaskStatus.ACTIVE, TaskStatus.REWORK)
_CANCELLABLE = (TaskStatus.PROPOSED, TaskStatus.ACTIVE, TaskStatus.REWORK, TaskStatus.SUBMITTED)

_EDIT_FIELDS: list[tuple[str, str]] = [
    ("title", "Название"),
    ("result", "Ожидаемый результат"),
    ("plan", "План (число)"),
    ("deadline", "Срок"),
    ("priority", "Приоритет"),
    ("weight", "Вес"),
]
_CHANGE_LABELS = {
    "title": "название",
    "expected_result": "ожидаемый результат",
    "description": "описание",
    "plan_value": "план",
    "plan_unit": "единица плана",
    "deadline": "срок",
    "priority": "приоритет",
    "weight": "вес",
}
_PRIORITY_WORDS = (
    ("выс", Priority.HIGH), ("сред", Priority.MEDIUM), ("низ", Priority.LOW),
    # по-узбекски: «yuqori», «oʻrta», «past»
    ("yuqori", Priority.HIGH), ("baland", Priority.HIGH), ("o'rta", Priority.MEDIUM), ("orta", Priority.MEDIUM),
    ("past", Priority.LOW), ("quyi", Priority.LOW),
)

# Ключи FSM-данных этого модуля.
K_TASK = "tv_task_id"
K_FIELD = "tv_field"
K_PROMPT = "tv_prompt_id"  # id сообщения с текущим вопросом диалога: кнопки других сообщений устарели
K_BACK = "tv_back"         # последний открытый список задач (ListCB.pack()) — для «◀ К списку задач»

_CANCEL_PREFIX = PickCB(field="cancel").pack()
# Вес текстом: «20», «20 %», «20%», «20.0», «вес 20», «20 процентов». Не «1e309» и не «20,5».
_WEIGHT_RE = re.compile(r"(?:вес\s*)?(\d{1,3})(?:[.,]0+)?\s*(?:%|процент\w*)?", re.IGNORECASE)
# Срок дальше — опечатка в годе («31.12.9999» к тому же ломал расчёт недели). Как в dateparse.
_DEADLINE_MAX_YEARS: int = getattr(dateparse, "MAX_YEARS_AHEAD", 5)
_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_MAX_DB_ID = 2**63 - 1  # предел INTEGER в SQLite; больше — только подделанный callback (OverflowError)
_NUMBER_RE = re.compile(r"\d(?:[\d   ]*\d)?(?:[.,]\d+)?")

HINT_BUTTONS = "Выберите вариант кнопкой в сообщении выше 👆 или нажмите «✖️ Отмена»."
# Экраны, которые «📋 Открыть» заменяет карточкой: списки задач и предложений, история, карточка.
# Уведомления и напоминания («⏰ До срока…», «🆕 Вам поставлена…», «⚠️ Просрочена…») не трогаем —
# карточка приходит новым сообщением, а вопросы «Что фактически сделано?…» остаются в чате.
_OWN_SCREENS = ("📋", "📜", "📥", "📌")
HINT_TEXT = "Пришлите ответ текстовым сообщением ✍️ или нажмите «✖️ Отмена»."
STALE_EDIT = "Задачу уже нельзя изменить — её статус изменился."
STALE_CANCEL = "Задачу уже нельзя отменить — она завершена или отменена."
STALE_BUTTON = "Эта кнопка уже неактуальна — продолжите в последнем сообщении."
TOO_FAR = (
    f"Срок слишком далеко. Укажите дату не дальше чем на {plural(_DEADLINE_MAX_YEARS, 'год', 'года', 'лет')} "
    "вперёд — проверьте год."
)


class EditTaskSG(StatesGroup):
    field = State()     # выбор поля
    text = State()      # название / ожидаемый результат / план — текстом
    deadline = State()  # deadline_kb или текст
    priority = State()  # priority_kb
    weight = State()    # weight_kb или число


class CancelTaskSG(StatesGroup):
    confirm = State()  # «Да, отменить» / «Не отменять»
    reason = State()   # причина текстом или «Без причины»


# --- Общие хелперы ----------------------------------------------------------------------------


def _btn(text: str, cb: Any) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


def _cancel_btn() -> InlineKeyboardButton:
    return _btn("✖️ Отмена", PickCB(field="cancel"))


def _with_rows(kb: InlineKeyboardMarkup | None, *rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    base = list(kb.inline_keyboard) if kb is not None else []
    return InlineKeyboardMarkup(inline_keyboard=[*base, *rows])


def _ensure_cancel(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Добавить «✖️ Отмена», если в клавиатуре её ещё нет."""
    for row in kb.inline_keyboard:
        for button in row:
            if button.callback_data and button.callback_data.startswith(_CANCEL_PREFIX):
                return kb
    return _with_rows(kb, [_cancel_btn()])


def _clip(value: str | None, limit: int) -> str:
    """Обрезка обычного (ещё не экранированного) текста."""
    text = (value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fit_tail(text: str, limit: int = _MSG_LIMIT) -> str:
    """Длинную историю сокращаем сверху: заголовок + «…» + последние строки (свежие события)."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    head, tail = lines[0], []
    size = len(head) + 3
    for line in reversed(lines[1:]):
        if size + len(line) + 1 > limit:
            break
        tail.append(line)
        size += len(line) + 1
    if not tail:
        return truncate(text, limit)
    return "\n".join([head, "…", *reversed(tail)])


async def _get_task(session: AsyncSession, task_id: int) -> Task | None:
    """Задача по id из callback; id вне диапазона БД (подделка) — как «не найдена»."""
    if not 0 < task_id <= _MAX_DB_ID:
        return None
    return await tasks_svc.get_task(session, task_id)


async def _get_user(session: AsyncSession, user_id: int) -> User | None:
    if not 0 < user_id <= _MAX_DB_ID:
        return None
    return await users_svc.get_user(session, user_id)


def _is_active(user: User | None) -> bool:
    return user is not None and user.is_active


def _can_view(task: Task, user: User | None) -> bool:
    """Начальник видит любые задачи, сотрудник — только свои."""
    if user is None or not user.is_active:
        return False
    return user.is_manager or task.assignee_id == user.id


async def _safe_notify(coro: Awaitable[Any]) -> Any:
    """Уведомление другим пользователям не должно ломать уже сохранённое действие.

    -> результат notify_* (доставлено ли); при ошибке — None.
    """
    try:
        return await coro
    except Exception:  # noqa: BLE001
        log.exception("Не удалось отправить уведомление")
        return None


async def _answer(event: Message | CallbackQuery, text: str | None = None) -> None:
    """Ответить на нажатие кнопки (для сообщения — ничего не делать)."""
    if isinstance(event, CallbackQuery):
        await event.answer(text)


async def _clear_dialog(state: FSMContext) -> None:
    """state.clear(), но позицию списка задач (K_BACK) сохраняем — для кнопки «◀ К списку задач»."""
    back = (await state.get_data()).get(K_BACK)
    await state.clear()
    if back:
        await state.update_data({K_BACK: back})


async def _strip_prompt(event: Message | CallbackQuery, state: FSMContext) -> None:
    """Убрать кнопки у текущего вопроса диалога (чтобы по ним нельзя было нажать повторно).

    Сообщение, на кнопку которого сейчас нажали, не трогаем: его хендлер сам отредактирует.
    """
    prompt_id = (await state.get_data()).get(K_PROMPT)
    if not isinstance(prompt_id, int) or event.bot is None:
        return
    if isinstance(event, CallbackQuery):
        if event.message is not None and event.message.message_id == prompt_id:
            return
        chat_id = event.message.chat.id if event.message is not None else event.from_user.id
    else:
        chat_id = event.chat.id
    try:
        await event.bot.edit_message_reply_markup(chat_id=chat_id, message_id=prompt_id, reply_markup=None)
    except TelegramAPIError:
        pass


async def _leave_own_dialog(event: Message | CallbackQuery, state: FSMContext) -> None:
    """Пользователь ушёл из диалога правки/отмены на другой экран — диалог закрываем."""
    current = await state.get_state()
    if current is not None and (current in EditTaskSG or current in CancelTaskSG):
        await _strip_prompt(event, state)
        await _clear_dialog(state)


def _task_header(task: Task) -> str:
    return f"<b>Задача #{task.id}</b> «{esc(_clip(task.title, 200))}»"


# --- Карточка ---------------------------------------------------------------------------------


def _back_cb(user: User, saved: object) -> ListCB:
    """Куда ведёт «◀ К списку задач»: последний открытый список (вкладка, страница) или «В работе».

    Очереди начальника «📥 Предложения» и «📝 На проверке» (их ведут task_propose / task_review)
    сами запоминают себя под тем же ключом K_BACK — карточка из очереди возвращает в очередь.
    """
    allowed = (*_LIST_SCOPES, "proposals", "review") if common.is_manager(user) else ("my",)
    if isinstance(saved, str):
        try:
            back = ListCB.unpack(saved)
        except (TypeError, ValueError):
            back = None
        if back is not None and back.scope in allowed:
            return back
    return ListCB(scope="all" if common.is_manager(user) else "my", status="open")


def _card_kb(task: Task, user: User, back: ListCB) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    sub = task.last_submission
    if common.is_manager(user) and sub is not None and sub.attachments:
        # Файлы-подтверждения последней сдачи — и после решения («что фактически получено?»).
        rows.append([_btn(f"📎 Файлы ({len(sub.attachments)})", SubCB(action="files", sub_id=sub.id))])
    rows.append([_btn("◀ К списку задач", back)])
    return _with_rows(keyboards.task_actions_kb(task, user), *rows)


async def _show_card(
    event: Message | CallbackQuery,
    state: FSMContext,
    task: Task,
    user: User,
    note: str | None = None,
    *,
    new_message: bool = False,
) -> Message | None:
    text = render.task_card(task, utcnow(), show_assignee=common.is_manager(user))
    if note:
        text = f"{note}\n\n{text}"
    back = _back_cb(user, (await state.get_data()).get(K_BACK))
    text, kb = truncate(text, _MSG_LIMIT), _card_kb(task, user, back)
    if new_message:
        return await common.send_new(event, text, kb)
    return await common.edit_or_answer(event, text, kb)


def _from_notification(callback: CallbackQuery) -> bool:
    """Кнопка нажата в уведомлении или напоминании, а не в списке задач / истории / карточке."""
    msg = callback.message
    if not isinstance(msg, Message) or msg.text is None:
        return False
    return not msg.text.startswith(_OWN_SCREENS)  # значки экранов одинаковы на обоих языках


# --- Списки -----------------------------------------------------------------------------------


def _status_filter(status: str, viewer_is_manager: bool) -> tuple[tuple[TaskStatus, ...], bool]:
    """Вкладка списка -> (статусы, только просроченные)."""
    match status:
        case "overdue":
            return OPEN_STATUSES, True
        case "review":
            return (TaskStatus.SUBMITTED,), False
        case "done":
            return (TaskStatus.DONE,), False
        case "all":
            return (_ALL_FOR_MANAGER if viewer_is_manager else _ALL_FOR_EMPLOYEE), False
    return OPEN_STATUSES, False


async def _render_list(
    session: AsyncSession,
    viewer: User,
    scope: str,
    status: str,
    page: int,
    target: User | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    if status not in _STATUS_TITLES:
        status = "open"
    statuses, overdue_only = _status_filter(status, viewer.is_manager)
    if scope == "my":
        assignee_id: int | None = viewer.id
    elif scope == "emp" and target is not None:
        assignee_id = target.id
    else:
        assignee_id = None

    total = await tasks_svc.count_tasks(
        session, assignee_id=assignee_id, statuses=statuses, overdue_only=overdue_only
    )
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    items = await tasks_svc.list_tasks(
        session,
        assignee_id=assignee_id,
        statuses=statuses,
        overdue_only=overdue_only,
        limit=PAGE_SIZE,
        offset=page * PAGE_SIZE,
    )

    if scope == "my":
        title = "📋 Мои задачи"
    elif scope == "emp" and target is not None:
        title = f"📋 Задачи: {esc(target.short_name)}"
    else:
        title = "📋 Задачи"
    lines = [f"<b>{title} — {_STATUS_TITLES[status]} ({total})</b>"]
    if pages > 1:
        lines.append(f"Страница {page + 1} из {pages}")
    lines.append("")
    if items:
        now = utcnow()
        lines.extend(render.task_line(task, now, with_assignee=scope == "all") for task in items)
    else:
        lines.append("Задач нет.")

    user_id = target.id if (scope == "emp" and target is not None) else 0
    kb = keyboards.task_list_kb(items, scope, status, page, total, user_id=user_id, page_size=PAGE_SIZE)
    if scope == "emp" and target is not None:
        kb = _with_rows(kb, [_btn("◀ К карточке сотрудника", UserCB(action="card", user_id=target.id))])
    return truncate("\n".join(lines), _MSG_LIMIT), kb


@router.message(F.text.in_({BTN_TASKS, BTN_MY_TASKS}), IsActiveUser())
@router.message(Command("tasks", "my"), IsActiveUser())
async def menu_tasks(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    """«📋 Задачи» (начальник — все задачи) / «📋 Мои задачи» (сотрудник — свои)."""
    await _leave_own_dialog(message, state)
    await state.clear()
    scope = "all" if user.is_manager else "my"
    text, kb = await _render_list(session, user, scope, "open", 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(ListCB.filter(F.scope.in_(_LIST_SCOPES)))
async def list_page(
    callback: CallbackQuery,
    callback_data: ListCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if user is None or not user.is_active:
        await common.deny(callback)
        return
    scope = callback_data.scope
    if scope in ("all", "emp") and not user.is_manager:
        await common.deny(callback)
        return
    target: User | None = None
    if scope == "emp":
        target = await _get_user(session, callback_data.user_id)
        if target is None:
            await callback.answer("Сотрудник не найден.", show_alert=True)
            return
    await _leave_own_dialog(callback, state)
    text, kb = await _render_list(session, user, scope, callback_data.status, callback_data.page, target)
    await state.update_data({K_BACK: callback_data.pack()})
    await common.edit_or_answer(callback, text, kb)
    await callback.answer()


# --- Карточка, принятие, история --------------------------------------------------------------


def _visible_events(task: Task, events: list[TaskEvent], viewer: User) -> list[TaskEvent]:
    """Исполнитель не видит предварительную оценку AI, пока начальник не принял решение (SPEC 7.6).

    Скрываются события AI_EVALUATED после последней сдачи, если по ней ещё нет решения
    (так же, как в карточке задачи для исполнителя).
    """
    if common.is_manager(viewer):
        return events
    sub = task.last_submission
    if sub is None or sub.decision is not None:
        return events
    last_submit = max((i for i, event in enumerate(events) if event.type == EventType.SUBMITTED), default=-1)
    return [
        event for i, event in enumerate(events)
        if not (event.type == EventType.AI_EVALUATED and i > last_submit)
    ]


@router.callback_query(TaskCB.filter(F.action == "open"))
async def open_task(
    callback: CallbackQuery,
    callback_data: TaskCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    task = await _get_task(session, callback_data.task_id)
    if task is None:
        await callback.answer(common.NOT_FOUND, show_alert=True)
        return
    if user is None or not _can_view(task, user):
        await common.deny(callback)
        return
    await _leave_own_dialog(callback, state)
    await _show_card(callback, state, task, user, new_message=_from_notification(callback))
    await callback.answer()


@router.callback_query(TaskCB.filter(F.action == "accept"))
async def accept_task(
    callback: CallbackQuery,
    callback_data: TaskCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    task = await _get_task(session, callback_data.task_id)
    if task is None:
        await callback.answer(common.NOT_FOUND, show_alert=True)
        return
    if user is None or not user.is_active or task.assignee_id != user.id:
        await common.deny(callback)
        return
    if task.accepted_at is not None:
        await _show_card(callback, state, task, user)
        await callback.answer("Задача уже принята в работу.")
        return
    if not task.is_open:
        await _show_card(callback, state, task, user)
        await callback.answer("Задача уже не в работе.", show_alert=True)
        return
    task = await tasks_svc.accept_task(session, task.id, user)
    await session.commit()
    await _show_card(callback, state, task, user)
    await callback.answer("Принято в работу ✅")


@router.callback_query(TaskCB.filter(F.action == "history"))
async def task_history(
    callback: CallbackQuery,
    callback_data: TaskCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    task = await _get_task(session, callback_data.task_id)
    if task is None:
        await callback.answer(common.NOT_FOUND, show_alert=True)
        return
    if user is None or not _can_view(task, user):
        await common.deny(callback)
        return
    await _leave_own_dialog(callback, state)
    events = _visible_events(task, await tasks_svc.task_events(session, task.id), user)
    text = _fit_tail(render.events_text(task, events))
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[_btn("◀ К задаче", TaskCB(action="open", task_id=task.id))]]
    )
    await common.edit_or_answer(callback, text, kb)
    await callback.answer()


# --- Диалоговые хелперы (правка и отмена) -----------------------------------------------------


async def _stale_prompt(callback: CallbackQuery, state: FSMContext) -> bool:
    """Кнопка не из текущего вопроса диалога (сообщение прежнего диалога, возможно о другой задаче).

    Её значение к задаче текущего диалога не применяем; сам диалог продолжается.
    """
    prompt_id = (await state.get_data()).get(K_PROMPT)
    if callback.message is not None and callback.message.message_id == prompt_id:
        return False
    await common.remove_markup(callback)
    await callback.answer(STALE_BUTTON, show_alert=True)
    return True


async def _prompt(
    event: Message | CallbackQuery, state: FSMContext, text: str, kb: InlineKeyboardMarkup
) -> None:
    """Показать вопрос: по кнопке — отредактировать сообщение, после ввода текста — новым сообщением."""
    if isinstance(event, CallbackQuery):
        msg = await common.edit_or_answer(event, text, kb)
    else:
        await _strip_prompt(event, state)
        msg = await event.answer(text, reply_markup=kb)
    if msg is not None:
        await state.update_data({K_PROMPT: msg.message_id})


async def _finish(event: Message | CallbackQuery, state: FSMContext) -> None:
    """Закрыть диалог. Вопрос с кнопками — сообщение callback'а, его отредактирует вызывающий."""
    await _strip_prompt(event, state)
    await _clear_dialog(state)


async def _abort(event: Message | CallbackQuery, state: FSMContext, text: str) -> None:
    """Завершить диалог с объяснением. Для callback отвечает на него (дальше — только return)."""
    await _finish(event, state)
    if isinstance(event, CallbackQuery):
        await common.remove_markup(event)
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)


async def _dialog_task(
    session: AsyncSession,
    state: FSMContext,
    user: User | None,
    statuses: tuple[TaskStatus, ...],
) -> Task | None:
    """Задача текущего диалога, если пользователь — начальник и статус ещё подходит."""
    if not common.is_manager(user):
        return None
    task_id = (await state.get_data()).get(K_TASK)
    if not isinstance(task_id, int):
        return None
    task = await tasks_svc.get_task(session, task_id)
    if task is None or task.status not in statuses:
        return None
    return task


# --- Правка задачи (начальник) -------------------------------------------------------------


def _plan_label(task: Task) -> str:
    if task.plan_value is None:
        return "не задан"
    return f"{fmt_num(task.plan_value)} {task.plan_unit or ''}".strip()


def _unit_after_number(text: str) -> str | None:
    """«100 договоров» -> «договоров», «95 %» -> «%» (слово сразу после первого числа)."""
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    words = text[match.end():].split()
    if not words:
        return None
    if words[0].startswith("%"):
        return "%"
    word = words[0].strip(".,;:!?()«»\"'%")
    return word[:64] or None


def _parse_weight(text: str) -> int | None:
    """Целый вес 1..100 из «20», «20 %», «вес 20». «1e309», «20,5», «-5» — None (а не «1 %»)."""
    match = _WEIGHT_RE.fullmatch((text or "").strip())
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 100 else None


_APOSTROPHES = str.maketrans({char: "'" for char in "ʻʼ‘’`´"})


def _parse_priority(text: str) -> Priority | None:
    """«высокий», «🔴 Высокий», «низкий» -> приоритет. Слово должно начинаться с основы:
    «невысокий» — не «высокий» (лучше переспросить, чем поставить противоположное)."""
    low = (text or "").strip().lstrip("🔴🟡🟢 ").lower().translate(_APOSTROPHES)
    for prefix, priority in _PRIORITY_WORDS:
        if low.startswith(prefix):
            return priority
    return None


def _edit_menu_text(task: Task) -> str:
    return (
        f"✏️ <b>Изменение задачи #{task.id}</b>\n"
        f"«{esc(_clip(task.title, 200))}»\n"
        f"Исполнитель: {esc(task.assignee.short_name)}\n\n"
        "Что изменить? Выберите поле 👇\n"
        "<i>Исполнитель получит уведомление об изменении.</i>"
    )


async def _field_prompt(
    session: AsyncSession, task: Task, key: str
) -> tuple[State, str, InlineKeyboardMarkup] | None:
    """Вопрос для выбранного поля: (состояние, текст, клавиатура)."""
    header = _task_header(task)
    match key:
        case "title":
            return (
                EditTaskSG.text,
                f"{header}\n\n✏️ Введите новое <b>название</b> задачи (до 255 символов).\n\n"
                f"Сейчас: <i>{esc(_clip(task.title, 300))}</i>",
                keyboards.cancel_kb(),
            )
        case "result":
            return (
                EditTaskSG.text,
                f"{header}\n\n✏️ Введите новый <b>ожидаемый результат</b> — измеримо: что, сколько "
                "и в какой форме сдаётся.\nНапример: «Проверить 100 договоров и представить отчёт».\n\n"
                f"Сейчас: <i>{esc(_clip(task.expected_result, 1500))}</i>",
                keyboards.cancel_kb(),
            )
        case "plan":
            rows: list[list[InlineKeyboardButton]] = []
            if task.plan_value is not None:
                rows.append([_btn("🗑 Убрать план", PickCB(field="tv_plan", value="clear"))])
            rows.append([_cancel_btn()])
            return (
                EditTaskSG.text,
                f"{header}\n\n✏️ Введите <b>плановое число</b> с единицей, например: «100 договоров» "
                "или «5 отчётов».\nЕсли написать только число, единица останется прежней.\n\n"
                f"Сейчас: <i>{esc(_plan_label(task))}</i>",
                InlineKeyboardMarkup(inline_keyboard=rows),
            )
        case "deadline":
            return (
                EditTaskSG.deadline,
                f"{header}\n\n✏️ Выберите новый <b>срок</b> кнопкой или напишите его, например: "
                "«завтра», «в пятницу», «5 октября», «05.10 18:00».\n\n"
                f"Сейчас: <i>{esc(fmt_deadline(task.deadline))}</i>",
                _ensure_cancel(keyboards.deadline_kb()),
            )
        case "priority":
            current = render.PRIORITY_LABELS.get(task.priority, str(task.priority))
            return (
                EditTaskSG.priority,
                f"{header}\n\n✏️ Выберите <b>приоритет</b>.\n\nСейчас: {current}",
                _ensure_cancel(keyboards.priority_kb()),
            )
        case "weight":
            load = await _week_load(session, task)
            hint = (
                f"Другие задачи сотрудника на неделе срока: {load} % "
                f"(вместе с этой — {load + task.weight} %).\n"
                if load is not None
                else ""
            )
            return (
                EditTaskSG.weight,
                f"{header}\n\n✏️ Выберите <b>вес</b> задачи кнопкой или введите целое число от 1 до 100.\n\n"
                f"Сейчас: {task.weight} %\n"
                f"{hint}"
                "Рекомендуется, чтобы сумма весов за неделю была ≈100 %.",
                _ensure_cancel(keyboards.weight_kb(load)),
            )
    return None


async def _week_load(session: AsyncSession, task: Task) -> int | None:
    """Вес других задач сотрудника на неделе срока; срок у границы календаря — без подсказки."""
    try:
        return await tasks_svc.weight_load(session, task.assignee_id, task.deadline, exclude_task_id=task.id)
    except OverflowError:
        log.warning("weight_load: срок задачи #%s вне диапазона дат", task.id)
        return None


def _too_far(deadline: datetime) -> bool:
    return to_local(deadline).year > to_local(utcnow()).year + _DEADLINE_MAX_YEARS


def _names_far_year(text: str) -> bool:
    """В тексте год дальше допустимого («31.12.9999») — объяснить это, а не «не понял срок»."""
    limit = to_local(utcnow()).year + _DEADLINE_MAX_YEARS
    return any(int(year) > limit for year in _YEAR_RE.findall(text))


async def _reprompt(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    task: Task,
    key: str,
    error: str,
) -> None:
    prompt = await _field_prompt(session, task, key)
    if prompt is None:  # неизвестное поле — диалог не продолжить (callback здесь не отвечаем)
        await _finish(event, state)
        await common.edit_or_answer(event, "Не удалось продолжить изменение. Откройте задачу заново.")
        return
    _, text, kb = prompt
    await _prompt(event, state, f"⚠️ {error}\n\n{text}", kb)


async def _apply_edit(
    event: Message | CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
    user: User,
    task: Task,
    key: str,
    fields: dict[str, Any],
) -> None:
    """update_task -> commit -> ответ на нажатие -> уведомление исполнителю -> карточка.

    На callback отвечает сама до уведомления (оно может ждать флуд-лимит Telegram); карточка —
    после: в ней сказано, дошло ли уведомление. Значение не подошло — вопрос задаётся снова.
    """
    try:
        task, changes = await tasks_svc.update_task(session, task.id, user, **fields)
    except DomainError as exc:
        await _reprompt(event, state, session, task, key, esc(exc.message))
        await _answer(event)
        return
    await session.commit()
    await _finish(event, state)
    await _answer(event)
    if changes:
        delivered = await _safe_notify(notify.notify_task_changed(bot, task, changes))
        labels = list(dict.fromkeys(_CHANGE_LABELS.get(name, name) for name in changes))
        note = f"✅ Изменено: {', '.join(labels)}. " + (
            "Исполнитель получил уведомление." if delivered else f"\n{common.NOT_DELIVERED}"
        )
    else:
        note = "Ничего не изменилось — значение совпадает с текущим."
    await _show_card(event, state, task, user, note)


@router.callback_query(TaskCB.filter(F.action == "edit"))
async def edit_start(
    callback: CallbackQuery,
    callback_data: TaskCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if not common.is_manager(user):
        await common.deny(callback)
        return
    task = await _get_task(session, callback_data.task_id)
    if task is None:
        await callback.answer(common.NOT_FOUND, show_alert=True)
        return
    if task.status not in _EDITABLE:
        await callback.answer("Изменить можно только задачу в работе или на доработке.", show_alert=True)
        return
    await _strip_prompt(callback, state)  # вопрос прежнего диалога правки/отмены, если он был
    await _clear_dialog(state)
    await state.set_state(EditTaskSG.field)
    await state.update_data({K_TASK: task.id})
    await _prompt(callback, state, _edit_menu_text(task), _ensure_cancel(keyboards.edit_fields_kb(_EDIT_FIELDS)))
    await callback.answer()


@router.callback_query(EditTaskSG.field, PickCB.filter(F.field == "field"))
async def edit_field_chosen(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if await _stale_prompt(callback, state):
        return
    task = await _dialog_task(session, state, user, _EDITABLE)
    if task is None:
        await _abort(callback, state, STALE_EDIT)
        return
    prompt = await _field_prompt(session, task, callback_data.value)
    if prompt is None:
        await callback.answer("Это поле изменить нельзя.", show_alert=True)
        return
    next_state, text, kb = prompt
    await state.set_state(next_state)
    await state.update_data({K_FIELD: callback_data.value})
    await _prompt(callback, state, text, kb)
    await callback.answer()


@router.message(EditTaskSG.text, TextInput())
async def edit_text_value(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User | None,
) -> None:
    task = await _dialog_task(session, state, user, _EDITABLE)
    if task is None or user is None:
        await _abort(message, state, STALE_EDIT)
        return
    key = (await state.get_data()).get(K_FIELD)
    value = (message.text or "").strip()
    fields: dict[str, Any]
    if key == "title":
        fields = {"title": value}
    elif key == "result":
        if len(value) > _MAX_RESULT:
            await _reprompt(
                message, state, session, task, key,
                f"Слишком длинно ({len(value)} симв.). Сократите до {_MAX_RESULT} символов.",
            )
            return
        fields = {"expected_result": value}
    elif key == "plan":
        number = parse_number(value)
        if number is None or not math.isfinite(number) or not 0 < number <= _MAX_PLAN:
            await _reprompt(
                message, state, session, task, key,
                "Не нашёл подходящего положительного числа. Напишите, например: «100 договоров».",
            )
            return
        fields = {"plan_value": number}
        unit = _unit_after_number(value)
        if unit:
            fields["plan_unit"] = unit
    else:
        await _abort(message, state, "Не удалось продолжить изменение. Откройте задачу заново.")
        return
    await _apply_edit(message, session, bot, state, user, task, key, fields)


@router.callback_query(EditTaskSG.text, PickCB.filter(F.field == "tv_plan"))
async def edit_plan_clear(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User | None,
) -> None:
    if await _stale_prompt(callback, state):
        return
    task = await _dialog_task(session, state, user, _EDITABLE)
    if task is None or user is None:
        await _abort(callback, state, STALE_EDIT)
        return
    await _apply_edit(callback, session, bot, state, user, task, "plan", {"plan_value": None, "plan_unit": None})


@router.callback_query(EditTaskSG.deadline, PickCB.filter(F.field == "deadline"))
async def edit_deadline_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User | None,
) -> None:
    if await _stale_prompt(callback, state):
        return
    task = await _dialog_task(session, state, user, _EDITABLE)
    if task is None or user is None:
        await _abort(callback, state, STALE_EDIT)
        return
    try:
        deadline = iso_to_deadline(callback_data.value)
    except (TypeError, ValueError, OverflowError):
        await callback.answer("Напишите срок сообщением, например «5 октября 18:00».", show_alert=True)
        return
    if _too_far(deadline):
        await _reprompt(callback, state, session, task, "deadline", TOO_FAR)
        await callback.answer()
        return
    await _apply_edit(callback, session, bot, state, user, task, "deadline", {"deadline": deadline})


@router.message(EditTaskSG.deadline, TextInput())
async def edit_deadline_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User | None,
) -> None:
    task = await _dialog_task(session, state, user, _EDITABLE)
    if task is None or user is None:
        await _abort(message, state, STALE_EDIT)
        return
    deadline = parse_deadline(message.text or "")
    if deadline is None and not _names_far_year(message.text or ""):
        await _reprompt(message, state, session, task, "deadline", "Не понял срок или он уже прошёл.")
        return
    if deadline is None or _too_far(deadline):
        await _reprompt(message, state, session, task, "deadline", TOO_FAR)
        return
    await _apply_edit(message, session, bot, state, user, task, "deadline", {"deadline": deadline})


@router.callback_query(EditTaskSG.priority, PickCB.filter(F.field == "prio"))
async def edit_priority_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User | None,
) -> None:
    if await _stale_prompt(callback, state):
        return
    task = await _dialog_task(session, state, user, _EDITABLE)
    if task is None or user is None:
        await _abort(callback, state, STALE_EDIT)
        return
    try:
        priority = Priority(callback_data.value)
    except ValueError:
        await callback.answer("Неизвестный приоритет.", show_alert=True)
        return
    await _apply_edit(callback, session, bot, state, user, task, "priority", {"priority": priority})


@router.message(EditTaskSG.priority, TextInput())
async def edit_priority_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User | None,
) -> None:
    task = await _dialog_task(session, state, user, _EDITABLE)
    if task is None or user is None:
        await _abort(message, state, STALE_EDIT)
        return
    priority = _parse_priority(message.text or "")
    if priority is None:
        await _reprompt(message, state, session, task, "priority", "Выберите приоритет кнопкой.")
        return
    await _apply_edit(message, session, bot, state, user, task, "priority", {"priority": priority})


@router.callback_query(EditTaskSG.weight, PickCB.filter(F.field == "weight"))
async def edit_weight_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User | None,
) -> None:
    if await _stale_prompt(callback, state):
        return
    task = await _dialog_task(session, state, user, _EDITABLE)
    if task is None or user is None:
        await _abort(callback, state, STALE_EDIT)
        return
    weight = _parse_weight(callback_data.value)
    if weight is None:
        await callback.answer("Вес — целое число от 1 до 100.", show_alert=True)
        return
    await _apply_edit(callback, session, bot, state, user, task, "weight", {"weight": weight})


@router.message(EditTaskSG.weight, TextInput())
async def edit_weight_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User | None,
) -> None:
    task = await _dialog_task(session, state, user, _EDITABLE)
    if task is None or user is None:
        await _abort(message, state, STALE_EDIT)
        return
    weight = _parse_weight(message.text or "")
    if weight is None:
        await _reprompt(
            message, state, session, task, "weight", "Вес — целое число от 1 до 100, например: 20."
        )
        return
    await _apply_edit(message, session, bot, state, user, task, "weight", {"weight": weight})


# --- Отмена задачи (начальник) -------------------------------------------------------------


def _cancel_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🚫 Да, отменить задачу", PickCB(field="cancel_task", value="yes"))],
            [_btn("◀ Нет, не отменять", PickCB(field="cancel_task", value="no"))],
        ]
    )


def _cancel_reason_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("⏭ Без причины", PickCB(field="cancel_reason", value="skip"))],
            [_cancel_btn()],
        ]
    )


def _cancel_reason_text(task: Task) -> str:
    return (
        f"{_task_header(task)}\n\n"
        "✍️ Напишите <b>причину отмены</b> — её увидит исполнитель.\n"
        "Или нажмите «⏭ Без причины»."
    )


async def _do_cancel(
    event: Message | CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
    user: User,
    task: Task,
    reason: str | None,
) -> None:
    """cancel_task -> commit -> ответ на нажатие -> уведомление исполнителю -> карточка.

    На callback отвечает сама до уведомления (оно может ждать флуд-лимит Telegram); карточка —
    после: в ней сказано, дошло ли уведомление.
    """
    try:
        task = await tasks_svc.cancel_task(session, task.id, user, reason)
    except DomainError as exc:
        await _finish(event, state)
        await _show_card(event, state, task, user, f"⚠️ {esc(exc.message)}")
        await _answer(event)
        return
    await session.commit()
    await _finish(event, state)
    await _answer(event, "Задача отменена 🚫")
    delivered = await _safe_notify(notify.notify_task_cancelled(bot, task, reason))
    tail = "Исполнитель получил уведомление." if delivered else f"\n{common.NOT_DELIVERED}"
    await _show_card(event, state, task, user, f"🚫 <b>Задача #{task.id} отменена.</b> {tail}")


@router.callback_query(TaskCB.filter(F.action == "cancel"))
async def cancel_start(
    callback: CallbackQuery,
    callback_data: TaskCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if not common.is_manager(user):
        await common.deny(callback)
        return
    task = await _get_task(session, callback_data.task_id)
    if task is None:
        await callback.answer(common.NOT_FOUND, show_alert=True)
        return
    if task.status not in _CANCELLABLE:
        await callback.answer(STALE_CANCEL, show_alert=True)
        return
    await _strip_prompt(callback, state)  # вопрос прежнего диалога правки/отмены, если он был
    await _clear_dialog(state)
    await state.set_state(CancelTaskSG.confirm)
    await state.update_data({K_TASK: task.id})
    text = (
        f"🚫 <b>Отменить задачу #{task.id}?</b>\n"
        f"«{esc(_clip(task.title, 200))}»\n"
        f"Исполнитель: {esc(task.assignee.short_name)}\n\n"
        "Отменённая задача не учитывается в KPI, исполнитель получит уведомление."
    )
    await _prompt(callback, state, text, _cancel_confirm_kb())
    await callback.answer()


@router.callback_query(CancelTaskSG.confirm, PickCB.filter(F.field == "cancel_task"))
async def cancel_confirm(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if await _stale_prompt(callback, state):
        return
    if callback_data.value != "yes":
        task_id = (await state.get_data()).get(K_TASK)
        await _finish(callback, state)
        task = await tasks_svc.get_task(session, task_id) if isinstance(task_id, int) else None
        if task is not None and user is not None and _can_view(task, user):
            await _show_card(callback, state, task, user)
        else:
            await common.edit_or_answer(callback, "Задача не отменена.")
        await callback.answer("Задача не отменена.")
        return
    task = await _dialog_task(session, state, user, _CANCELLABLE)
    if task is None:
        await _abort(callback, state, STALE_CANCEL)
        return
    await state.set_state(CancelTaskSG.reason)
    await _prompt(callback, state, _cancel_reason_text(task), _cancel_reason_kb())
    await callback.answer()


@router.callback_query(CancelTaskSG.reason, PickCB.filter(F.field == "cancel_reason"))
async def cancel_without_reason(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User | None,
) -> None:
    if await _stale_prompt(callback, state):
        return
    task = await _dialog_task(session, state, user, _CANCELLABLE)
    if task is None or user is None:
        await _abort(callback, state, STALE_CANCEL)
        return
    await _do_cancel(callback, session, bot, state, user, task, None)


@router.message(CancelTaskSG.reason, TextInput())
async def cancel_reason_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User | None,
) -> None:
    task = await _dialog_task(session, state, user, _CANCELLABLE)
    if task is None or user is None:
        await _abort(message, state, STALE_CANCEL)
        return
    reason = (message.text or "").strip()
    if len(reason) > _MAX_REASON:
        await _prompt(
            message,
            state,
            f"⚠️ Слишком длинно — до {_MAX_REASON} символов.\n\n{_cancel_reason_text(task)}",
            _cancel_reason_kb(),
        )
        return
    await _do_cancel(message, session, bot, state, user, task, reason)


# --- Подсказки при неподходящем вводе ---------------------------------------------------------

_BUTTON_ONLY_STATES = (EditTaskSG.field, CancelTaskSG.confirm)


@router.message(StateFilter(*_BUTTON_ONLY_STATES), TextInput())
async def hint_use_buttons(message: Message) -> None:
    await message.answer(HINT_BUTTONS)


@router.message(StateFilter(EditTaskSG, CancelTaskSG), F.text.is_(None))
async def hint_non_text(message: Message, state: FSMContext) -> None:
    """Фото, файл, стикер и т. п. посреди диалога правки/отмены."""
    current = await state.get_state()
    button_only = {s.state for s in _BUTTON_ONLY_STATES} | {EditTaskSG.priority.state}
    await message.answer(HINT_BUTTONS if current in button_only else HINT_TEXT)


@router.callback_query(PickCB.filter(F.field.in_({"cancel_task", "cancel_reason", "tv_plan"})))
async def stale_dialog_button(callback: CallbackQuery) -> None:
    """Кнопка из диалога, который уже завершён (повторное нажатие, перезапуск бота).

    Кнопки не убираем: при двойном нажатии это сообщение уже стало карточкой задачи
    или следующим вопросом диалога, и их кнопки нужны.
    """
    await callback.answer("Эта кнопка уже неактуальна. Откройте задачу заново.", show_alert=True)
