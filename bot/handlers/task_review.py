"""Проверка результатов начальником (SPEC 7.7, шаг 6 ТЗ).

Начальник видит «🤖 AI предлагает: 110 %» и решает:
* ✅ подтвердить оценку AI (`SubCB("ok")`);
* ✏️ изменить оценку (`SubCB("change")` -> оценка -> комментарий);
* ↩ вернуть на доработку (`SubCB("rework")` -> что доработать -> срок);
* 📎 посмотреть приложенные файлы (`SubCB("files")`) — кнопка остаётся и после решения.

Если начальник не ответил за AUTO_CONFIRM_HOURS, оценку AI подтверждает бот (bot.services.auto). Такую
оценку начальник может изменить в течение AUTO_REVISE_DAYS: `SubCB("revise")` -> оценка -> комментарий —
тот же диалог, что у «✏️ изменить оценку», с пометкой ``revise`` в данных диалога.

Очередь проверки — «📝 На проверке» / /review и `ListCB("review")`, открыть сдачу — `TaskCB("review")`.
Любое действие возможно только над последней сдачей задачи, которая ещё на проверке: если другой
начальник успел принять решение, показывается «Результат уже обработан».

Кнопки выбора внутри диалога (оценка, «Пропустить», срок) привязаны к сдаче: `PickCB.value` =
«<sub_id>/<значение>». Кнопка, оставшаяся в вопросе старого диалога по другой сдаче, не действует
на текущий диалог — отвечает «Кнопка уже неактуальна».
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notify
from bot.config import get_settings
from bot.db.models import Submission, Task, TaskStatus, User
from bot.filters import IsManager, TextInput
from bot.handlers.common import (
    NO_RIGHTS,
    NOT_DELIVERED,
    NOT_FOUND,
    deny,
    edit_or_answer,
    is_manager,
    remove_markup,
    send_new,
)
from bot.services import auto, tasks
from bot.services.errors import DomainError
from bot.ui import keyboards, render
from bot.ui.callbacks import ListCB, PickCB, SubCB, TaskCB
from bot.ui.texts import BTN_REVIEW
from bot.utils import dateparse
from bot.utils.dates import fmt_deadline, to_local, utcnow
from bot.utils.text import esc, fmt_num, fmt_pct, parse_percent, plural, truncate

log = logging.getLogger(__name__)

router = Router(name="task_review")

PAGE_SIZE = 8
MAX_COMMENT_LEN = 2000
MSG_LIMIT = 4000
COMMENT_HTML_LIMIT = 1500  # комментарий в итоговом сообщении (после экранирования), чтобы не вытеснить срок

ALREADY_PROCESSED = "Результат уже обработан"
CANCELLED_TASK = "Результат уже обработан: задача отменена"
STALE_TEXT = "⚠️ Результат уже обработан — возможно, его проверил другой начальник."
OWN_TASK = "Нельзя оценивать результат собственной задачи"
STALE_BUTTON = "Кнопка уже неактуальна"
EMPTY_QUEUE = "Нет результатов на проверке ✨"
DEADLINE_EXAMPLES = "«завтра», «в пятницу», «через неделю», «5 октября», «05.10 18:00»"
# Последняя строка render.submission_text до решения; после решения её убираем из текста.
PENDING_LINE = "Окончательное решение — за начальником."


class ReviewSG(StatesGroup):
    """Диалоги проверки: изменение оценки и возврат на доработку."""

    score = State()            # ✏️ итоговая оценка (кнопка или число)
    comment = State()          # ✏️ комментарий к оценке (или пропустить)
    rework_comment = State()   # ↩ что нужно доработать (обязательно)
    rework_deadline = State()  # ↩ новый срок или «оставить текущий»


# --- Проверки ---------------------------------------------------------------------------------


def _reviewable(sub: Submission | None) -> tuple[Task, Submission] | None:
    """Сдача, по которой ещё можно принять решение: последняя, без решения, задача на проверке."""
    if sub is None:
        return None
    task = sub.task
    if task is None or task.status != TaskStatus.SUBMITTED:
        return None
    last = task.last_submission
    if last is None or last.id != sub.id or sub.decision is not None:
        return None
    return task, sub


async def _load_reviewable(session: AsyncSession, sub_id: int | None) -> tuple[Task, Submission] | None:
    if not sub_id:
        return None
    return _reviewable(await tasks.get_submission(session, int(sub_id)))


async def _load_revisable(session: AsyncSession, sub_id: int | None) -> tuple[Task, Submission] | None:
    """Сдача, оценку которой подтвердил бот и которую начальник ещё может изменить (bot.services.auto)."""
    sub = await tasks.get_submission(session, int(sub_id)) if sub_id else None
    if sub is None or sub.task is None or not auto.can_revise(sub.task, sub):
        return None
    return sub.task, sub


def _revise_closed() -> str:
    """Почему автоматически подтверждённую оценку уже не изменить (как отвечает сервис)."""
    days = plural(max(get_settings().auto_revise_days, 0), "дня", "дней", "дней")
    return tasks.REVISE_CLOSED.format(days=days)


async def _refusal(session: AsyncSession, sub_id: int | None) -> str:
    """Почему решение по сдаче уже не принять: «уже обработан» (+ «задача отменена», если так)."""
    sub = await tasks.get_submission(session, int(sub_id)) if sub_id else None
    if sub is not None and sub.task is not None and sub.task.status == TaskStatus.CANCELLED:
        return CANCELLED_TASK
    return ALREADY_PROCESSED


async def _guard_sub(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User | None,
    sub_id: int,
) -> tuple[Task, Submission] | None:
    """Проверки для решений по SubCB (ok/change/rework). При отказе сам отвечает на callback и возвращает None."""
    if not is_manager(user):
        await deny(callback)
        return None
    pair = await _load_reviewable(session, sub_id)
    if pair is None:
        await callback.answer(await _refusal(session, sub_id), show_alert=True)
        await _close_stale_review(callback, session, sub_id)
        return None
    task, _sub = pair
    if user is not None and task.assignee_id == user.id:
        await callback.answer(OWN_TASK, show_alert=True)
        return None
    return pair


async def _recheck(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    data: dict[str, Any],
) -> tuple[Task, Submission] | None:
    """Повторная проверка внутри диалога (пока начальник писал, ситуация могла измениться).

    При отказе закрывает диалог (у вопроса убираются кнопки), сообщает пользователю (для callback —
    отвечает на него) и возвращает None.
    """
    if not is_manager(user):
        await _close_dialog(event, state)
        if isinstance(event, CallbackQuery):
            await deny(event)
        else:
            await event.answer(NO_RIGHTS)
        return None
    if data.get("revise"):
        pair = await _load_revisable(session, data.get("sub_id"))
        reason = _revise_closed()
    else:
        pair = await _load_reviewable(session, data.get("sub_id"))
        reason = "" if pair is not None else await _refusal(session, data.get("sub_id"))
    if pair is None:
        await _close_dialog(event, state, keep_id=_clicked_id(event))  # нажатое очистит remove_markup
        if isinstance(event, CallbackQuery):
            await event.answer(reason, show_alert=True)
            await remove_markup(event)
        else:
            await event.answer(f"⚠️ {reason}." if reason == CANCELLED_TASK or data.get("revise") else STALE_TEXT)
        return None
    return pair


async def _close_stale_review(callback: CallbackQuery, session: AsyncSession, sub_id: int) -> None:
    """Решение по сдаче уже принято (например, другим начальником): у сообщения, где нажали кнопку,
    убрать кнопки решения — остаются «📎 Файлы» и переход к очереди, чтобы по мёртвым кнопкам больше
    не нажимали. Трогаем, только если нажатая кнопка действительно есть в этом сообщении."""
    msg = callback.message
    if not isinstance(msg, Message) or msg.reply_markup is None:
        return
    pressed = callback.data
    if not any(button.callback_data == pressed for row in msg.reply_markup.inline_keyboard for button in row):
        return
    kb = await _queue_kb(session, await tasks.get_submission(session, sub_id))
    try:
        await msg.edit_reply_markup(reply_markup=kb)
    except TelegramAPIError as exc:  # сообщение слишком старое и т. п. — не важно
        log.debug("cannot close stale review message %s: %s", msg.message_id, exc)


async def _drop_dialog_for(state: FSMContext, sub_id: int, bot: Bot) -> None:
    """Если начальник был в диалоге проверки этой же сдачи — диалог больше не нужен."""
    current = await state.get_state()
    if current is not None and current in ReviewSG:
        data = await state.get_data()
        if data.get("sub_id") == sub_id:
            await state.clear()
            await _drop_kb(bot, data.get("prompt_chat_id"), data.get("prompt_id"))


# --- Вопросы диалога: кнопки живут только у последнего вопроса -------------------------------------


async def _drop_kb(bot: Bot | None, chat_id: int | None, message_id: int | None) -> None:
    if bot is None or not chat_id or not message_id:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramAPIError as exc:  # сообщение удалено / уже без кнопок — не важно
        log.debug("cannot drop keyboard of %s/%s: %s", chat_id, message_id, exc)


async def _show_prompt(
    event: Message | CallbackQuery,
    state: FSMContext,
    text: str,
    kb: InlineKeyboardMarkup | None,
    *,
    new_message: bool = False,
) -> None:
    """Задать вопрос диалога: по кнопке — правкой её сообщения (или новым при new_message), на ввод —
    новым сообщением. У предыдущего вопроса кнопки убираются, чтобы в чате не оставалось «живых»
    клавиатур уже пройденных шагов."""
    edit = isinstance(event, CallbackQuery) and not new_message
    # Сначала убрать кнопки прежнего вопроса, потом показать новый: новый вопрос остаётся последним в чате.
    await _drop_old_prompt(event, state, keep_id=_clicked_id(event) if edit else None)
    if isinstance(event, CallbackQuery):
        sent = await (edit_or_answer(event, text, kb) if edit else send_new(event, text, kb))
    else:
        sent = await event.answer(text, reply_markup=kb)
    if sent is not None:
        await state.update_data(prompt_id=sent.message_id, prompt_chat_id=sent.chat.id)


def _clicked_id(event: Message | CallbackQuery) -> int | None:
    if isinstance(event, CallbackQuery) and event.message is not None:
        return event.message.message_id
    return None


async def _drop_old_prompt(event: Message | CallbackQuery, state: FSMContext, keep_id: int | None) -> None:
    """Убрать кнопки у последнего вопроса диалога (кроме keep_id — его сейчас отредактируют)."""
    data = await state.get_data()
    prompt_id = data.get("prompt_id")
    if prompt_id and prompt_id != keep_id:
        await _drop_kb(event.bot, data.get("prompt_chat_id"), prompt_id)
        await state.update_data(prompt_id=None)


async def _start_dialog(state: FSMContext, step: State, data: dict[str, Any]) -> None:
    """Начать диалог проверки; вопрос прежнего диалога (если был) потеряет кнопки при первом вопросе."""
    previous: dict[str, Any] = {}
    current = await state.get_state()
    if current is not None and current in ReviewSG:
        old = await state.get_data()
        previous = {key: old[key] for key in ("prompt_id", "prompt_chat_id") if key in old}
    await state.clear()
    await state.set_state(step)
    await state.update_data(**previous, **data)


async def _close_dialog(event: Message | CallbackQuery, state: FSMContext, keep_id: int | None = None) -> None:
    """Закончить диалог: убрать кнопки у последнего вопроса (кроме keep_id) и сбросить состояние."""
    await _drop_old_prompt(event, state, keep_id)
    await state.clear()


def _dialog_data(callback: CallbackQuery, task: Task, sub: Submission) -> dict[str, Any]:
    """Данные диалога: id сдачи/задачи и сообщение с результатом (чтобы закрыть его кнопки в конце)."""
    data: dict[str, Any] = {"sub_id": sub.id, "task_id": task.id, "ai_score": sub.ai_score}
    if isinstance(callback.message, Message):
        data["review_chat_id"] = callback.message.chat.id
        data["review_msg_id"] = callback.message.message_id
    return data


# --- Кнопки диалога привязаны к сдаче ------------------------------------------------------------

_BIND_SEP = "/"


def _as_pick(button: InlineKeyboardButton) -> PickCB | None:
    if not button.callback_data:
        return None
    try:
        return PickCB.unpack(button.callback_data)
    except (TypeError, ValueError):
        return None


def _bind(markup: InlineKeyboardMarkup, sub_id: int | None) -> InlineKeyboardMarkup:
    """PickCB-кнопки клавиатуры (кроме глобальной «Отмена») -> value «<sub_id>/<значение>»."""
    rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row: list[InlineKeyboardButton] = []
        for button in row:
            pick = _as_pick(button)
            if pick is not None and pick.field != "cancel":
                bound = PickCB(field=pick.field, value=f"{sub_id}{_BIND_SEP}{pick.value}")
                button = InlineKeyboardButton(text=button.text, callback_data=bound.pack())
            new_row.append(button)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _dialog_pick(callback: CallbackQuery, callback_data: PickCB, state: FSMContext) -> str | None:
    """Значение кнопки текущего диалога. Кнопка из диалога по другой сдаче -> «неактуальна» и None."""
    sub_id, sep, value = callback_data.value.partition(_BIND_SEP)
    if not sep or sub_id != str(await state.get_value("sub_id")):
        await callback.answer(STALE_BUTTON)
        await remove_markup(callback)
        return None
    return value


def _score_kb(sub_id: int | None, ai_score: float | None) -> InlineKeyboardMarkup:
    return _bind(keyboards.score_kb(ai_score), sub_id)


def _skip_kb(sub_id: int | None) -> InlineKeyboardMarkup:
    return _bind(keyboards.skip_cancel_kb(), sub_id)


# --- Тексты и клавиатуры ------------------------------------------------------------------------


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fmt_short_dt(value: datetime) -> str:
    return to_local(value).strftime("%d.%m %H:%M")


def _task_ref(task: Task) -> str:
    """«Задача #12 «Анализ договоров» — Иванов И. И.» (HTML)."""
    assignee = task.assignee.short_name if task.assignee is not None else "—"
    return f"Задача #{task.id} «{esc(task.title)}» — {esc(assignee)}"


