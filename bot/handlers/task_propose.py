"""Поручения, внесённые сотрудником, и их подтверждение руководителем (SPEC 7.4).

Сотрудник («➕ Добавить поручение», /propose) вносит устное поручение:
название -> ожидаемый результат (AI-подсказка, как при постановке задачи) -> план (если нужно) ->
срок -> сводка -> propose_task -> commit -> notify_proposal руководителям.

Руководитель («📥 Предложения», /proposals, ListCB("proposals"), кнопки proposal_kb / task_actions_kb):
* TaskCB("approve") -> вес -> приоритет -> approve_proposal -> commit -> notify_proposal_decision(True);
* TaskCB("reject")  -> причина (или «Пропустить») -> reject_proposal -> commit -> notify_proposal_decision(False);
* TaskCB("pedit")   -> поле -> значение -> update_task -> commit -> notify_task_changed -> карточка + proposal_kb.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
from collections.abc import Awaitable
from dataclasses import asdict
from datetime import datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notify
from bot.ai import progress
from bot.ai.formulate import ResultSuggestion, rules_suggestion, suggest_expected_result
from bot.ai.provider import ai_available
from bot.db.models import Priority, Role, Task, TaskStatus, User, UserStatus
from bot.filters import IsEmployee, IsManager, TextInput
from bot.handlers import common
from bot.services import tasks as tasks_svc
from bot.services.errors import DomainError
from bot.ui import keyboards, render
from bot.ui.callbacks import ListCB, PickCB, TaskCB
from bot.ui.texts import BTN_PROPOSALS, BTN_PROPOSE, MENU_BUTTONS
from bot.utils.dateparse import FAR_YEAR_HINT, iso_to_deadline, names_far_year, parse_deadline
from bot.utils.dates import fmt_deadline, utcnow
from bot.utils.text import esc, fmt_num, parse_number, parse_percent, truncate

log = logging.getLogger(__name__)

router = Router(name="task_propose")

# --- Ограничения и тексты ------------------------------------------------------------------

TITLE_MAX = 255
RESULT_MAX = 2000
REASON_MAX = 1000
UNIT_MAX = 64
PAGE_SIZE = 8
# Ключ FSM-данных bot.handlers.task_view (K_BACK): последний открытый список для «◀ К списку задач».
TASK_VIEW_BACK_KEY = "tv_back"
MAX_AI_RETRIES = 3

ALREADY_PROCESSED = "Предложение уже обработано"
ASSIGNEE_INACTIVE = (
    "Подтвердить нельзя: исполнитель больше не активный сотрудник (заблокирован или сменил роль). "
    "Отклоните поручение или верните доступ (👥 Сотрудники)."
)
OWN_PROPOSAL = (
    "Это ваше поручение — вы вносили его, когда были сотрудником. Задачи ставятся только "
    "сотрудникам: если оно больше не актуально, отклоните его."
)
STALE_BUTTON = "Эта кнопка уже неактуальна — воспользуйтесь последним сообщением."
DRAFT_NOT_ALLOWED = "⛔ Вносить поручения могут только сотрудники — черновик поручения закрыт."
PRESS_BUTTON = "👆 Выберите вариант кнопкой в сообщении выше (или «✖️ Отмена»)."
SEND_TEXT = "✍️ Пожалуйста, отправьте ответ обычным текстом (или нажмите «✖️ Отмена»)."

DEADLINE_EXAMPLES = (
    "<i>завтра</i>, <i>в пятницу</i>, <i>через неделю</i>, <i>5 октября</i>, "
    "<i>05.10 18:00</i>, <i>конец месяца</i>"
)
PLAN_EXAMPLE = "<i>100 договоров</i>, <i>15 встреч</i>, <i>95 %</i>"

# Поля, которые можно изменить в черновике (сотрудник) и в предложении (руководитель).
EDIT_FIELDS: list[tuple[str, str]] = [
    ("title", "Название"),
    ("result", "Ожидаемый результат"),
    ("plan", "План (число)"),
    ("deadline", "Срок"),
]
CHANGE_LABELS = {
    "title": "название",
    "expected_result": "ожидаемый результат",
    "description": "описание",
    "plan_value": "плановое число",
    "plan_unit": "единица плана",
    "deadline": "срок",
    "priority": "приоритет",
    "weight": "вес",
}

# Первое число в тексте и слово после него: «100 договоров» -> («100», «договоров»), «95 %» -> «%».
_PLAN_UNIT_RE = re.compile(
    r"(?<![\w.,])(?:\d{1,3}(?:[   ]\d{3})+(?!\d)|\d+)(?:[.,]\d+)?\s*"
    r"(?P<unit>%|[^\W\d_]+(?:-[^\W\d_]+)*)?"
)

# Апдейты разных руководителей обрабатываются параллельно, и двое могут решать по одному
# предложению одновременно. Свежая проверка статуса, запись решения и commit идут под этим
# замком: второй руководитель дождётся коммита первого и получит «Предложение уже обработано».
# Внутри — только короткая работа с БД, без сообщений в Telegram.
_decision_lock = asyncio.Lock()


class ProposeTaskSG(StatesGroup):
    """Сотрудник вносит поручение."""

    title = State()
    result = State()          # своими словами -> AI-подсказка
    ai = State()              # выбор варианта формулировки (или свой вариант текстом)
    result_manual = State()   # ввод своего варианта
    plan = State()            # плановое число (можно пропустить)
    deadline = State()
    confirm = State()         # сводка: отправить / изменить
    edit_pick = State()       # выбор поля для изменения


class DecideProposalSG(StatesGroup):
    """Руководитель решает по предложению сотрудника."""

    weight = State()
    priority = State()
    reject_reason = State()
    edit_pick = State()
    edit_value = State()


# --- Общие хелперы ---------------------------------------------------------------------------


def _is_active_manager(user: User | None) -> bool:
    return user is not None and user.status == UserStatus.ACTIVE and user.role == Role.MANAGER


def _is_active_employee(user: User | None) -> bool:
    return user is not None and user.status == UserStatus.ACTIVE and user.role == Role.EMPLOYEE


async def _show(
    event: Message | CallbackQuery,
    state: FSMContext,
    text: str,
    kb: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Показать шаг диалога (редактируя сообщение с кнопкой или новым сообщением) и запомнить его id."""
    msg = await common.edit_or_answer(event, truncate(text), kb)
    if msg is not None:
        await state.update_data(msg_id=msg.message_id)
    return msg


async def _replace(msg: Message, text: str, kb: InlineKeyboardMarkup | None) -> Message:
    """Заменить текст сообщения «⏳ …» (если нельзя — отправить новое). -> показанное сообщение."""
    text = truncate(text)
    target: Message = msg
    try:
        edited = await msg.edit_text(text, reply_markup=kb)
        if isinstance(edited, Message):
            target = edited
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.debug("edit_text failed, sending new message: %s", exc)
            target = await msg.answer(text, reply_markup=kb)
    return target


