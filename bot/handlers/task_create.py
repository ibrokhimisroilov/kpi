"""«➕ Поставить задачу» (/new) — руководитель ставит задачу сотруднику (SPEC 7.3).

Сценарий: сотрудник → название → ожидаемый результат своими словами (+ подсказка AI / правил) →
плановое число (если не определилось) → срок → приоритет → вес → сводка → создание.
Из сводки «✏️ Изменить» позволяет поправить любое поле; после ввода значения диалог
возвращается к сводке (флаг ``editing`` в FSM-данных), а не продолжает мастер.

FSM-данные (только JSON-совместимые значения):
    assignee_id: int, assignee_name: str, title: str, description: str | None (исходные слова),
    expected_result: str, plan_value: float | None, plan_unit: str | None,
    deadline: str (ISO naive UTC, common.dt_to_state), priority: str (Priority.value), weight: int.
    Служебные: raw_result, suggestion (dict ResultSuggestion), ai_busy + ai_busy_since (ISO naive UTC),
    ai_lost (запрос к AI прервала остановка бота), editing, creating,
    prompt_id — id последнего сообщения-вопроса диалога: срабатывают только его кнопки, а кнопки
    старых сообщений (в том числе из брошенных диалогов) отвечают «кнопка уже неактуальна».

Состояние диалога хранится в БД и переживает перезапуск бота. Если бот остановили посреди запроса
к AI (обновление на хостинге, сбой), флаг ai_busy не должен «заморозить» черновик навсегда: при
мягкой остановке вместо ответа AI сохраняется вариант по правилам, а флаг старше времени, за которое
AI обязан ответить (_ai_busy_stale_sec), считается брошенным — руководитель получает вариант по правилам.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notify
from bot.ai import formulate
from bot.config import get_settings
from bot.db.models import Priority, Role, User
from bot.filters import IsManager, TextInput
from bot.handlers import common
from bot.services import tasks, users
from bot.services.errors import DomainError
from bot.ui import keyboards, render
from bot.ui.callbacks import ListCB, PickCB
from bot.ui.texts import BTN_NEW_TASK
from bot.utils import dateparse
from bot.utils.dates import fmt_deadline, to_local, utcnow
from bot.utils.text import esc, fmt_num, parse_number, plural

log = logging.getLogger(__name__)

router = Router(name="task_create")


class CreateTaskSG(StatesGroup):
    assignee = State()       # выбор сотрудника (кнопки)
    title = State()          # название (текст)
    result_raw = State()     # ожидаемый результат своими словами (текст)
    result_choice = State()  # выбор: принять подсказку / другой вариант / свой / как написал
    result_manual = State()  # своя формулировка (текст)
    plan_value = State()     # плановое число (текст или «Пропустить»)
    deadline = State()       # срок (кнопки или текст)
    priority = State()       # приоритет (кнопки)
    weight = State()         # вес (кнопки или текст)
    confirm = State()        # сводка: создать / изменить
    edit_field = State()     # какое поле изменить


TITLE_MAX = 255
RESULT_MAX = 1000
UNIT_MAX = 64
PLAN_MAX = 1e15  # больше — явно ошибка ввода (и fmt_num не умеет показывать числа от 1e26)
# Срок дальше — опечатка в годе («31.12.9999» к тому же ломал расчёт недели). Как в dateparse.
DEADLINE_MAX_YEARS: int = getattr(dateparse, "MAX_YEARS_AHEAD", 5)
TOTAL_STEPS = 6
_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
STEP_NO = {"assignee": 1, "title": 2, "result": 3, "plan": 3, "deadline": 4, "priority": 5, "weight": 6}
STEP_STATES: dict[str, State] = {
    "assignee": CreateTaskSG.assignee,
    "title": CreateTaskSG.title,
    "result": CreateTaskSG.result_raw,
    "plan": CreateTaskSG.plan_value,
    "deadline": CreateTaskSG.deadline,
    "priority": CreateTaskSG.priority,
    "weight": CreateTaskSG.weight,
}
EDIT_FIELDS: list[tuple[str, str]] = [
    ("assignee", "Сотрудник"),
    ("title", "Название"),
    ("result", "Ожидаемый результат"),
    ("plan", "План (число)"),
    ("deadline", "Срок"),
    ("priority", "Приоритет"),
    ("weight", "Вес"),
]
DRAFT_KEYS = (
    "assignee_id", "assignee_name", "title", "description", "expected_result",
    "plan_value", "plan_unit", "deadline", "priority", "weight",
)
DEADLINE_EXAMPLES = "«05.10», «5 октября 18:00», «завтра», «через 3 дня»"
NO_EMPLOYEES_TEXT = (
    "👥 Пока нет активных сотрудников.\n"
    "Сначала подтвердите сотрудников (👥 Сотрудники) — после этого им можно ставить задачи."
)
STALE_BUTTON = "Эта кнопка уже неактуальна — продолжите в последнем сообщении."
AI_BUSY_TEXT = "⏳ Подождите, формулирую вариант…"
AI_LOST_NOTICE = (
    "⚠️ Вариант от AI не пришёл (бот перезапускался) — ниже вариант по вашим словам. "
    "Можно принять его, попросить другой или ввести свой."
)
_AI_BUSY_MARGIN_SEC = 60  # запас сверх времени, за которое AI обязан ответить
TOO_FAR_TEXT = (
    f"⚠️ Срок слишком далеко. Укажите дату не дальше чем на {plural(DEADLINE_MAX_YEARS, 'год', 'года', 'лет')} вперёд — "
    "проверьте год."
)
_PRIORITY_WORDS = (
    ("выс", Priority.HIGH), ("high", Priority.HIGH),
    ("сред", Priority.MEDIUM), ("обыч", Priority.MEDIUM), ("норм", Priority.MEDIUM), ("medium", Priority.MEDIUM),
    ("низ", Priority.LOW), ("low", Priority.LOW),
)

_CANCEL_DATA = PickCB(field="cancel").pack()          # «k:cancel:»
_BACK_TO_SUMMARY = PickCB(field="field", value="back").pack()

# Число как в text.parse_number («100», «1 200», «10,5») и слово-единица сразу после него.
_PLAN_NUMBER_RE = re.compile(r"(?<![\w.,])[-−]?(?:\d{1,3}(?:[   ]\d{3})+(?!\d)|\d+)(?:[.,]\d+)?")
_PLAN_UNIT_RE = re.compile(r"\s*(%|[^\W\d_]+(?:-[^\W\d_]+)*\.?)")
# Вес текстом: «20», «20 %», «20%», «20.0», «вес 20», «20 процентов». Не «1e309» и не «20,5».
_WEIGHT_RE = re.compile(r"(?:вес\s*)?(\d{1,3})(?:[.,]0+)?\s*(?:%|процент\w*)?", re.IGNORECASE)
_MAX_DB_ID = 2**63 - 1  # предел INTEGER в SQLite; больше — только подделанный callback (OverflowError)


# --- Вход ------------------------------------------------------------------------------------


@router.message(F.text == BTN_NEW_TASK, IsManager())
@router.message(Command("new"), IsManager())
async def start_create(message: Message, state: FSMContext, session: AsyncSession) -> None:
    current = await state.get_state()
    if current is not None and current in CreateTaskSG:
        await _strip_prompt(message, state)  # начали заново — кнопки брошенного черновика убираем
    await state.clear()
    await _show_step(message, state, session, "assignee")


# --- 1. Сотрудник ----------------------------------------------------------------------------


@router.callback_query(CreateTaskSG.assignee, PickCB.filter(F.field == "assignee"))
async def on_assignee(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if not await _guard_cb(callback, state, user):
        return
    employee = None
    if callback_data.value.isdigit() and 0 < int(callback_data.value) <= _MAX_DB_ID:
        employee = await users.get_user(session, int(callback_data.value))
    if employee is None or not employee.is_active or employee.role != Role.EMPLOYEE:
        await _show_step(callback, state, session, "assignee",
                         notice="⚠️ Этот сотрудник сейчас недоступен — выберите другого.")
        await callback.answer()
        return
    await state.update_data(assignee_id=employee.id, assignee_name=employee.full_name)
    await _after_value(callback, state, session, "title",
                       notice=f"👤 Сотрудник: <b>{esc(employee.full_name)}</b>")
    await callback.answer()


# --- 2. Название -----------------------------------------------------------------------------


@router.message(CreateTaskSG.title, TextInput())
async def on_title(message: Message, state: FSMContext, session: AsyncSession, user: User | None) -> None:
    if not await _guard_msg(message, state, user):
        return
    title = " ".join((message.text or "").split())
    if len(title) > TITLE_MAX:
        await _show_step(message, state, session, "title", notice=(
            f"⚠️ Слишком длинное название ({len(title)} симв.). Сократите до {TITLE_MAX} символов — "
            "подробности можно указать в ожидаемом результате."
        ))
        return
    await state.update_data(title=title)
    await _after_value(message, state, session, "result", notice=f"📝 Задача: <b>{esc(title)}</b>")


# --- 3. Ожидаемый результат (+ подсказка AI) ---------------------------------------------------


@router.message(CreateTaskSG.result_raw, TextInput())
async def on_result_raw(message: Message, state: FSMContext, session: AsyncSession, user: User | None) -> None:
    if not await _guard_msg(message, state, user):
        return
    raw = (message.text or "").strip()
    if len(raw) > RESULT_MAX:
        await _show_step(message, state, session, "result",
                         notice=f"⚠️ Слишком длинно ({len(raw)} симв.). Опишите результат короче — до {RESULT_MAX} символов.")
        return
    await state.update_data(raw_result=raw)
    await session.commit()  # перед долгим запросом к AI не держим транзакцию
    await _strip_prompt(message, state)
    data = await state.get_data()
    wait = await message.answer("⏳ Формулирую измеримый результат…")
    await _run_suggestion(state, wait, title=data.get("title") or "", raw_for_ai=raw, raw=raw, retry=False)


@router.callback_query(CreateTaskSG.result_choice, PickCB.filter(F.field == "ai"))
async def on_ai_choice(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if not await _guard_cb(callback, state, user):
        return
    data = await state.get_data()
    if data.get("ai_busy"):
        if not _ai_busy_stale(data):
            await callback.answer(AI_BUSY_TEXT)
            return
        data = await _unstick_ai(state, data)
        await _show(callback, state, _suggestion_text(data, notice=AI_LOST_NOTICE), _ai_kb())
        await callback.answer()
        return

    suggestion: dict[str, Any] = data.get("suggestion") or {}
    title: str = data.get("title") or ""
    raw: str = data.get("raw_result") or ""
    action = callback_data.value

    if action == "retry":
        await callback.answer()
        await session.commit()
        msg = await common.edit_or_answer(
            callback, f"{_header('result', bool(data.get('editing')))}\n\n⏳ Формулирую другой вариант…"
        )
        if msg is None:
            return
        previous = suggestion.get("expected_result") or ""
        raw_for_ai = f"{raw}\n\nПредыдущий вариант: {previous}. Предложи другую формулировку."
        await _run_suggestion(state, msg, title=title, raw_for_ai=raw_for_ai, raw=raw, retry=True)
        return

    if action == "manual":
        await state.set_state(CreateTaskSG.result_manual)
        await _show(callback, state, _manual_text(data), _with_cancel(keyboards.cancel_kb()))
        await callback.answer()
        return

    if action == "accept" and suggestion.get("expected_result"):
        expected = suggestion["expected_result"]
        plan_value, plan_unit = suggestion.get("plan_value"), suggestion.get("plan_unit")
    elif action == "raw" and raw:
        expected = raw
        rules = formulate.rules_suggestion(title, raw)
        plan_value, plan_unit = rules.plan_value, rules.plan_unit
    else:
        await callback.answer("Вариант недоступен — выберите другой.", show_alert=True)
        return
    await _set_result(state, expected=expected, raw=raw, plan_value=plan_value, plan_unit=plan_unit)
    await _after_result(callback, state, session, expected)
    await callback.answer()


@router.message(CreateTaskSG.result_manual, TextInput())
async def on_result_manual(message: Message, state: FSMContext, session: AsyncSession, user: User | None) -> None:
    if not await _guard_msg(message, state, user):
        return
    data = await state.get_data()
    text = (message.text or "").strip()
    if len(text) > RESULT_MAX:
        notice = f"⚠️ Слишком длинно ({len(text)} симв.). Сократите до {RESULT_MAX} символов."
        await _show(message, state, _manual_text(data, notice=notice), _with_cancel(keyboards.cancel_kb()))
        return
    rules = formulate.rules_suggestion(data.get("title") or "", text)
    await _set_result(state, expected=text, raw=data.get("raw_result") or "",
                      plan_value=rules.plan_value, plan_unit=rules.plan_unit)
    await _after_result(message, state, session, text)


# --- 3a. Плановое число ----------------------------------------------------------------------


@router.message(CreateTaskSG.plan_value, TextInput())
async def on_plan_text(message: Message, state: FSMContext, session: AsyncSession, user: User | None) -> None:
    if not await _guard_msg(message, state, user):
        return
    value, unit = _parse_plan(message.text or "")
    if value is None:
        await _show_step(message, state, session, "plan", notice=(
            "⚠️ Не понял число. Введите, например: <i>100</i> или <i>100 договоров</i> — "
            "или нажмите «⏭ Пропустить»."
        ))
        return
    if value <= 0:
        await _show_step(message, state, session, "plan", notice="⚠️ Плановое число должно быть больше нуля.")
        return
    if value > PLAN_MAX:  # в том числе inf: parse_number(«9» × 400) == float("inf")
        await _show_step(message, state, session, "plan", notice=(
            "⚠️ Слишком большое число. Введите реальное плановое значение, например: <i>100</i> "
            "или <i>100 договоров</i>."
        ))
        return
    await state.update_data(plan_value=value, plan_unit=unit)
    await _after_value(message, state, session, "deadline", notice=f"📊 План: <b>{_plan_str(value, unit)}</b>")


@router.callback_query(CreateTaskSG.plan_value, PickCB.filter(F.field == "skip"))
async def on_plan_skip(callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User | None) -> None:
    if not await _guard_cb(callback, state, user):
        return
    await state.update_data(plan_value=None, plan_unit=None)
    await _after_value(callback, state, session, "deadline", notice="📊 Без числового плана.")
    await callback.answer()


# --- 4. Срок ---------------------------------------------------------------------------------


@router.callback_query(CreateTaskSG.deadline, PickCB.filter(F.field == "deadline"))
async def on_deadline_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if not await _guard_cb(callback, state, user):
        return
    try:
        deadline: datetime | None = dateparse.iso_to_deadline(callback_data.value)
    except (ValueError, OverflowError):
        deadline = None
    if deadline is None or deadline <= utcnow():
        await _show_step(callback, state, session, "deadline", notice="⚠️ Этот срок уже прошёл — выберите другой.")
        await callback.answer()
        return
    if _too_far(deadline):
        await _show_step(callback, state, session, "deadline", notice=TOO_FAR_TEXT)
        await callback.answer()
        return
    await _set_deadline(callback, state, session, deadline)
    await callback.answer()


@router.message(CreateTaskSG.deadline, TextInput())
async def on_deadline_text(message: Message, state: FSMContext, session: AsyncSession, user: User | None) -> None:
    if not await _guard_msg(message, state, user):
        return
    deadline = dateparse.parse_deadline(message.text or "")
    if deadline is None and not _names_far_year(message.text or ""):
        await _show_step(message, state, session, "deadline", notice=(
            f"⚠️ Не удалось распознать срок или он уже прошёл. Напишите, например: {DEADLINE_EXAMPLES}."
        ))
        return
    if deadline is None or _too_far(deadline):
        await _show_step(message, state, session, "deadline", notice=TOO_FAR_TEXT)
        return
    await _set_deadline(message, state, session, deadline)


# --- 5. Приоритет ----------------------------------------------------------------------------


@router.callback_query(CreateTaskSG.priority, PickCB.filter(F.field == "prio"))
async def on_priority_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if not await _guard_cb(callback, state, user):
        return
    try:
        priority = Priority(callback_data.value)
    except ValueError:
        await callback.answer("Неизвестный приоритет — выберите кнопкой.", show_alert=True)
        return
    await _set_priority(callback, state, session, priority)
    await callback.answer()


@router.message(CreateTaskSG.priority, TextInput())
async def on_priority_text(message: Message, state: FSMContext, session: AsyncSession, user: User | None) -> None:
    if not await _guard_msg(message, state, user):
        return
    priority = _priority_from_text(message.text or "")
    if priority is None:
        await _show_step(message, state, session, "priority", notice="👇 Выберите приоритет кнопкой.")
        return
    await _set_priority(message, state, session, priority)


# --- 6. Вес ----------------------------------------------------------------------------------


@router.callback_query(CreateTaskSG.weight, PickCB.filter(F.field == "weight"))
async def on_weight_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if not await _guard_cb(callback, state, user):
        return
    weight = _parse_weight(callback_data.value)
    if weight is None:
        await callback.answer("Вес — целое число от 1 до 100.", show_alert=True)
        return
    await state.update_data(weight=weight)
    await _after_value(callback, state, session, "summary")
    await callback.answer()


@router.message(CreateTaskSG.weight, TextInput())
async def on_weight_text(message: Message, state: FSMContext, session: AsyncSession, user: User | None) -> None:
    if not await _guard_msg(message, state, user):
        return
    weight = _parse_weight(message.text or "")
    if weight is None:
        await _show_step(message, state, session, "weight",
                         notice="⚠️ Вес — целое число от 1 до 100. Например: <i>20</i>.")
        return
    await state.update_data(weight=weight)
    await _after_value(message, state, session, "summary")


# --- 7. Сводка, изменение, создание ----------------------------------------------------------


@router.callback_query(CreateTaskSG.confirm, PickCB.filter(F.field == "confirm"))
async def on_confirm(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
) -> None:
    if not await _guard_cb(callback, state, user):
        return
    assert user is not None  # проверено в _guard_cb
    if callback_data.value == "edit":
        await _show_summary(callback, state, edit_menu=True)
        await callback.answer()
        return
    if callback_data.value != "yes":
        await callback.answer()
        return

    data = await state.get_data()
    if data.get("creating"):  # защита от двойного нажатия «Создать»
        await callback.answer("⏳ Задача уже создаётся…")
        return
    deadline = common.dt_from_state(data.get("deadline"))
    if deadline is None or deadline <= utcnow():
        await _reask_deadline(callback, state, session)
        return

    await state.update_data(creating=True)
    try:
        task = await tasks.create_task(
            session,
            creator=user,
            assignee_id=int(data["assignee_id"]),
            title=data["title"],
            expected_result=data["expected_result"],
            deadline=deadline,
            weight=int(data["weight"]),
            priority=Priority(data.get("priority") or Priority.MEDIUM.value),
            description=data.get("description"),
            plan_value=data.get("plan_value"),
            plan_unit=data.get("plan_unit"),
        )
        await session.commit()
    except DomainError as exc:
        await state.update_data(creating=False)
        if deadline <= utcnow():  # срок истёк, пока руководитель смотрел на сводку
            await _reask_deadline(callback, state, session)
        else:
            await callback.answer(exc.message, show_alert=True)
        return
    except Exception:
        await state.update_data(creating=False)
        raise

    await state.clear()
    await callback.answer("✅ Задача поставлена")
    delivered = await notify.notify_new_task(bot, task)
    head = f"✅ <b>Задача #{task.id} поставлена</b>"
    if not delivered:
        head += f"\n{common.NOT_DELIVERED}"
    # Как у карточки из списка: действия с задачей + «◀ К списку задач».
    actions = keyboards.task_actions_kb(task, user).inline_keyboard
    back = InlineKeyboardButton(
        text="◀ К списку задач", callback_data=ListCB(scope="all", status="open").pack()
    )
    await common.edit_or_answer(
        callback,
        f"{head}\n\n{render.task_card(task)}",
        InlineKeyboardMarkup(inline_keyboard=[*actions, [back]]),
    )


@router.callback_query(CreateTaskSG.edit_field, PickCB.filter(F.field == "field"))
async def on_edit_field(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if not await _guard_cb(callback, state, user):
        return
    key = callback_data.value
    if key == "back":
        await _show_summary(callback, state)
    elif key in STEP_STATES:
        await state.update_data(editing=True)
        await _show_step(callback, state, session, key)
    else:
        await callback.answer("Это поле изменить нельзя.", show_alert=True)
        return
    await callback.answer()


# --- Неожиданный ввод и устаревшие кнопки (регистрируются последними) --------------------------


@router.message(StateFilter(CreateTaskSG), ~F.text)
async def on_non_text(message: Message, state: FSMContext, session: AsyncSession, user: User | None) -> None:
    """Фото, стикер, голос и т. п. посреди диалога — напомнить, что ждём."""
    if not await _guard_msg(message, state, user):
        return
    await _reshow(message, state, session, hint="⚠️ Пожалуйста, ответьте текстом или кнопкой.")


@router.message(
    StateFilter(CreateTaskSG.assignee, CreateTaskSG.result_choice, CreateTaskSG.confirm, CreateTaskSG.edit_field),
    TextInput(),
)
async def on_unexpected_text(message: Message, state: FSMContext, session: AsyncSession, user: User | None) -> None:
    """Текст там, где ждём нажатия кнопки, — показать шаг заново."""
    if not await _guard_msg(message, state, user):
        return
    await _reshow(message, state, session, hint="👇 Выберите вариант кнопкой или нажмите «✖️ Отмена».")


@router.callback_query(StateFilter(CreateTaskSG), PickCB.filter(F.field != "cancel"))
async def on_stale_pick(callback: CallbackQuery) -> None:
    """Кнопка из предыдущего шага этого диалога — не подвешивать «часики»."""
    await callback.answer(STALE_BUTTON)


# --- Показ шагов -----------------------------------------------------------------------------


def _header(step: str, editing: bool) -> str:
    if editing:
        return "✏️ <b>Изменение черновика задачи</b>"
    return f"➕ <b>Новая задача</b> · шаг {STEP_NO.get(step, 1)} из {TOTAL_STEPS}"


async def _show_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    step: str,
    notice: str | None = None,
) -> None:
    """Перевести диалог на шаг ``step`` и показать вопрос (редактируя сообщение с кнопкой, если можно)."""
    data = await state.get_data()
    editing = bool(data.get("editing"))
    kb: InlineKeyboardMarkup

    if step == "assignee":
        employees = await users.list_employees(session)
        if not employees:
            if editing:
                await _show_summary(event, state, notice="⚠️ Нет активных сотрудников — исполнителя сейчас не изменить.")
            else:
                await state.clear()
                await common.edit_or_answer(event, NO_EMPLOYEES_TEXT)
            return
        body = "👤 Выберите <b>сотрудника</b>, которому ставите задачу:"
        kb = keyboards.choose_user_kb(employees, "assignee")
    elif step == "title":
        body = (
            f"📝 Введите короткое <b>название задачи</b> (до {TITLE_MAX} символов).\n"
            "Например: <i>Анализ договоров поставщиков</i>"
        )
        kb = keyboards.cancel_kb()
    elif step == "result":
        body = (
            "🎯 Опишите <b>ожидаемый результат</b> своими словами — что должно получиться в итоге.\n"
            "Например: <i>проверить 100 договоров и подготовить отчёт о нарушениях</i>\n\n"
            "Я помогу сделать формулировку измеримой."
        )
        kb = keyboards.cancel_kb()
    elif step == "plan":
        body = (
            "🔢 <b>Плановое число</b> (если применимо), например <i>100 договоров</i>.\n"
            "Введите число и единицу измерения или нажмите «⏭ Пропустить», если числового плана нет."
        )
        kb = keyboards.skip_cancel_kb("skip")
    elif step == "deadline":
        body = (
            "📅 Укажите <b>срок</b>: выберите кнопкой или напишите, например: "
            f"{DEADLINE_EXAMPLES}.\n"
            f"Если время не указано — до {esc(get_settings().default_deadline_time)}."
        )
        kb = keyboards.deadline_kb()
    elif step == "priority":
        body = "🚦 Выберите <b>приоритет</b> задачи:"
        kb = keyboards.priority_kb()
    elif step == "weight":
        load = await _week_load(session, data)
        body = "⚖️ Укажите <b>вес задачи</b> от 1 до 100 % — её долю в оценке эффективности сотрудника."
        if load is not None:
            body += (
                f"\nЗагрузка недели сотрудника: <b>{load} %</b>. "
                "Рекомендуется, чтобы сумма весов за неделю была ≈100 %."
            )
        body += "\nВыберите кнопкой или введите число."
        kb = keyboards.weight_kb(load)
    else:  # pragma: no cover - программная ошибка
        raise ValueError(f"Неизвестный шаг: {step}")

    if editing:
        current = _current_value(step, data)
        if current:
            body += f"\n\nСейчас: {current}"
    parts = [notice] if notice else []
    parts += [_header(step, editing), body]
    await state.set_state(STEP_STATES[step])
    await _show(event, state, "\n\n".join(parts), _with_cancel(kb))


async def _show_summary(
    event: Message | CallbackQuery,
    state: FSMContext,
    notice: str | None = None,
    *,
    edit_menu: bool = False,
) -> None:
    """Сводка черновика: [✅ Создать][✏️ Изменить] или меню выбора поля для изменения."""
    await state.update_data(editing=True, creating=False)
    data = await state.get_data()
    text = _draft_text(data)
    if edit_menu:
        text += "\n\n✏️ <b>Что изменить?</b>"
        kb = _with_cancel(keyboards.edit_fields_kb(EDIT_FIELDS), ("◀ К сводке", _BACK_TO_SUMMARY))
        await state.set_state(CreateTaskSG.edit_field)
    else:
        text += "\n\nВсё верно? Нажмите «✅ Создать» — или «✏️ Изменить», чтобы поправить."
        kb = _with_cancel(keyboards.confirm_kb("✅ Создать"))
        await state.set_state(CreateTaskSG.confirm)
    if notice:
        text = f"{notice}\n\n{text}"
    await _show(event, state, text, kb)


async def _show(
    event: Message | CallbackQuery, state: FSMContext, text: str, kb: InlineKeyboardMarkup
) -> None:
    """Показать вопрос диалога и запомнить его сообщение: работают только кнопки последнего вопроса.

    После ответа текстом вопрос задаётся новым сообщением, а у прежнего кнопки убираются —
    иначе после создания задачи выше остались бы «✖️ Отмена» и кнопки старых шагов.
    """
    if isinstance(event, Message):
        await _strip_prompt(event, state)
    msg = await common.edit_or_answer(event, text, kb)
    if msg is not None:
        await state.update_data(prompt_id=msg.message_id)


async def _strip_prompt(message: Message, state: FSMContext) -> None:
    """Убрать кнопки у последнего вопроса диалога (prompt_id), если он есть."""
    prompt_id = (await state.get_data()).get("prompt_id")
    if not isinstance(prompt_id, int) or message.bot is None:
        return
    try:
        await message.bot.edit_message_reply_markup(
            chat_id=message.chat.id, message_id=prompt_id, reply_markup=None
        )
    except TelegramAPIError:  # кнопок уже нет, сообщение удалено или слишком старое
        pass


async def _after_value(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    next_step: str,
    notice: str | None = None,
) -> None:
    """Значение сохранено: в режиме правки — назад к сводке, иначе — следующий шаг мастера."""
    data = await state.get_data()
    if data.get("editing"):
        await _show_summary(event, state, notice="✔️ Черновик обновлён.")
    elif next_step == "summary":
        await _show_summary(event, state)
    else:
        await _show_step(event, state, session, next_step, notice=notice)


async def _after_result(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, expected: str
) -> None:
    """Результат выбран: если плановое число неизвестно — спросить его, иначе дальше."""
    notice = f"🎯 Ожидаемый результат: <b>{esc(expected)}</b>"
    data = await state.get_data()
    if data.get("plan_value") is None:
        await _show_step(event, state, session, "plan", notice=notice)
    else:
        await _after_value(event, state, session, "deadline", notice=notice)


async def _reshow(message: Message, state: FSMContext, session: AsyncSession, hint: str) -> None:
    """Повторить вопрос текущего шага новым сообщением (кнопки могли уехать вверх)."""
    current = await state.get_state()
    data = await state.get_data()
    step = next((name for name, st in STEP_STATES.items() if st.state == current), None)
    if step is not None:
        await _show_step(message, state, session, step, notice=hint)
    elif current == CreateTaskSG.result_manual.state:
        await _show(message, state, _manual_text(data, notice=hint), _with_cancel(keyboards.cancel_kb()))
    elif current == CreateTaskSG.result_choice.state:
        if data.get("ai_busy") and not _ai_busy_stale(data):
            await message.answer(AI_BUSY_TEXT)
            return
        if data.get("ai_busy") or data.get("ai_lost") or not data.get("suggestion"):
            # Запрос к AI прервала остановка бота (состояние диалога пережило перезапуск) — не держать
            # черновик «занятым»: показать вариант по правилам.
            data = await _unstick_ai(state, data)
            hint = AI_LOST_NOTICE
        await _show(message, state, _suggestion_text(data, notice=hint), _ai_kb())
    elif current == CreateTaskSG.edit_field.state:
        await _show_summary(message, state, notice=hint, edit_menu=True)
    else:
        await _show_summary(message, state, notice=hint)


# --- Подсказка AI ----------------------------------------------------------------------------


async def _run_suggestion(
    state: FSMContext, msg: Message, *, title: str, raw_for_ai: str, raw: str, retry: bool
) -> None:
    """Запросить формулировку и показать её в ``msg`` с ai_suggestion_kb.

    Пока идёт запрос, ``ai_busy`` блокирует кнопки выбора. Если за это время диалог отменили
    или начали заново, результат молча отбрасывается. Бот останавливается посреди запроса
    (обновление на хостинге) — вместо ответа AI сохраняется вариант по правилам (его покажет
    следующее сообщение руководителя); жёсткую остановку покрывает ai_busy_since (_ai_busy_stale).
    """
    token = msg.message_id
    await state.set_state(CreateTaskSG.result_choice)
    await state.update_data(
        ai_busy=True, ai_busy_since=common.dt_to_state(utcnow()), prompt_id=token, suggestion=None
    )
    try:
        suggestion = await formulate.suggest_expected_result(title, raw_for_ai, deadline_text=None)
    except asyncio.CancelledError:
        try:
            await _settle_cancelled(state, token, title, raw)
        except Exception:  # noqa: BLE001 - останавливаемся; флаг снимет проверка «устаревания»
            log.debug("Не удалось сохранить вариант по правилам при остановке", exc_info=True)
        raise
    except Exception:  # noqa: BLE001 - по контракту не бросает; подстраховка, чтобы не потерять диалог
        log.exception("suggest_expected_result failed")
        suggestion = formulate.rules_suggestion(title, raw)

    notice = None
    if retry and suggestion.source != "ai":
        # Правила взяли бы текст вместе с «Предыдущий вариант: …» — берём исходные слова руководителя.
        suggestion = formulate.rules_suggestion(title, raw)
        notice = (
            "⚠️ AI сейчас недоступен — другой вариант предложить не получилось. "
            "Нажмите «✏️ Свой вариант», чтобы ввести формулировку самостоятельно."
        )

    data = await state.get_data()
    if await state.get_state() != CreateTaskSG.result_choice.state or data.get("prompt_id") != token:
        await _safe_delete(msg)  # диалог отменён или начат заново
        return

    await state.update_data(suggestion=_suggestion_dict(suggestion))
    data = await state.get_data()
    shown = msg
    try:
        shown = await _edit_message(msg, _suggestion_text(data, notice=notice), _ai_kb())
    finally:
        await state.update_data(ai_busy=False, ai_busy_since=None, prompt_id=shown.message_id)


def _ai_busy_stale_sec() -> float:
    """Через сколько секунд ai_busy точно брошен: каждая модель Gemini — не дольше ai_timeout_sec + 5 с
    (bot.ai.provider.generate_json), модели пробуются по очереди; плюс запас."""
    settings = get_settings()
    models = max(1, len([model for model in settings.gemini_models if model.strip()]))
    return (settings.ai_timeout_sec + 5) * models + _AI_BUSY_MARGIN_SEC


def _ai_busy_stale(data: dict[str, Any]) -> bool:
    """ai_busy остался от прерванного запроса (нет отметки времени — от старой версии бота — тоже)."""
    try:
        since = common.dt_from_state(data.get("ai_busy_since"))
    except (TypeError, ValueError):
        since = None
    return since is None or (utcnow() - since).total_seconds() > _ai_busy_stale_sec()


async def _unstick_ai(state: FSMContext, data: dict[str, Any]) -> dict[str, Any]:
    """Снять брошенный ai_busy: вариант по правилам из исходных слов руководителя. -> новые данные диалога."""
    suggestion = formulate.rules_suggestion(data.get("title") or "", data.get("raw_result") or "")
    await state.update_data(
        ai_busy=False, ai_busy_since=None, ai_lost=None, suggestion=_suggestion_dict(suggestion)
    )
    return await state.get_data()


async def _settle_cancelled(state: FSMContext, token: int, title: str, raw: str) -> None:
    """Остановка бота посреди запроса к AI: сохранить вариант по правилам, если диалог всё тот же
    (ai_lost — следующее сообщение руководителя покажет его с пояснением, что AI не ответил)."""
    data = await state.get_data()
    if await state.get_state() != CreateTaskSG.result_choice.state or data.get("prompt_id") != token:
        return
    suggestion = formulate.rules_suggestion(title, raw)
    await state.update_data(
        ai_busy=False, ai_busy_since=None, ai_lost=True, suggestion=_suggestion_dict(suggestion)
    )


def _suggestion_dict(suggestion: formulate.ResultSuggestion) -> dict[str, Any]:
    data = asdict(suggestion)
    data["plan_value"], data["plan_unit"] = _clean_plan(data.get("plan_value"), data.get("plan_unit"))
    return data


def _suggestion_text(data: dict[str, Any], notice: str | None = None) -> str:
    suggestion: dict[str, Any] = data.get("suggestion") or {}
    lines = [
        _header("result", bool(data.get("editing"))),
        "",
        f"📝 Задача: <b>{esc(data.get('title'))}</b>",
        f"💬 Вы написали: <i>{esc(data.get('raw_result'))}</i>",
        "",
        "🤖 <b>Предлагаю измеримую формулировку:</b>" if suggestion.get("source") == "ai"
        else "📐 <b>Подсказка (без AI):</b>",
        esc(suggestion.get("expected_result")),
    ]
    if suggestion.get("plan_value") is not None:
        lines.append(f"📊 План: <b>{_plan_str(suggestion['plan_value'], suggestion.get('plan_unit'))}</b>")
    if suggestion.get("note"):
        lines.append(f"💡 <i>{esc(suggestion['note'])}</i>")
    if notice:
        lines += ["", notice]
    lines += ["", "Принять формулировку, запросить другой вариант, ввести свой или оставить как написали?"]
    return "\n".join(lines)


def _manual_text(data: dict[str, Any], notice: str | None = None) -> str:
    parts = [notice] if notice else []
    parts += [
        _header("result", bool(data.get("editing"))),
        "✏️ Введите <b>свою формулировку</b> ожидаемого результата: что сделать, сколько и в какой форме сдать.\n"
        "Например: <i>Проверить 100 договоров и представить отчёт в Excel с перечнем нарушений</i>",
    ]
    suggestion: dict[str, Any] = data.get("suggestion") or {}
    if suggestion.get("expected_result"):
        parts.append(
            "Вариант-подсказка (нажмите, чтобы скопировать, и поправьте):\n"
            f"<code>{esc(suggestion['expected_result'])}</code>"
        )
    return "\n\n".join(parts)


def _ai_kb() -> InlineKeyboardMarkup:
    return _with_cancel(keyboards.ai_suggestion_kb())


async def _set_result(
    state: FSMContext, *, expected: str, raw: str, plan_value: Any, plan_unit: Any
) -> None:
    expected = expected.strip()
    raw = raw.strip()
    value, unit = _clean_plan(plan_value, plan_unit)
    same = " ".join(raw.split()).casefold() == " ".join(expected.split()).casefold()
    await state.update_data(
        expected_result=expected,
        description=None if not raw or same else raw,  # исходные слова руководителя, если отличаются
        plan_value=value,
        plan_unit=unit,
    )


# --- Сохранение значений ---------------------------------------------------------------------


async def _set_deadline(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, deadline: datetime
) -> None:
    if deadline.tzinfo is not None:  # по контракту dateparse отдаёт naive UTC; подстраховка
        deadline = deadline.astimezone(UTC).replace(tzinfo=None)
    await state.update_data(deadline=common.dt_to_state(deadline))
    await _after_value(event, state, session, "priority", notice=f"📅 Срок: <b>{esc(fmt_deadline(deadline))}</b>")


async def _set_priority(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, priority: Priority
) -> None:
    await state.update_data(priority=priority.value)
    await _after_value(event, state, session, "weight", notice=f"🚦 Приоритет: {_priority_label(priority.value)}")


async def _reask_deadline(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Срок в черновике уже в прошлом — спросить новый и вернуться к сводке."""
    await state.update_data(editing=True, creating=False)
    await _show_step(callback, state, session, "deadline",
                     notice="⚠️ Указанный срок уже прошёл — выберите новый.")
    await callback.answer("Срок уже прошёл — укажите новый.", show_alert=True)