def _queue_line(n: int, task: Task) -> str:
    sub = task.last_submission
    assignee = task.assignee.short_name if task.assignee is not None else "—"
    head = f"{n}. <b>#{task.id}</b> {esc(_short(task.title, 70))} — {esc(assignee)}"
    details: list[str] = []
    if sub is not None:
        details.append(f"сдано {_fmt_short_dt(sub.created_at)}")
        if sub.is_late:
            details.append(f"⏰ опоздание {fmt_num(sub.late_days)} дн.")
        details.append(f"🤖 {fmt_pct(sub.ai_score)}" if sub.ai_score is not None else "🤖 нет оценки")
        if sub.attempt and sub.attempt > 1:
            details.append(f"попытка {sub.attempt}")
    if not details:
        return head
    return head + "\n      " + " · ".join(details)


async def _review_list(session: AsyncSession, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    items = await tasks.list_for_review(session)
    total = len(items)
    if total == 0:
        return EMPTY_QUEUE, None
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    header = f"📝 <b>На проверке</b> — {plural(total, 'результат', 'результата', 'результатов')}"
    if pages > 1:
        header += f" · стр. {page + 1}/{pages}"
    lines = [header, "Выберите задачу: покажу план и факт, оценку AI и кнопки решения.", ""]
    lines += [_queue_line(n, task) for n, task in enumerate(chunk, start=page * PAGE_SIZE + 1)]

    kb = InlineKeyboardBuilder()
    for task in chunk:
        assignee = task.assignee.short_name if task.assignee is not None else "—"
        kb.row(
            InlineKeyboardButton(
                text=_short(f"🔍 #{task.id} · {assignee} · {task.title}", 60),
                callback_data=TaskCB(action="review", task_id=task.id).pack(),
            )
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀ Назад", callback_data=ListCB(scope="review", status="review", page=page - 1).pack()
            )
        )
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                text="Вперёд ▶", callback_data=ListCB(scope="review", status="review", page=page + 1).pack()
            )
        )
    if nav:
        kb.row(*nav)
    return truncate("\n".join(lines), MSG_LIMIT), kb.as_markup()