def _chat_id(event: Message | CallbackQuery) -> int | None:
    if isinstance(event, Message):
        return event.chat.id
    return event.message.chat.id if event.message is not None else None


async def _stale(callback: CallbackQuery, state: FSMContext, data: dict[str, Any] | None = None) -> bool:
    """Кнопка нажата не в последнем сообщении диалога -> alert и True. data — уже прочитанные данные диалога."""
    if data is None:
        data = await state.get_data()
    msg_id = data.get("msg_id")
    if msg_id is not None and callback.message is not None and callback.message.message_id != msg_id:
        await callback.answer(STALE_BUTTON, show_alert=True)
        return True
    return False


async def _safe_notify(coro: Awaitable[Any]) -> Any:
    """Уведомление не должно ломать уже сохранённое действие. -> результат notify_* (None при ошибке)."""
    try:
        return await coro
    except Exception:  # noqa: BLE001
        log.exception("Не удалось отправить уведомление")
        return None


def _delivery_note(delivered: object, ok_text: str) -> str:
    """«Сотрудник получил уведомление» — только если оно действительно доставлено."""
    return ok_text if delivered else common.NOT_DELIVERED


def _is_cancel_button(button: InlineKeyboardButton) -> bool:
    try:
        return bool(button.callback_data) and PickCB.unpack(button.callback_data).field == "cancel"
    except (TypeError, ValueError):
        return False


def _with_back(kb: InlineKeyboardMarkup, text: str = "◀ Назад") -> InlineKeyboardMarkup:
    """Добавить к клавиатуре выбора поля кнопку «Назад» (PickCB("field", "back")) рядом с «Отмена»."""
    back = InlineKeyboardButton(text=text, callback_data=PickCB(field="field", value="back").pack())
    rows = [list(row) for row in kb.inline_keyboard]
    if rows and rows[-1] and all(_is_cancel_button(button) for button in rows[-1]):
        rows[-1] = [back, *rows[-1]]
    else:
        rows.append([back])
    return InlineKeyboardMarkup(inline_keyboard=rows)


BACK_TO_SUMMARY = "◀ К сводке"