async def _week_load(session: AsyncSession, data: dict[str, Any]) -> int | None:
    deadline = common.dt_from_state(data.get("deadline"))
    assignee_id = data.get("assignee_id")
    if deadline is None or assignee_id is None:
        return None
    try:
        return await tasks.weight_load(session, int(assignee_id), deadline)
    except OverflowError:  # срок у границы календаря — подсказка о загрузке не важнее самого диалога
        log.warning("weight_load: срок %s вне диапазона дат", deadline)
        return None


def _too_far(deadline: datetime) -> bool:
    return to_local(deadline).year > to_local(utcnow()).year + DEADLINE_MAX_YEARS


def _names_far_year(text: str) -> bool:
    """В тексте год дальше допустимого («31.12.9999») — объяснить это, а не «не понял срок»."""
    limit = to_local(utcnow()).year + DEADLINE_MAX_YEARS
    return any(int(year) > limit for year in _YEAR_RE.findall(text))


# --- Черновик --------------------------------------------------------------------------------


def _draft_text(data: dict[str, Any]) -> str:
    draft = {key: data.get(key) for key in DRAFT_KEYS}
    try:
        return render.task_summary_draft(draft)
    except Exception:  # noqa: BLE001 - сводка не должна обрывать диалог
        log.exception("render.task_summary_draft failed, using local summary")
        return _fallback_draft(draft)