def _queue_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="📝 Все на проверке", callback_data=ListCB(scope="review", status="review", page=0).pack()
    )


def _review_markup(sub: Submission) -> InlineKeyboardMarkup:
    """review_kb + переход к очереди проверки."""
    base = keyboards.review_kb(sub)
    rows = [list(row) for row in base.inline_keyboard]
    rows.append([_queue_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _queue_kb(session: AsyncSession, sub: Submission | None = None) -> InlineKeyboardMarkup | None:
    """После решения: «📎 Файлы (N)» этой сдачи (если были) и «к следующим результатам» (если очередь не пуста)."""
    rows = _files_rows(sub)
    left = len(await tasks.list_for_review(session))
    if left:
        button = InlineKeyboardButton(
            text=f"📝 Ещё на проверке: {left}",
            callback_data=ListCB(scope="review", status="review", page=0).pack(),
        )
        rows.append([button])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _files_rows(sub: Submission | None) -> list[list[InlineKeyboardButton]]:
    """Кнопка файлов сдачи — остаётся и после решения: подтверждения могут понадобиться позже."""
    if sub is None or not sub.attachments:
        return []
    button = InlineKeyboardButton(
        text=f"📎 Файлы ({len(sub.attachments)})", callback_data=SubCB(action="files", sub_id=sub.id).pack()
    )
    return [[button]]


def _original_text(task: Task, sub: Submission) -> str:
    """Текст сдачи, каким его видел начальник до решения (вызывать ДО сервиса проверки).

    Строку «Окончательное решение — за начальником.» убираем: решение уже принято.
    """
    return render.submission_text(task, sub).replace("\n" + PENDING_LINE, "")


def _closed_text(original: str, suffix: str, extra: str | None = None) -> str:
    """Исходный текст сдачи + отметка о решении (для сообщения, у которого убираются кнопки).

    extra — строка под решением (например, что уведомление сотруднику не доставлено).
    """
    tail = f"\n\n<b>{suffix}</b>" + (f"\n{extra}" if extra else "")
    return truncate(original, MSG_LIMIT - len(tail)) + tail


async def _close_review_message(
    bot: Bot, data: dict[str, Any], original: str, suffix: str, sub: Submission
) -> None:
    """Закрыть исходное сообщение с результатом: дописать решение, оставить только «📎 Файлы»."""
    chat_id, msg_id = data.get("review_chat_id"), data.get("review_msg_id")
    if not chat_id or not msg_id:
        return
    rows = _files_rows(sub)
    try:
        await bot.edit_message_text(
            text=_closed_text(original, suffix),
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
        )
    except TelegramAPIError as exc:  # сообщение удалено, слишком старое, не текстовое и т. п.
        log.debug("cannot close review message %s/%s: %s", chat_id, msg_id, exc)


def _cancel_row_index(rows: list[list[InlineKeyboardButton]]) -> int | None:
    for index, row in enumerate(rows):
        for button in row:
            pick = _as_pick(button)
            if pick is not None and pick.field == "cancel":
                return index
    return None


def _deadline_markup(keep: bool, sub_id: int) -> InlineKeyboardMarkup:
    """deadline_kb() + «Оставить текущий срок» (перед рядом «Отмена»), если текущий срок ещё не прошёл."""
    base = keyboards.deadline_kb()
    rows = [list(row) for row in base.inline_keyboard]
    if keep:
        keep_row = [
            InlineKeyboardButton(
                text="📌 Оставить текущий срок",
                callback_data=PickCB(field="deadline", value="keep").pack(),
            )
        ]
        index = _cancel_row_index(rows)
        rows.insert(index if index is not None else len(rows), keep_row)
    return _bind(InlineKeyboardMarkup(inline_keyboard=rows), sub_id)


def _deadline_prompt(task: Task, sub_id: int, error: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    keep = task.deadline > utcnow()
    lines: list[str] = []
    if error:
        lines += [error, ""]
    lines.append("📅 <b>Срок доработки?</b>")
    if keep:
        lines.append(f"Текущий срок: {fmt_deadline(task.deadline)}.")
        lines.append(
            f"Выберите дату кнопкой, напишите свою (например: {DEADLINE_EXAMPLES}) или оставьте текущий срок."
        )
    else:
        lines.append(f"⚠️ Текущий срок ({fmt_deadline(task.deadline)}) уже прошёл — укажите новый.")
        lines.append(f"Выберите дату кнопкой или напишите свою (например: {DEADLINE_EXAMPLES}).")
    return "\n".join(lines), _deadline_markup(keep, sub_id)


def _score_prompt(task: Task, ai_score: float | None) -> str:
    max_score = get_settings().max_score
    ai_line = (
        f"🤖 AI предлагает: <b>{fmt_pct(ai_score)}</b>"
        if ai_score is not None
        else "🤖 Предварительной оценки AI нет."
    )
    return (
        "✏️ <b>Изменение оценки</b>\n"
        f"{_task_ref(task)}\n"
        f"{ai_line}\n\n"
        f"Выберите итоговую оценку кнопкой или напишите число от 0 до {max_score} (например, <b>100</b>)."
    )


def _comment_html(comment: str) -> str:
    """Комментарий для итогового сообщения: сначала экранировать, потом обрезать (HTML-безопасно)."""
    return truncate(esc(comment), COMMENT_HTML_LIMIT)


def _change_summary(score: float, ai_score: float | None) -> str:
    if ai_score is None:
        return f"✏️ Оценка выставлена: {fmt_pct(score)}"
    return f"✏️ Оценка изменена: {fmt_pct(score)} (AI предлагал {fmt_pct(ai_score)})"


def _parse_score(value: str | None) -> tuple[float | None, str | None]:
    """Оценка из кнопки или текста -> (оценка, None) или (None, текст ошибки)."""
    max_score = get_settings().max_score
    number = parse_percent(value) if value else None
    if number is None or not math.isfinite(number):
        return None, f"Не понял оценку. Напишите число от 0 до {max_score}, например: <b>95</b> или <b>110 %</b>."
    if not 0 <= number <= max_score:
        return None, f"Оценка должна быть от 0 до {max_score} %. Напишите другое число."
    # Оценки везде показываются целыми («96 %») — храним так же, чтобы KPI сходился с тем, что видно.
    return float(math.floor(number + 0.5)), None


# --- Очередь проверки ---------------------------------------------------------------------------


@router.message(F.text == BTN_REVIEW, IsManager())
@router.message(Command("review"), IsManager())
async def review_queue(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    text, kb = await _review_list(session, page=0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(ListCB.filter(F.scope == "review"))
async def review_queue_page(
    callback: CallbackQuery, callback_data: ListCB, session: AsyncSession, user: User | None
) -> None:
    if not is_manager(user):
        await deny(callback)
        return
    text, kb = await _review_list(session, page=callback_data.page)
    await callback.answer()
    await edit_or_answer(callback, text, kb)


@router.callback_query(TaskCB.filter(F.action == "review"))
async def open_review(
    callback: CallbackQuery, callback_data: TaskCB, session: AsyncSession, user: User | None
) -> None:
    if not is_manager(user):
        await deny(callback)
        return
    task = await tasks.get_task(session, callback_data.task_id)
    if task is None:
        await callback.answer(NOT_FOUND, show_alert=True)
        return
    sub = task.last_submission
    if task.status != TaskStatus.SUBMITTED or sub is None or sub.decision is not None:
        reason = CANCELLED_TASK if task.status == TaskStatus.CANCELLED else ALREADY_PROCESSED
        await callback.answer(reason, show_alert=True)
        return
    await callback.answer()
    await edit_or_answer(callback, render.submission_text(task, sub), _review_markup(sub))


# --- ✅ Подтвердить оценку AI ----------------------------------------------------------------------


@router.callback_query(SubCB.filter(F.action == "ok"))
async def confirm_score(
    callback: CallbackQuery,
    callback_data: SubCB,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
    state: FSMContext,
) -> None:
    pair = await _guard_sub(callback, session, user, callback_data.sub_id)
    if pair is None:
        return
    task, sub = pair
    if sub.ai_score is None:
        await callback.answer(
            "У этого результата нет оценки AI. Нажмите «✏️ Изменить оценку» и поставьте оценку вручную.",
            show_alert=True,
        )
        return
    assert user is not None
    original = _original_text(task, sub)
    task = await tasks.review_confirm(session, sub.id, user)
    await session.commit()
    await _drop_dialog_for(state, sub.id, bot)
    await callback.answer("✅ Оценка подтверждена")
    delivered = await notify.notify_review_result(bot, task, sub)
    suffix = f"✅ Подтверждено: {fmt_pct(task.final_score)}"
    text = _closed_text(original, suffix, None if delivered else NOT_DELIVERED)
    await edit_or_answer(callback, text, await _queue_kb(session, sub))


# --- ✏️ Изменить оценку -----------------------------------------------------------------------------


@router.callback_query(SubCB.filter(F.action == "change"))
async def change_start(
    callback: CallbackQuery,
    callback_data: SubCB,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
) -> None:
    pair = await _guard_sub(callback, session, user, callback_data.sub_id)
    if pair is None:
        return
    task, sub = pair
    await _start_dialog(state, ReviewSG.score, _dialog_data(callback, task, sub))
    await callback.answer()
    await _show_prompt(
        callback, state, _score_prompt(task, sub.ai_score), _score_kb(sub.id, sub.ai_score), new_message=True
    )


@router.callback_query(SubCB.filter(F.action == "revise"))
async def revise_start(
    callback: CallbackQuery,
    callback_data: SubCB,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
) -> None:
    """«✏️ Изменить оценку» под сообщением об автоподтверждении или в карточке выполненной задачи."""
    if not is_manager(user):
        await deny(callback)
        return
    pair = await _load_revisable(session, callback_data.sub_id)
    if pair is None:
        await callback.answer(_revise_closed(), show_alert=True)
        return
    task, sub = pair
    if user is not None and task.assignee_id == user.id:
        await callback.answer(OWN_TASK, show_alert=True)
        return
    await _start_dialog(state, ReviewSG.score, {"sub_id": sub.id, "task_id": task.id, "ai_score": sub.ai_score, "revise": True})
    await callback.answer()
    until = auto.revise_until(sub)
    prompt = (
        "✏️ <b>Изменение автоматически подтверждённой оценки</b>\n"
        f"{_task_ref(task)}\n"
        f"🏁 Сейчас: <b>{fmt_pct(sub.final_score)}</b> (подтверждена автоматически)\n"
        + (f"Изменить можно до {render.auto_when(until)}.\n" if until is not None else "")
        + f"\nВыберите новую оценку кнопкой или напишите число от 0 до {get_settings().max_score}."
    )
    await _show_prompt(callback, state, prompt, _score_kb(sub.id, sub.ai_score), new_message=True)


async def _ask_comment(event: Message | CallbackQuery, state: FSMContext, sub_id: int, score: float) -> None:
    await state.update_data(score=score)
    await state.set_state(ReviewSG.comment)
    text = (
        f"Итоговая оценка: <b>{fmt_pct(score)}</b>\n\n"
        "💬 <b>Комментарий к оценке?</b>\n"
        "Например, почему оценка отличается от предложенной. Сотрудник увидит его вместе с оценкой.\n"
        "Напишите текст или нажмите «⏭ Пропустить»."
    )
    await _show_prompt(event, state, text, _skip_kb(sub_id))


@router.callback_query(ReviewSG.score, PickCB.filter(F.field == "score"))
async def change_score_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
) -> None:
    value = await _dialog_pick(callback, callback_data, state)
    if value is None:
        return
    score, _error = _parse_score(value)
    if score is None:
        await callback.answer("Не удалось распознать оценку — напишите её числом.", show_alert=True)
        return
    pair = await _recheck(callback, state, session, user, await state.get_data())
    if pair is None:
        return
    await callback.answer()
    await _ask_comment(callback, state, pair[1].id, score)


@router.message(ReviewSG.score, TextInput())
async def change_score_text(
    message: Message, state: FSMContext, session: AsyncSession, user: User | None
) -> None:
    data = await state.get_data()
    score, error = _parse_score(message.text)
    if score is None:
        kb = _score_kb(data.get("sub_id"), data.get("ai_score"))
        await _show_prompt(message, state, error or "Не понял оценку.", kb)
        return
    pair = await _recheck(message, state, session, user, data)
    if pair is None:
        return
    await _ask_comment(message, state, pair[1].id, score)


@router.callback_query(ReviewSG.comment, PickCB.filter(F.field == "skip"))
async def change_comment_skip(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
) -> None:
    if await _dialog_pick(callback, callback_data, state) is None:
        return
    await _finish_change(callback, state, session, user, bot, comment=None)


@router.message(ReviewSG.comment, TextInput())
async def change_comment_text(
    message: Message, state: FSMContext, session: AsyncSession, user: User | None, bot: Bot
) -> None:
    comment = (message.text or "").strip()
    if len(comment) > MAX_COMMENT_LEN:
        await _show_prompt(
            message,
            state,
            f"Комментарий слишком длинный ({len(comment)} симв.). Сократите до {MAX_COMMENT_LEN} символов.",
            _skip_kb(await state.get_value("sub_id")),
        )
        return
    await _finish_change(message, state, session, user, bot, comment=comment or None)


async def _finish_change(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
    comment: str | None,
) -> None:
    data = await state.get_data()
    pair = await _recheck(event, state, session, user, data)
    if pair is None:
        return
    task, sub = pair
    score = data.get("score")
    if score is None:  # не должно случиться: вернуть на шаг оценки
        await state.set_state(ReviewSG.score)
        if isinstance(event, CallbackQuery):
            await event.answer()
        await _show_prompt(event, state, _score_prompt(task, sub.ai_score), _score_kb(sub.id, sub.ai_score))
        return
    assert user is not None
    if data.get("revise"):
        await _finish_revise(event, state, session, user, bot, task, sub, float(score), comment)
        return
    original = _original_text(task, sub)
    try:
        task = await tasks.review_set_score(session, sub.id, user, float(score), comment)
    except DomainError:  # например, другой начальник успел раньше: диалог закончен, текст покажет main.py
        await _close_dialog(event, state)
        raise
    await session.commit()
    await _close_dialog(event, state, keep_id=_clicked_id(event))  # нажатый вопрос станет итогом
    if isinstance(event, CallbackQuery):
        await event.answer("✏️ Оценка сохранена")

    delivered = await notify.notify_review_result(bot, task, sub)
    summary = _change_summary(float(score), sub.ai_score)
    lines = [f"<b>{summary}</b>", f"📌 {_task_ref(task)}"]
    if comment:
        lines.append(f"💬 Комментарий: {_comment_html(comment)}")
    lines += ["", "Сотруднику отправлено уведомление с итоговой оценкой." if delivered else NOT_DELIVERED]
    await edit_or_answer(event, truncate("\n".join(lines), MSG_LIMIT), await _queue_kb(session))
    await _close_review_message(bot, data, original, summary, sub)


async def _finish_revise(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    bot: Bot,
    task: Task,
    sub: Submission,
    score: float,
    comment: str | None,
) -> None:
    """Сохранить новую оценку вместо подтверждённой автоматически и сообщить сотруднику."""
    previous = sub.final_score
    try:
        task = await tasks.review_revise_auto(session, sub.id, user, score, comment)
    except DomainError:  # срок вышел или оценку уже изменил другой начальник: текст покажет main.py
        await _close_dialog(event, state)
        raise
    await session.commit()
    await _close_dialog(event, state, keep_id=_clicked_id(event))
    if isinstance(event, CallbackQuery):
        await event.answer("✏️ Оценка изменена")
    delivered = await notify.notify_review_result(bot, task, sub)
    lines = [
        f"<b>✏️ Оценка изменена: {fmt_pct(previous)} → {fmt_pct(task.final_score)}</b>",
        f"📌 {_task_ref(task)}",
    ]
    if comment:
        lines.append(f"💬 Комментарий: {_comment_html(comment)}")
    lines += ["", "Сотруднику отправлено уведомление с новой оценкой." if delivered else NOT_DELIVERED]
    await edit_or_answer(event, truncate("\n".join(lines), MSG_LIMIT), await _queue_kb(session))


# --- ↩ Вернуть на доработку ------------------------------------------------------------------------


@router.callback_query(SubCB.filter(F.action == "rework"))
async def rework_start(
    callback: CallbackQuery,
    callback_data: SubCB,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
) -> None:
    pair = await _guard_sub(callback, session, user, callback_data.sub_id)
    if pair is None:
        return
    task, sub = pair
    await _start_dialog(state, ReviewSG.rework_comment, _dialog_data(callback, task, sub))
    await callback.answer()
    text = (
        "↩ <b>Возврат на доработку</b>\n"
        f"{_task_ref(task)}\n\n"
        "<b>Что нужно доработать?</b>\n"
        "Напишите, что именно исправить или дополнить — сотрудник увидит этот комментарий."
    )
    await _show_prompt(callback, state, text, keyboards.cancel_kb(), new_message=True)


@router.message(ReviewSG.rework_comment, TextInput())
async def rework_comment_text(
    message: Message, state: FSMContext, session: AsyncSession, user: User | None
) -> None:
    comment = (message.text or "").strip()
    if len(comment) > MAX_COMMENT_LEN:
        await _show_prompt(
            message,
            state,
            f"Комментарий слишком длинный ({len(comment)} симв.). Сократите до {MAX_COMMENT_LEN} символов.",
            keyboards.cancel_kb(),
        )
        return
    data = await state.get_data()
    pair = await _recheck(message, state, session, user, data)
    if pair is None:
        return
    task, sub = pair
    await state.update_data(comment=comment)
    await state.set_state(ReviewSG.rework_deadline)
    text, kb = _deadline_prompt(task, sub.id, error="✅ Комментарий записан.")
    await _show_prompt(message, state, text, kb)


@router.callback_query(ReviewSG.rework_deadline, PickCB.filter(F.field == "deadline"))
async def rework_deadline_pick(
    callback: CallbackQuery,
    callback_data: PickCB,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
) -> None:
    value = await _dialog_pick(callback, callback_data, state)
    if value is None:
        return
    if value == "keep":
        await _finish_rework(callback, state, session, user, bot, new_deadline=None)
        return
    try:
        deadline: datetime | None = dateparse.iso_to_deadline(value)
    except (TypeError, ValueError, OverflowError):
        deadline = None
    await _finish_rework(callback, state, session, user, bot, new_deadline=deadline, invalid=deadline is None)


@router.message(ReviewSG.rework_deadline, TextInput())
async def rework_deadline_text(
    message: Message, state: FSMContext, session: AsyncSession, user: User | None, bot: Bot
) -> None:
    text = message.text or ""
    deadline = dateparse.parse_deadline(text)
    await _finish_rework(
        message, state, session, user, bot,
        new_deadline=deadline,
        invalid=deadline is None,
        far_year=deadline is None and dateparse.names_far_year(text),
    )


async def _finish_rework(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
    *,
    new_deadline: datetime | None,
    invalid: bool = False,
    far_year: bool = False,
) -> None:
    """new_deadline=None и invalid=False — «оставить текущий срок». far_year — срок не принят из-за года."""
    data = await state.get_data()
    pair = await _recheck(event, state, session, user, data)
    if pair is None:
        return
    task, sub = pair

    comment = data.get("comment")
    if not comment:  # не должно случиться: вернуть на шаг комментария
        await state.set_state(ReviewSG.rework_comment)
        if isinstance(event, CallbackQuery):
            await event.answer()
        await _show_prompt(event, state, "<b>Что нужно доработать?</b> Напишите комментарий.", keyboards.cancel_kb())
        return

    now = utcnow()
    error: str | None = None
    if invalid:
        error = f"⚠️ {dateparse.FAR_YEAR_HINT}" if far_year else "⚠️ Не понял срок или он уже в прошлом."
    elif new_deadline is not None and new_deadline <= now:
        error = "⚠️ Этот срок уже прошёл — выберите другой."
    elif new_deadline is None and task.deadline <= now:
        error = "⚠️ Текущий срок уже прошёл — укажите новый."
    if error is not None:
        # Если «оставить» больше нельзя, подсказка об этом уже есть в тексте вопроса — не дублируем.
        keep_expired = new_deadline is None and not invalid
        text, kb = _deadline_prompt(task, sub.id, error=None if keep_expired else error)
        if isinstance(event, CallbackQuery):
            await event.answer(error.removeprefix("⚠️ "), show_alert=True)
        await _show_prompt(event, state, text, kb)
        return

    assert user is not None
    original = _original_text(task, sub)
    old_deadline = task.deadline
    try:
        task = await tasks.review_rework(session, sub.id, user, comment, new_deadline)
    except DomainError:  # например, другой начальник успел раньше: диалог закончен, текст покажет main.py
        await _close_dialog(event, state)
        raise
    await session.commit()
    await _close_dialog(event, state, keep_id=_clicked_id(event))  # нажатый вопрос станет итогом
    if isinstance(event, CallbackQuery):
        await event.answer("↩ Возвращено на доработку")

    delivered = await notify.notify_rework(bot, task, sub)
    deadline_note = "новый" if task.deadline != old_deadline else "без изменений"
    lines = [
        "<b>↩ Возвращено на доработку</b>",
        f"📌 {_task_ref(task)}",
        f"💬 Что доработать: {_comment_html(comment)}",
        f"📅 Срок: {fmt_deadline(task.deadline)} ({deadline_note})",
        "",
        "Сотруднику отправлено уведомление — после доработки он сдаст результат снова."
        if delivered
        else NOT_DELIVERED,
    ]
    await edit_or_answer(event, truncate("\n".join(lines), MSG_LIMIT), await _queue_kb(session))
    await _close_review_message(bot, data, original, "↩ Возвращено на доработку", sub)


# --- 📎 Файлы ------------------------------------------------------------------------------------------


@router.callback_query(SubCB.filter(F.action == "files"))
async def send_files(
    callback: CallbackQuery,
    callback_data: SubCB,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
) -> None:
    # Файлы-подтверждения нужны и после решения («что фактически получено?»), поэтому здесь
    # не требуется, чтобы сдача ещё ждала проверки — только права начальника.
    if not is_manager(user):
        await deny(callback)
        return
    sub = await tasks.get_submission(session, callback_data.sub_id)
    if sub is None:
        await callback.answer("Результат не найден.", show_alert=True)
        return
    if not sub.attachments:
        await callback.answer("К этому результату файлы не приложены.", show_alert=True)
        return
    # Сначала ответить: отправка нескольких файлов (и ожидание флуд-лимита) может занять больше,
    # чем Telegram ждёт ответа на нажатие.
    await callback.answer("📎 Отправляю файлы…")
    chat_id = callback.message.chat.id if callback.message is not None else callback.from_user.id
    await notify.send_attachments(bot, chat_id, sub)


# --- Не текст в текстовом шаге -------------------------------------------------------------------------

_NON_TEXT_HINTS: dict[str, str] = {
    ReviewSG.score.state: "Напишите оценку числом (например, <b>100</b>) или выберите кнопкой выше.",
    ReviewSG.comment.state: "Напишите комментарий текстом или нажмите «⏭ Пропустить».",
    ReviewSG.rework_comment.state: "Опишите текстом, что нужно доработать.",
    ReviewSG.rework_deadline.state: (
        f"Напишите срок текстом (например: {DEADLINE_EXAMPLES}) или выберите кнопкой выше."
    ),
}


@router.message(StateFilter(ReviewSG), ~F.text)
async def non_text_hint(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    hint = _NON_TEXT_HINTS.get(current or "", "Ответьте текстом или нажмите «✖️ Отмена».")
    # Без своих кнопок: кнопки текущего вопроса (и «✖️ Отмена») — в сообщении выше.
    await message.answer(hint)