def _draft_kb(data: dict[str, Any], kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Шаг черновика сотрудника. При правке из сводки — ещё и «◀ К сводке», чтобы передумать,
    не теряя черновик («✖️ Отмена» сбросила бы его целиком)."""
    return _with_back(kb, BACK_TO_SUMMARY) if data.get("editing") else kb


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold().rstrip(".") == " ".join(b.split()).casefold().rstrip(".")


def _plan_label(value: float | None, unit: str | None) -> str:
    if value is None:
        return "— <i>(без числа)</i>"
    return f"{fmt_num(value)} {esc(unit)}".strip() if unit else fmt_num(value)


def _parse_plan(text: str) -> tuple[float | None, str | None]:
    """«100 договоров» -> (100.0, «договоров»); «95 %» -> (95.0, «%»); нет числа / ≤ 0 / inf -> (None, None)."""
    value = parse_number(text)
    # Очень длинная строка цифр даёт float('inf') — такое число сервис всё равно не примет.
    if value is None or not math.isfinite(value) or value <= 0:
        return None, None
    match = _PLAN_UNIT_RE.search(text)
    unit = match.group("unit") if match else None
    return value, (unit[:UNIT_MAX] if unit else None)


def _deadline_error(text: str, deadline: datetime | None) -> str:
    """Почему срок не принят: опечатка в годе («31.12.9999») или просто непонятно / уже прошло."""
    if deadline is None and names_far_year(text):
        return f"🤔 {FAR_YEAR_HINT}"
    return f"🤔 Не понял срок «{truncate(esc(text), 60)}» или он уже прошёл."


def _deadline_or_none(iso_value: str) -> datetime | None:
    """ISO-дата из deadline_kb -> naive UTC; непонятное значение -> None."""
    try:
        return iso_to_deadline(iso_value)
    except (ValueError, TypeError):
        return None


def _task_head(task: Task) -> str:
    who = task.assignee.short_name if task.assignee else "—"
    return f"<b>#{task.id}</b> «{esc(task.title)}» — {esc(who)}"


# =============================================================================================
#  Сотрудник: «➕ Добавить поручение»
# =============================================================================================

STEP_TITLE = (
    "➕ <b>Новое поручение</b>\n\n"
    "Если поручение было дано устно или лично — внесите его, руководитель подтвердит.\n"
    "Так все поручения фиксируются в одной системе и войдут в вашу оценку эффективности.\n\n"
    "<b>Шаг 1/3.</b> Как называется задача? Коротко, до 255 символов.\n"
    "Например: <i>Анализ договоров поставщиков</i>"
)


@router.message(F.text == BTN_PROPOSE, IsEmployee())
@router.message(Command("propose"), IsEmployee())
async def propose_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ProposeTaskSG.title)
    await _show(message, state, STEP_TITLE, keyboards.cancel_kb())


# Роль сменили посреди черновика (повысили до руководителя) или черновик остался от прошлой роли:
# шаги ниже роль не проверяют, поэтому сначала — этот перехватчик. Блокировку ловит start.py,
# а users_admin при смене роли сам сбрасывает диалог; здесь — страховка на случай, если не удалось.


async def _draft_of_non_employee(
    event: TelegramObject, state: FSMContext | None = None, user: User | None = None
) -> bool:
    """Ввод в черновике поручения от того, кто сейчас не активный сотрудник.

    Кнопки главного меню и команды пропускаем: их обработают свои хендлеры (и сами сбросят диалог).
    """
    if state is None or _is_active_employee(user):
        return False
    current = await state.get_state()
    if current is None or current not in ProposeTaskSG:
        return False
    if isinstance(event, Message):
        text = (event.text or "").strip()
        return not (text.startswith("/") or text in MENU_BUTTONS)
    if isinstance(event, CallbackQuery):
        return (event.data or "").startswith(f"{PickCB.__prefix__}{PickCB.__separator__}")
    return False


@router.message(_draft_of_non_employee)
async def draft_not_allowed_message(message: Message, state: FSMContext, user: User | None) -> None:
    await state.clear()
    await message.answer(DRAFT_NOT_ALLOWED, reply_markup=keyboards.main_menu(user))


@router.callback_query(_draft_of_non_employee)
async def draft_not_allowed_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer(DRAFT_NOT_ALLOWED, show_alert=True)
    await common.remove_markup(callback)


# --- Шаг 1: название ---


async def _ask_title(event: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(ProposeTaskSG.title)
    current = data.get("title")
    text = "✏️ Введите новое название задачи (до 255 символов)."
    if current:
        text += f"\nСейчас: <i>{esc(current)}</i>"
    await _show(event, state, text, _draft_kb(data, keyboards.cancel_kb()))


@router.message(ProposeTaskSG.title, TextInput())
async def propose_title(message: Message, state: FSMContext) -> None:
    title = " ".join((message.text or "").split())
    kb = _draft_kb(await state.get_data(), keyboards.cancel_kb())
    if len(title) < 2:
        await _show(message, state, "Название слишком короткое. Напишите, что нужно сделать, например: "
                    "<i>Анализ договоров поставщиков</i>", kb)
        return
    if len(title) > TITLE_MAX:
        await _show(
            message,
            state,
            f"Название длинновато ({len(title)} символов). Сократите до {TITLE_MAX} — подробности "
            "укажете в ожидаемом результате.",
            kb,
        )
        return
    data = await state.update_data(title=title)
    if data.get("editing"):
        await _show_summary(message, state)
        return
    await _ask_result(message, state)


# --- Шаг 2: ожидаемый результат + AI ---


async def _ask_result(event: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(ProposeTaskSG.result)
    text = (
        f"📌 Задача: <b>{esc(data.get('title'))}</b>\n\n"
        "<b>Шаг 2/3.</b> Какой результат нужно получить? Опишите своими словами — "
        "я помогу сделать формулировку измеримой.\n"
        "Например: <i>проверить 100 договоров и представить отчёт с нарушениями</i>"
    )
    if data.get("editing") and data.get("expected_result"):
        text += f"\n\nСейчас: <i>{esc(data.get('expected_result'))}</i>"
    await _show(event, state, text, _draft_kb(data, keyboards.cancel_kb()))


@router.message(ProposeTaskSG.result, TextInput())
async def propose_result(message: Message, state: FSMContext, session: AsyncSession) -> None:
    raw = (message.text or "").strip()
    if len(raw) < 3 or len(raw) > RESULT_MAX:
        kb = _draft_kb(await state.get_data(), keyboards.cancel_kb())
        if len(raw) < 3:
            await _show(message, state, "Опишите результат чуть подробнее: что должно быть сделано или получено?", kb)
        else:
            await _show(message, state, f"Слишком длинно ({len(raw)} символов). Уложитесь в {RESULT_MAX}.", kb)
        return
    await _run_suggestion(message, state, session, retry=False, saved={"raw_result": raw, "ai_retries": 0})


async def _suggest(title: str, raw: str, deadline_text: str | None) -> ResultSuggestion:
    try:
        return await suggest_expected_result(title, raw, deadline_text)
    except Exception:  # noqa: BLE001 - подсказка не должна ломать диалог
        log.exception("suggest_expected_result failed")
        return rules_suggestion(title, raw)


async def _run_suggestion(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    *,
    retry: bool,
    saved: dict[str, Any] | None = None,
) -> None:
    """Сразу показать «⏳», запросить формулировку у AI (или правил), пока в чате «печатает…», и показать варианты.

    ``saved`` — значения диалога, которые записываются вместе с токеном запроса (одной операцией с хранилищем).
    """
    wait_text = "⏳ Формулирую другой вариант…" if retry else "⏳ Формулирую измеримый результат…"
    # «⏳» — до обращений к базе и к AI: сотрудник сразу видит, что бот работает.
    if isinstance(event, CallbackQuery):
        wait = await common.edit_or_answer(event, wait_text)
    else:
        wait = await event.answer(wait_text)

    # Уникальный токен запроса (а не счётчик: state.clear() сбросил бы счётчик в новом диалоге).
    req = secrets.token_hex(4)
    data = await state.update_data(**(saved or {}), ai_req=req, sug=None)
    await state.set_state(ProposeTaskSG.ai)

    # AI может отвечать долго — не держим транзакцию открытой.
    await session.commit()
    deadline = common.dt_from_state(data.get("deadline"))
    async with progress.typing(event.bot, _chat_id(event)):
        sug = await _suggest(
            data.get("title") or "",
            data.get("raw_result") or "",
            fmt_deadline(deadline) if deadline else None,
        )

    # Пока AI думал, пользователь мог отменить диалог или написать свой вариант.
    current = await state.get_state()
    fresh = await state.get_data()
    if current != ProposeTaskSG.ai.state or fresh.get("ai_req") != req:
        if wait is not None:
            try:
                await wait.delete()
            except TelegramBadRequest:
                pass
        return

    sug_data = asdict(sug)
    text = _suggestion_text({**fresh, "sug": sug_data})
    if retry and sug.source != "ai":
        # AI снова не ответил (лимит, сеть) — ничего «не предлагал», показаны правила.
        text += (
            "\n\n<i>AI по-прежнему недоступен — показана формулировка по правилам. "
            "Примите её или напишите свой вариант.</i>"
        )
    elif retry and fresh.get("prev_sug_text") == sug.expected_result:
        text += "\n\n<i>AI предложил тот же вариант — можно принять его или написать свой.</i>"
    # Сначала показать вариант, потом одной записью сохранить его и id сообщения (и при сбое показа).
    shown: Message | None = None
    try:
        if wait is not None:
            shown = await _replace(wait, text, keyboards.ai_suggestion_kb())
        else:
            shown = await common.edit_or_answer(event, truncate(text), keyboards.ai_suggestion_kb())
    finally:
        values: dict[str, Any] = {"sug": sug_data, "prev_sug_text": sug.expected_result}
        if shown is not None:
            values["msg_id"] = shown.message_id
        await state.update_data(**values)


def _suggestion_text(data: dict[str, Any]) -> str:
    sug = data.get("sug") or {}
    if sug.get("source") == "ai":
        head = "🤖 <b>Предлагаю измеримую формулировку результата:</b>"
    else:
        head = "📐 <b>Формулировка результата</b> <i>(AI сейчас недоступен — проверено по правилам)</i>:"
    lines = [head, "", f"<b>{esc(sug.get('expected_result'))}</b>"]
    if sug.get("plan_value") is not None:
        lines.append(f"📏 План: {_plan_label(sug.get('plan_value'), sug.get('plan_unit'))}")
    if sug.get("note"):
        lines.append(f"💡 {esc(sug.get('note'))}")
    lines += [
        "",
        f"Вы написали: <i>{esc(data.get('raw_result'))}</i>",
        "",
        "Выберите вариант кнопкой — или просто напишите свой текстом.",
    ]
    return "\n".join(lines)


@router.callback_query(ProposeTaskSG.ai, PickCB.filter(F.field == "ai"))
async def propose_ai_choice(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if user is None:
        await common.deny(callback)
        return
    data = await state.get_data()
    if await _stale(callback, state, data):
        return
    sug = data.get("sug")
    choice = callback_data.value

    if choice == "manual":
        await callback.answer()
        await state.set_state(ProposeTaskSG.result_manual)
        await _show(
            callback,
            state,
            "✏️ Напишите свой вариант ожидаемого результата.\n"
            "Хорошая формулировка отвечает на вопросы: <b>что</b> сделать, <b>сколько</b> и "
            "<b>в какой форме</b> сдаётся результат.\n"
            "Например: <i>Проверить 100 договоров и представить отчёт в Excel с перечнем нарушений</i>",
            keyboards.cancel_kb(),
        )
        return

    if not sug:
        await callback.answer("⏳ Ещё формулирую, подождите пару секунд…")
        return

    if choice == "retry":
        retries = int(data.get("ai_retries") or 0)
        if retries >= MAX_AI_RETRIES:
            await callback.answer("Вариантов достаточно — примите подходящий или напишите свой.", show_alert=True)
            return
        if sug.get("source") != "ai" and not ai_available():
            await callback.answer(
                "AI сейчас недоступен, другой вариант не получить. Напишите свой или оставьте как есть.",
                show_alert=True,
            )
            return
        await callback.answer()
        await _run_suggestion(callback, state, session, retry=True, saved={"ai_retries": retries + 1})
        return

    if choice == "accept":
        await callback.answer("✅ Принято")
        await _after_result(
            callback, state, sug.get("expected_result") or data.get("raw_result") or "",
            sug.get("plan_value"), sug.get("plan_unit"),
        )
        return

    if choice == "raw":
        await callback.answer()
        raw = data.get("raw_result") or ""
        parsed = rules_suggestion(data.get("title") or "", raw)
        await _after_result(callback, state, raw, parsed.plan_value, parsed.plan_unit)
        return

    await callback.answer()


@router.message(StateFilter(ProposeTaskSG.ai, ProposeTaskSG.result_manual), TextInput())
async def propose_result_manual(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.answer("Слишком коротко. Опишите, что должно быть сделано или получено.")
        return
    if len(text) > RESULT_MAX:
        await message.answer(f"Слишком длинно ({len(text)} символов). Уложитесь в {RESULT_MAX}.")
        return
    data = await state.get_data()
    if not data.get("raw_result"):
        await state.update_data(raw_result=text)
    parsed = rules_suggestion(data.get("title") or "", text)
    await _after_result(message, state, text, parsed.plan_value, parsed.plan_unit)


async def _after_result(
    event: Message | CallbackQuery,
    state: FSMContext,
    expected: str,
    plan_value: float | None,
    plan_unit: str | None,
) -> None:
    unit = plan_unit[:UNIT_MAX] if plan_unit else None
    data = await state.update_data(
        expected_result=expected.strip(),
        plan_value=float(plan_value) if plan_value is not None else None,
        plan_unit=unit if plan_value is not None else None,
    )
    if plan_value is None:
        await _ask_plan(event, state)
    elif data.get("editing") and data.get("deadline"):
        await _show_summary(event, state)
    else:
        await _ask_deadline(event, state)


# --- Шаг 2б: плановое число ---


async def _ask_plan(event: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(ProposeTaskSG.plan)
    text = (
        f"🎯 Результат: <i>{esc(data.get('expected_result'))}</i>\n\n"
        "📏 <b>Плановое число</b> (если применимо) — по нему потом сравнят план и факт.\n"
        f"Например: {PLAN_EXAMPLE}.\n"
        "Если измерить числом нельзя — нажмите «⏭ Пропустить»."
    )
    if data.get("editing") and data.get("plan_value") is not None:
        text += f"\n\nСейчас: {_plan_label(data.get('plan_value'), data.get('plan_unit'))}"
    await _show(event, state, text, _draft_kb(data, keyboards.skip_cancel_kb()))


@router.message(ProposeTaskSG.plan, TextInput())
async def propose_plan(message: Message, state: FSMContext) -> None:
    value, unit = _parse_plan(message.text or "")
    data = await state.get_data()
    if value is None:
        await _show(
            message,
            state,
            f"Не нашёл положительного числа. Напишите, например: {PLAN_EXAMPLE} — или нажмите «⏭ Пропустить».",
            _draft_kb(data, keyboards.skip_cancel_kb()),
        )
        return
    if unit is None and data.get("plan_value") is not None:
        unit = data.get("plan_unit")  # «120» при правке — единица остаётся прежней
    await state.update_data(plan_value=value, plan_unit=unit)
    await _after_plan(message, state)


@router.callback_query(ProposeTaskSG.plan, PickCB.filter(F.field == "skip"))
async def propose_plan_skip(callback: CallbackQuery, state: FSMContext) -> None:
    if await _stale(callback, state):
        return
    await callback.answer()
    await state.update_data(plan_value=None, plan_unit=None)
    await _after_plan(callback, state)


async def _after_plan(event: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("editing") and data.get("deadline"):
        await _show_summary(event, state)
    else:
        await _ask_deadline(event, state)


# --- Шаг 3: срок ---


async def _ask_deadline(event: Message | CallbackQuery, state: FSMContext, error: str | None = None) -> None:
    data = await state.get_data()
    await state.set_state(ProposeTaskSG.deadline)
    lines = []
    if error:
        lines += [error, ""]
    lines.append(
        "<b>Шаг 3/3.</b> ⏰ Какой срок выполнения? Выберите кнопкой или напишите, например: "
        f"{DEADLINE_EXAMPLES}."
    )
    current = common.dt_from_state(data.get("deadline"))
    kb = keyboards.deadline_kb()
    if data.get("editing") and current:
        lines.append(f"\nСейчас: {fmt_deadline(current)}")
        if current > utcnow():  # прошедший срок оставить нельзя — назад к сводке не ведём
            kb = _draft_kb(data, kb)
    await _show(event, state, "\n".join(lines), kb)


@router.message(ProposeTaskSG.deadline, TextInput())
async def propose_deadline_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    deadline = parse_deadline(text)
    if deadline is None or deadline <= utcnow():
        await _ask_deadline(message, state, _deadline_error(text, deadline))
        return
    await state.update_data(deadline=common.dt_to_state(deadline))
    await _show_summary(message, state)


@router.callback_query(ProposeTaskSG.deadline, PickCB.filter(F.field == "deadline"))
async def propose_deadline_pick(callback: CallbackQuery, callback_data: PickCB, state: FSMContext) -> None:
    if await _stale(callback, state):
        return
    deadline = _deadline_or_none(callback_data.value)
    if deadline is None or deadline <= utcnow():
        await callback.answer("Этот срок уже прошёл — выберите другой или напишите дату.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(deadline=common.dt_to_state(deadline))
    await _show_summary(callback, state)


# --- Сводка и отправка ---


def _draft_text(data: dict[str, Any]) -> str:
    deadline = common.dt_from_state(data.get("deadline"))
    lines = [
        "📝 <b>Проверьте поручение перед отправкой</b>",
        "",
        f"📌 <b>Задача:</b> {esc(data.get('title'))}",
        f"🎯 <b>Ожидаемый результат:</b> {esc(data.get('expected_result'))}",
        f"📏 <b>План:</b> {_plan_label(data.get('plan_value'), data.get('plan_unit'))}",
        f"⏰ <b>Срок:</b> {fmt_deadline(deadline) if deadline else '—'}",
        "👤 <b>Исполнитель:</b> вы",
        "",
        "Руководитель подтвердит поручение (укажет вес и приоритет) или скорректирует его.",
    ]
    return "\n".join(lines)


async def _show_summary(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProposeTaskSG.confirm)
    await state.update_data(editing=False)
    data = await state.get_data()
    await _show(event, state, _draft_text(data), keyboards.confirm_kb("📤 Отправить руководителю"))


@router.callback_query(ProposeTaskSG.confirm, PickCB.filter(F.field == "confirm"))
async def propose_confirm(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
) -> None:
    if not _is_active_employee(user):
        await common.deny(callback)
        return
    if await _stale(callback, state):
        return

    if callback_data.value == "edit":
        await callback.answer()
        await state.set_state(ProposeTaskSG.edit_pick)
        await _show(
            callback,
            state,
            _draft_text(await state.get_data()) + "\n\n✏️ <b>Что изменить?</b>",
            _with_back(keyboards.edit_fields_kb(EDIT_FIELDS), "◀ К сводке"),
        )
        return
    if callback_data.value != "yes":
        await callback.answer()
        return

    data = await state.get_data()
    deadline = common.dt_from_state(data.get("deadline"))
    if deadline is None or deadline <= utcnow():
        await callback.answer()
        await state.update_data(editing=True)
        await _ask_deadline(callback, state, "⌛ Указанный срок уже прошёл — выберите новый.")
        return

    # Сбрасываем состояние сразу — повторное нажатие не создаст дубль.
    await state.clear()
    raw = (data.get("raw_result") or "").strip()
    expected = (data.get("expected_result") or "").strip()
    try:
        task = await tasks_svc.propose_task(
            session,
            employee=user,
            title=data.get("title") or "",
            expected_result=expected,
            deadline=deadline,
            description=raw if raw and not _same_text(raw, expected) else None,
            plan_value=data.get("plan_value"),
            plan_unit=data.get("plan_unit"),
        )
    except DomainError as exc:
        await state.set_state(ProposeTaskSG.confirm)
        await state.set_data(data)
        await callback.answer(exc.message, show_alert=True)
        return

    await session.commit()
    await callback.answer("📤 Отправлено")
    notified = await _safe_notify(notify.notify_proposal(bot, session, task))
    if notified:
        head = (
            f"📤 <b>Поручение #{task.id} отправлено руководителю на подтверждение.</b>\n"
            "Когда руководитель подтвердит или скорректирует его, я пришлю уведомление."
        )
    else:
        # Руководителей в боте нет (или ни до кого не дошло): не обещать, что поручение уже у руководителя.
        head = (
            f"📥 <b>Поручение #{task.id} сохранено</b>, но уведомить руководителя сейчас не удалось — "
            "в боте нет активного руководителя.\n"
            "Поручение ждёт в «📥 Предложения»; сообщите руководителю о нём лично."
        )
    await common.edit_or_answer(
        callback,
        # вид исполнителя: «Исполнитель: вы» и так ясно
        truncate(f"{head}\n\n{render.task_card(task, show_assignee=False)}"),
    )


@router.callback_query(ProposeTaskSG.edit_pick, PickCB.filter(F.field == "field"))
async def propose_edit_field(callback: CallbackQuery, callback_data: PickCB, state: FSMContext) -> None:
    if await _stale(callback, state):
        return
    await callback.answer()
    key = callback_data.value
    if key == "back":
        await _show_summary(callback, state)
        return
    await state.update_data(editing=True)
    if key == "title":
        await _ask_title(callback, state)
    elif key == "result":
        await _ask_result(callback, state)
    elif key == "plan":
        await _ask_plan(callback, state)
    elif key == "deadline":
        await _ask_deadline(callback, state)
    else:
        await _show_summary(callback, state)


@router.callback_query(
    StateFilter(ProposeTaskSG.title, ProposeTaskSG.result, ProposeTaskSG.plan, ProposeTaskSG.deadline),
    PickCB.filter((F.field == "field") & (F.value == "back")),
)
async def propose_edit_back(callback: CallbackQuery, state: FSMContext) -> None:
    """«◀ К сводке» при правке поля черновика: передумал менять — черновик без изменений."""
    if await _stale(callback, state):
        return
    data = await state.get_data()
    deadline = common.dt_from_state(data.get("deadline"))
    if not data.get("editing") or deadline is None:
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    await callback.answer()
    await _show_summary(callback, state)


# =============================================================================================
#  Руководитель: «📥 Предложения»
# =============================================================================================


def _proposals_text(page_tasks: list[Task], total: int, page: int) -> str:
    if total == 0:
        return (
            "📥 <b>Предложения сотрудников</b>\n\n"
            "Новых предложений нет. Когда сотрудник внесёт поручение, я пришлю уведомление."
        )
    lines = [
        f"📥 <b>Предложения сотрудников</b> ({total})",
        "Поручения, которые сотрудники внесли сами (устные и личные поручения). "
        "Откройте, чтобы подтвердить, изменить или отклонить.",
        "",
    ]
    start = page * PAGE_SIZE
    for num, task in enumerate(page_tasks, start=start + 1):
        lines.append(f"{num}. {render.task_line(task, with_assignee=True)}")
    return "\n".join(lines)


async def _show_proposals(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, page: int = 0
) -> None:
    proposals = await tasks_svc.list_proposals(session)
    total = len(proposals)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    page_tasks = proposals[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    # Карточку из списка открывает task_view; её «◀ К списку задач» ведёт в последний открытый
    # список — запоминаем очередь предложений (и страницу) тем же ключом, что и task_view.
    await state.update_data({TASK_VIEW_BACK_KEY: ListCB(scope="proposals", status="all", page=page).pack()})
    kb = (
        keyboards.task_list_kb(
            page_tasks, "proposals", "all", page, total, page_size=PAGE_SIZE, status_tabs=False
        )
        if total
        else None
    )
    await common.edit_or_answer(event, truncate(_proposals_text(page_tasks, total, page)), kb)


@router.message(F.text == BTN_PROPOSALS, IsManager())
@router.message(Command("proposals"), IsManager())
async def proposals_menu(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await _show_proposals(message, state, session, 0)


@router.callback_query(ListCB.filter(F.scope == "proposals"))
async def proposals_page(
    callback: CallbackQuery, callback_data: ListCB, state: FSMContext, session: AsyncSession, user: User | None
) -> None:
    if not _is_active_manager(user):
        await common.deny(callback)
        return
    await callback.answer()
    await _show_proposals(callback, state, session, callback_data.page)


PROCESSED_HEAD = f"ℹ️ <b>{ALREADY_PROCESSED}.</b> Актуальное состояние:"


async def _show_card(callback: CallbackQuery, task: Task, user: User, head: str) -> None:
    """Решение не прошло (или кнопки устарели): заменить сообщение актуальной карточкой с кнопками
    по текущему статусу — для PROPOSED снова решение, иначе действия с задачей. Так по мёртвым
    кнопкам старого уведомления больше не нажимают, а после ошибки сразу видно, что делать дальше."""
    await common.edit_or_answer(
        callback, truncate(f"{head}\n\n{render.task_card(task)}"), keyboards.task_actions_kb(task, user)
    )


async def _load_proposal(
    callback: CallbackQuery, session: AsyncSession, task_id: int, user: User
) -> Task | None:
    """Задача-предложение для решения; иначе alert и None (callback уже отвечен)."""
    task = await tasks_svc.get_task(session, task_id)
    if task is None:
        await callback.answer(common.NOT_FOUND, show_alert=True)
        return None
    if task.status != TaskStatus.PROPOSED:
        await callback.answer(ALREADY_PROCESSED, show_alert=True)
        await _show_card(callback, task, user, PROCESSED_HEAD)
        return None
    return task


async def _decision_task(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, user: User | None
) -> Task | None:
    """Задача из FSM руководителя; если прав нет или предложение уже обработано — сообщить и сбросить диалог."""
    data = await state.get_data()
    task = await tasks_svc.get_task(session, int(data.get("task_id") or 0))
    problem: str | None = None
    if not _is_active_manager(user):
        problem = common.NO_RIGHTS
    elif task is None:
        problem = common.NOT_FOUND
    elif task.status != TaskStatus.PROPOSED:
        problem = ALREADY_PROCESSED
    if problem is None:
        return task
    await state.clear()
    if isinstance(event, CallbackQuery):
        await event.answer(problem, show_alert=True)
        if problem == ALREADY_PROCESSED and task is not None and user is not None:
            await _show_card(event, task, user, PROCESSED_HEAD)
    else:
        await event.answer(problem)
    return None


# --- ✅ Подтвердить: вес -> приоритет ---


@router.callback_query(TaskCB.filter(F.action == "approve"))
async def proposal_approve(
    callback: CallbackQuery,
    callback_data: TaskCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if not _is_active_manager(user):
        await common.deny(callback)
        return
    task = await _load_proposal(callback, session, callback_data.task_id, user)
    if task is None:
        return
    # Те же проверки, что сделает approve_proposal, — но сразу, а не после выбора веса и приоритета.
    if not _is_active_employee(task.assignee):
        # Своё поручение видит руководитель, которого повысили из сотрудников.
        await callback.answer(OWN_PROPOSAL if task.assignee_id == user.id else ASSIGNEE_INACTIVE, show_alert=True)
        return
    if task.deadline <= utcnow():
        await callback.answer(
            "Срок этого поручения уже прошёл. Сначала измените срок: «✏️ Изменить» → «Срок».",
            show_alert=True,
        )
        return
    await callback.answer()
    await state.clear()
    await state.set_state(DecideProposalSG.weight)
    await state.update_data(task_id=task.id)
    await _ask_weight(callback, state, session, task)


async def _ask_weight(
    event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, task: Task, error: str | None = None
) -> None:
    load = await tasks_svc.weight_load(session, task.assignee_id, task.deadline, exclude_task_id=task.id)
    lines = [f"✅ <b>Подтверждение поручения</b> {_task_head(task)}", ""]
    if error:
        lines += [error, ""]
    lines += [
        "<b>Шаг 1/2.</b> ⚖️ Укажите вес задачи (1–100 %) — кнопкой или числом.",
        f"Сейчас у сотрудника на неделе срока: <b>{load} %</b>. "
        "Рекомендуется, чтобы сумма весов за неделю была ≈100 %.",
    ]
    await _show(event, state, "\n".join(lines), keyboards.weight_kb(load))


async def _set_weight(event: Message | CallbackQuery, state: FSMContext, task: Task, weight: int) -> None:
    await state.update_data(weight=weight)
    await state.set_state(DecideProposalSG.priority)
    text = (
        f"✅ <b>Подтверждение поручения</b> {_task_head(task)}\n\n"
        f"⚖️ Вес: <b>{weight} %</b>\n\n"
        "<b>Шаг 2/2.</b> Выберите приоритет:"
    )
    await _show(event, state, text, keyboards.priority_kb())


@router.callback_query(DecideProposalSG.weight, PickCB.filter(F.field == "weight"))
async def proposal_weight_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if await _stale(callback, state):
        return
    task = await _decision_task(callback, state, session, user)
    if task is None:
        return
    try:
        weight = int(callback_data.value)
    except ValueError:
        weight = 0
    if not 1 <= weight <= 100:
        await callback.answer("Вес — от 1 до 100 %", show_alert=True)
        return
    await callback.answer()
    await _set_weight(callback, state, task, weight)


@router.message(DecideProposalSG.weight, TextInput())
async def proposal_weight_text(
    message: Message, state: FSMContext, session: AsyncSession, user: User | None
) -> None:
    task = await _decision_task(message, state, session, user)
    if task is None:
        return
    value = parse_percent(message.text or "")
    # Сначала диапазон: int(inf) от длинной строки цифр бросил бы OverflowError.
    if value is None or not 1 <= value <= 100 or value != int(value):
        await _ask_weight(message, state, session, task, "Вес — целое число от 1 до 100, например <i>20</i>.")
        return
    await _set_weight(message, state, task, int(value))


@router.callback_query(DecideProposalSG.priority, PickCB.filter(F.field == "prio"))
async def proposal_priority_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
) -> None:
    if await _stale(callback, state):
        return
    task = await _decision_task(callback, state, session, user)
    if task is None:
        return
    try:
        priority = Priority(callback_data.value)
    except ValueError:
        await callback.answer("Неизвестный приоритет", show_alert=True)
        return
    data = await state.get_data()
    weight = int(data.get("weight") or 0)
    await state.clear()
    try:
        async with _decision_lock:
            await session.refresh(task)  # свежий статус: сервис сам проверит, что задача ещё PROPOSED
            task = await tasks_svc.approve_proposal(session, task.id, user, weight=weight, priority=priority)
            await session.commit()
    except DomainError as exc:
        await callback.answer(exc.message, show_alert=True)
        # Например, срок прошёл, пока выбирали вес: снова карточка с кнопками решения («✏️ Изменить»).
        head = f"⚠️ {esc(exc.message)}" if task.status == TaskStatus.PROPOSED else PROCESSED_HEAD
        await _show_card(callback, task, user, head)
        return
    await callback.answer("✅ Подтверждено")
    delivered = await _safe_notify(notify.notify_proposal_decision(bot, task, True))
    note = _delivery_note(delivered, "сотрудник получил уведомление.")
    await common.edit_or_answer(
        callback,
        truncate(
            f"✅ <b>Подтверждено.</b> Поручение #{task.id} в работе"
            + (f", {note}" if delivered else f".\n{note}")
            + "\n\n"
            + render.task_card(task)
        ),
        keyboards.task_actions_kb(task, user),
    )


# --- ❌ Отклонить: причина ---


@router.callback_query(TaskCB.filter(F.action == "reject"))
async def proposal_reject(
    callback: CallbackQuery,
    callback_data: TaskCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if not _is_active_manager(user):
        await common.deny(callback)
        return
    task = await _load_proposal(callback, session, callback_data.task_id, user)
    if task is None:
        return
    await callback.answer()
    await state.clear()
    await state.set_state(DecideProposalSG.reject_reason)
    await state.update_data(task_id=task.id)
    await _show(
        callback,
        state,
        f"❌ <b>Отклонение поручения</b> {_task_head(task)}\n\n"
        "Напишите причину — сотрудник её увидит. Или нажмите «⏭ Пропустить».",
        keyboards.skip_cancel_kb(),
    )


async def _do_reject(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    bot: Bot,
    task: Task,
    reason: str | None,
) -> None:
    await state.clear()
    try:
        async with _decision_lock:
            await session.refresh(task)  # свежий статус: сервис сам проверит, что задача ещё PROPOSED
            task = await tasks_svc.reject_proposal(session, task.id, user, reason)
            await session.commit()
    except DomainError as exc:
        if isinstance(event, CallbackQuery):
            await event.answer(exc.message, show_alert=True)
            await _show_card(event, task, user, PROCESSED_HEAD)
        else:
            await event.answer(exc.message)
        return
    if isinstance(event, CallbackQuery):
        await event.answer("❌ Отклонено")
    delivered = await _safe_notify(notify.notify_proposal_decision(bot, task, False, reason))
    note = _delivery_note(delivered, "Сотрудник получил уведомление.")
    text = f"❌ <b>Предложение #{task.id} отклонено.</b>" + (f" {note}" if delivered else f"\n{note}")
    if reason:
        text += f"\nПричина: <i>{esc(reason)}</i>"
    await common.edit_or_answer(event, truncate(text + "\n\n" + render.task_card(task)))


@router.message(DecideProposalSG.reject_reason, TextInput())
async def proposal_reject_reason(
    message: Message, state: FSMContext, session: AsyncSession, user: User | None, bot: Bot
) -> None:
    task = await _decision_task(message, state, session, user)
    if task is None:
        return
    reason = (message.text or "").strip()
    if len(reason) > REASON_MAX:
        await message.answer(f"Слишком длинно ({len(reason)} символов). Уложитесь в {REASON_MAX}.")
        return
    await _do_reject(message, state, session, user, bot, task, reason or None)


@router.callback_query(DecideProposalSG.reject_reason, PickCB.filter(F.field == "skip"))
async def proposal_reject_skip(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User | None, bot: Bot
) -> None:
    if await _stale(callback, state):
        return
    task = await _decision_task(callback, state, session, user)
    if task is None:
        return
    await _do_reject(callback, state, session, user, bot, task, None)


# --- ✏️ Изменить предложение ---


@router.callback_query(TaskCB.filter(F.action == "pedit"))
async def proposal_edit(
    callback: CallbackQuery,
    callback_data: TaskCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if not _is_active_manager(user):
        await common.deny(callback)
        return
    task = await _load_proposal(callback, session, callback_data.task_id, user)
    if task is None:
        return
    await callback.answer()
    await state.clear()
    await state.update_data(task_id=task.id)
    await _ask_edit_field(callback, state, task)


async def _ask_edit_field(event: Message | CallbackQuery, state: FSMContext, task: Task) -> None:
    await state.set_state(DecideProposalSG.edit_pick)
    text = (
        render.task_card(task)
        + "\n\n✏️ <b>Что изменить?</b> Сотрудник получит уведомление об исправлении."
    )
    await _show(event, state, text, _with_back(keyboards.edit_fields_kb(EDIT_FIELDS)))


def _plan_edit_kb(task: Task) -> InlineKeyboardMarkup:
    cancel = keyboards.cancel_kb()
    if task.plan_value is None:
        return cancel
    remove = InlineKeyboardButton(
        text="🚫 Убрать число из плана", callback_data=PickCB(field="plan", value="none").pack()
    )
    return InlineKeyboardMarkup(inline_keyboard=[[remove], *cancel.inline_keyboard])


async def _ask_edit_value(
    event: Message | CallbackQuery, state: FSMContext, task: Task, field: str, error: str | None = None
) -> None:
    await state.set_state(DecideProposalSG.edit_value)
    await state.update_data(field=field)
    head = f"✏️ <b>Изменение поручения</b> {_task_head(task)}\n\n"
    if error:
        head += f"{error}\n\n"
    if field == "title":
        text = f"Введите новое название (до {TITLE_MAX} символов).\nСейчас: <i>{esc(task.title)}</i>"
        kb = keyboards.cancel_kb()
    elif field == "result":
        text = (
            "Введите новый ожидаемый результат — измеримо: что, сколько, в какой форме сдаётся.\n"
            f"Сейчас: <i>{esc(task.expected_result)}</i>\n"
            "<i>Плановое число меняется отдельно — пункт «План (число)».</i>"
        )
        kb = keyboards.cancel_kb()
    elif field == "plan":
        text = (
            f"Введите плановое число, например: {PLAN_EXAMPLE}.\n"
            f"Сейчас: {_plan_label(task.plan_value, task.plan_unit)}"
        )
        kb = _plan_edit_kb(task)
    else:
        text = (
            f"Введите новый срок — кнопкой или текстом, например: {DEADLINE_EXAMPLES}.\n"
            f"Сейчас: {fmt_deadline(task.deadline)}"
        )
        kb = keyboards.deadline_kb()
    # «◀ Назад» — к выбору поля: передумал менять это поле, но не всю правку.
    await _show(event, state, head + text, _with_back(kb))


@router.callback_query(DecideProposalSG.edit_pick, PickCB.filter(F.field == "field"))
async def proposal_edit_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    if await _stale(callback, state):
        return
    task = await _decision_task(callback, state, session, user)
    if task is None:
        return
    await callback.answer()
    key = callback_data.value
    if key == "back":
        await state.clear()
        await common.edit_or_answer(callback, truncate(render.task_card(task)), keyboards.proposal_kb(task))
        return
    if key not in {"title", "result", "plan", "deadline"}:
        await _ask_edit_field(callback, state, task)
        return
    await _ask_edit_value(callback, state, task, key)


@router.callback_query(DecideProposalSG.edit_value, PickCB.filter((F.field == "field") & (F.value == "back")))
async def proposal_edit_value_back(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User | None
) -> None:
    """«◀ Назад» при вводе значения — снова выбор поля."""
    if await _stale(callback, state):
        return
    task = await _decision_task(callback, state, session, user)
    if task is None:
        return
    await callback.answer()
    await _ask_edit_field(callback, state, task)


async def _apply_edit(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    bot: Bot,
    task: Task,
    field: str,
    **fields: Any,
) -> None:
    """update_task -> commit -> notify_task_changed -> карточка с proposal_kb. Ошибка — переспросить."""
    try:
        async with _decision_lock:
            # Свежий статус: другой руководитель мог только что подтвердить или отклонить предложение
            # (update_task сам пропустил бы и ACTIVE-задачу).
            await session.refresh(task)
            if task.status != TaskStatus.PROPOSED:
                raise DomainError(ALREADY_PROCESSED)
            task, changes = await tasks_svc.update_task(session, task.id, user, **fields)
            await session.commit()
    except DomainError as exc:
        # update_task проверяет значения до изменения задачи — откат не нужен, переспрашиваем.
        if task.status != TaskStatus.PROPOSED:
            await state.clear()
            if isinstance(event, CallbackQuery):
                await event.answer(exc.message, show_alert=True)
                await _show_card(event, task, user, PROCESSED_HEAD)
            else:
                await event.answer(exc.message)
            return
        if isinstance(event, CallbackQuery):
            await event.answer()
        await _ask_edit_value(event, state, task, field, f"⚠️ {esc(exc.message)}")
        return

    await state.clear()
    if isinstance(event, CallbackQuery):
        await event.answer("✅ Сохранено" if changes else "Без изменений")
    if changes:
        delivered = await _safe_notify(notify.notify_task_changed(bot, task, changes))
        labels = ", ".join(dict.fromkeys(CHANGE_LABELS.get(name, name) for name in changes))
        note = _delivery_note(delivered, "Сотрудник получил уведомление об исправлении.")
        head = f"✅ Изменено: {labels}." + (f" {note}" if delivered else f"\n{note}")
    else:
        head = "Ничего не изменилось."
    await common.edit_or_answer(
        event,
        truncate(head + "\n\n" + render.task_card(task) + "\n\nПодтвердить поручение?"),
        keyboards.proposal_kb(task),
    )


@router.message(DecideProposalSG.edit_value, TextInput())
async def proposal_edit_value(
    message: Message, state: FSMContext, session: AsyncSession, user: User | None, bot: Bot
) -> None:
    task = await _decision_task(message, state, session, user)
    if task is None:
        return
    field = (await state.get_data()).get("field")
    text = (message.text or "").strip()

    if field == "title":
        title = " ".join(text.split())
        if not 2 <= len(title) <= TITLE_MAX:
            await _ask_edit_value(message, state, task, field, f"Название — от 2 до {TITLE_MAX} символов.")
            return
        await _apply_edit(message, state, session, user, bot, task, field, title=title)
    elif field == "result":
        if not 3 <= len(text) <= RESULT_MAX:
            await _ask_edit_value(message, state, task, field, f"Результат — от 3 до {RESULT_MAX} символов.")
            return
        await _apply_edit(message, state, session, user, bot, task, field, expected_result=text)
    elif field == "plan":
        value, unit = _parse_plan(text)
        if value is None:
            await _ask_edit_value(message, state, task, field, "Не нашёл положительного числа.")
            return
        fields: dict[str, Any] = {"plan_value": value}
        if unit is not None or task.plan_value is None:
            fields["plan_unit"] = unit  # «120» без единицы — единица остаётся прежней
        await _apply_edit(message, state, session, user, bot, task, field, **fields)
    elif field == "deadline":
        deadline = parse_deadline(text)
        if deadline is None or deadline <= utcnow():
            await _ask_edit_value(message, state, task, field, _deadline_error(text, deadline))
            return
        await _apply_edit(message, state, session, user, bot, task, field, deadline=deadline)
    else:
        await _ask_edit_field(message, state, task)


@router.callback_query(DecideProposalSG.edit_value, PickCB.filter(F.field == "deadline"))
async def proposal_edit_deadline_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
) -> None:
    if await _stale(callback, state):
        return
    task = await _decision_task(callback, state, session, user)
    if task is None:
        return
    deadline = _deadline_or_none(callback_data.value)
    if deadline is None or deadline <= utcnow():
        await callback.answer("Этот срок уже прошёл — выберите другой или напишите дату.", show_alert=True)
        return
    await _apply_edit(callback, state, session, user, bot, task, "deadline", deadline=deadline)


@router.callback_query(DecideProposalSG.edit_value, PickCB.filter(F.field == "plan"))
async def proposal_edit_plan_remove(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
) -> None:
    if await _stale(callback, state):
        return
    task = await _decision_task(callback, state, session, user)
    if task is None:
        return
    await _apply_edit(callback, state, session, user, bot, task, "plan", plan_value=None, plan_unit=None)


# =============================================================================================
#  Подсказки при неожиданном вводе (последними, чтобы не перехватывать основные хендлеры)
# =============================================================================================


@router.message(
    StateFilter(
        ProposeTaskSG.title,
        ProposeTaskSG.result,
        ProposeTaskSG.ai,
        ProposeTaskSG.result_manual,
        ProposeTaskSG.plan,
        ProposeTaskSG.deadline,
        DecideProposalSG.weight,
        DecideProposalSG.reject_reason,
        DecideProposalSG.edit_value,
    ),
    ~F.text,
)
async def hint_send_text(message: Message) -> None:
    """Фото, файл, стикер и т. п. на шаге, где ждём текст."""
    await message.answer(SEND_TEXT)


@router.message(
    StateFilter(
        ProposeTaskSG.confirm,
        ProposeTaskSG.edit_pick,
        DecideProposalSG.priority,
        DecideProposalSG.edit_pick,
    ),
    TextInput(),
)
async def hint_press_button(message: Message) -> None:
    """Текст на шаге, где ждём нажатия кнопки."""
    await message.answer(PRESS_BUTTON)


ALL_STATES = (*ProposeTaskSG.__all_states__, *DecideProposalSG.__all_states__)


@router.callback_query(StateFilter(*ALL_STATES), PickCB.filter(F.field != "cancel"))
async def stale_pick(callback: CallbackQuery) -> None:
    """Кнопка от предыдущего шага диалога (не подошла ни одному хендлеру выше)."""
    await callback.answer(STALE_BUTTON, show_alert=True)