def _fallback_draft(draft: dict[str, Any]) -> str:
    deadline = common.dt_from_state(draft.get("deadline"))
    lines = [
        "📋 <b>Новая задача — проверьте данные</b>",
        "",
        f"👤 Исполнитель: <b>{esc(draft.get('assignee_name'))}</b>",
        f"📝 Задача: <b>{esc(draft.get('title'))}</b>",
        f"🎯 Ожидаемый результат: {esc(draft.get('expected_result'))}",
    ]
    if draft.get("plan_value") is not None:
        lines.append(f"📊 План: {_plan_str(draft['plan_value'], draft.get('plan_unit'))}")
    lines += [
        f"📅 Срок: {esc(fmt_deadline(deadline)) if deadline else '—'}",
        f"🚦 Приоритет: {_priority_label(draft.get('priority'))}",
        f"⚖️ Вес: {esc(draft.get('weight'))} %",
    ]
    return "\n".join(lines)


def _current_value(step: str, data: dict[str, Any]) -> str | None:
    """Текущее значение поля для подсказки в режиме правки."""
    if step == "assignee":
        return f"<b>{esc(data.get('assignee_name'))}</b>" if data.get("assignee_name") else None
    if step == "title":
        return f"<code>{esc(data.get('title'))}</code>" if data.get("title") else None
    if step == "result":
        return f"<code>{esc(data.get('expected_result'))}</code>" if data.get("expected_result") else None
    if step == "plan":
        if data.get("plan_value") is None:
            return "без числового плана («⏭ Пропустить» — оставить без плана)"
        return f"<b>{_plan_str(data['plan_value'], data.get('plan_unit'))}</b> («⏭ Пропустить» — убрать план)"
    if step == "deadline":
        deadline = common.dt_from_state(data.get("deadline"))
        return f"<b>{esc(fmt_deadline(deadline))}</b>" if deadline else None
    if step == "priority":
        return _priority_label(data.get("priority")) if data.get("priority") else None
    if step == "weight":
        return f"<b>{esc(data.get('weight'))} %</b>" if data.get("weight") else None
    return None


# --- Мелкие хелперы --------------------------------------------------------------------------


async def _guard_msg(message: Message, state: FSMContext, user: User | None) -> bool:
    """Ставить задачи может только активный руководитель (роль могли снять посреди диалога)."""
    if common.is_manager(user):
        return True
    await state.clear()
    await message.answer(common.NO_RIGHTS)
    return False


async def _guard_cb(callback: CallbackQuery, state: FSMContext, user: User | None) -> bool:
    """Права (как _guard_msg) и кнопка из последнего вопроса этого диалога (см. prompt_id).

    Кнопки старых сообщений — из брошенного диалога или показанного заново шага — иначе
    сработали бы на текущий черновик: например, «✅ Создать» под сводкой с другим сотрудником.
    """
    if not common.is_manager(user):
        await state.clear()
        await common.deny(callback)
        return False
    message_id = callback.message.message_id if callback.message is not None else None
    if message_id is None or message_id != (await state.get_data()).get("prompt_id"):
        await callback.answer(STALE_BUTTON)
        return False
    return True


def _with_cancel(kb: InlineKeyboardMarkup, *extra: tuple[str, str]) -> InlineKeyboardMarkup:
    """Добавить кнопки ``extra`` (подпись, callback_data) и «✖️ Отмена», если их ещё нет в клавиатуре."""
    rows = [list(row) for row in kb.inline_keyboard]
    present = {button.callback_data for row in rows for button in row if button.callback_data}
    cancel_row = next(
        (i for i, row in enumerate(rows) if any((b.callback_data or "").startswith(_CANCEL_DATA) for b in row)),
        None,
    )
    insert_at = cancel_row if cancel_row is not None else len(rows)
    for text, data in extra:
        if data not in present:
            rows.insert(insert_at, [InlineKeyboardButton(text=text, callback_data=data)])
            insert_at += 1
    if cancel_row is None:
        rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data=_CANCEL_DATA)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_message(msg: Message, text: str, kb: InlineKeyboardMarkup | None) -> Message:
    """Отредактировать своё сообщение; если нельзя — отправить новое."""
    try:
        edited = await msg.edit_text(text, reply_markup=kb)
        return edited if isinstance(edited, Message) else msg
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return msg
        log.debug("edit_text failed, sending new message: %s", exc)
        return await msg.answer(text, reply_markup=kb)


async def _safe_delete(msg: Message) -> None:
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


def _parse_plan(text: str) -> tuple[float | None, str | None]:
    """«100 договоров» -> (100.0, «договоров»); «1 200» -> (1200.0, None); без числа -> (None, None)."""
    value = parse_number(text)
    if value is None:
        return None, None
    unit = None
    match = _PLAN_NUMBER_RE.search(text)
    if match is not None:
        unit_match = _PLAN_UNIT_RE.match(text, match.end())
        if unit_match is not None:
            unit = unit_match.group(1)[:UNIT_MAX]
    return value, unit


def _clean_plan(value: Any, unit: Any) -> tuple[float | None, str | None]:
    """План для create_task: число в (0, PLAN_MAX] (иначе без плана), единица — строка до 64 символов."""
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        number = None
    if number is None or not math.isfinite(number) or not 0 < number <= PLAN_MAX:
        return None, None
    unit_text = str(unit).strip()[:UNIT_MAX] if unit else None
    return number, unit_text or None


def _plan_str(value: Any, unit: Any) -> str:
    return f"{fmt_num(float(value))} {esc(unit or '')}".strip()


def _parse_weight(text: str) -> int | None:
    """Целый вес 1..100 из «20», «20 %», «вес 20». «1e309», «20,5», «-5» — None (а не «1 %»)."""
    match = _WEIGHT_RE.fullmatch((text or "").strip())
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 100 else None


def _priority_from_text(text: str) -> Priority | None:
    word = text.strip().lstrip("🔴🟡🟢 ").lower()
    for prefix, priority in _PRIORITY_WORDS:
        if word.startswith(prefix):
            return priority
    return None


def _priority_label(value: Any) -> str:
    try:
        return render.PRIORITY_LABELS[Priority(value)]
    except (ValueError, KeyError):
        return "—"
