"""Отчёты по эффективности (SPEC 7.8, ТЗ «Что получает руководитель — на одном экране»).

* «📊 Команда» /team (руководитель) — дашборд команды за период; ``PeriodCB("team")`` переключает
  период (неделя/месяц/квартал/год) и листает назад, редактируя то же сообщение.
* ``UserCB("card")`` / ``PeriodCB("emp")`` — карточка сотрудника: неделя + месяц + выбранный период.
* ``UserCB("history")`` — история оценок сотрудника с пагинацией (``UserCB.page``).
* «📈 Моя эффективность» /kpi (сотрудник) / ``PeriodCB("me")`` — своя карточка. Сотрудник видит только себя.
* «📤 Экспорт» /export (руководитель) → ``export_kb`` → ``PeriodCB("export")`` → Excel-файл.

Права в callback-хендлерах проверяются внутри (а не фильтром на декораторе), чтобы у
посторонних не «висели часики»: нет прав → ``common.deny``.
"""

from __future__ import annotations

import logging
import math
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import TaskStatus, User
from bot.filters import IsEmployee, IsManager
from bot.handlers import common
from bot.services import kpi, periods, tasks, users
from bot.services import export as export_service
from bot.services.errors import DomainError
from bot.ui import keyboards, render
from bot.ui.callbacks import ListCB, PeriodCB, UserCB
from bot.ui.texts import BTN_EXPORT, BTN_MY_KPI, BTN_TEAM
from bot.utils.dates import to_local, utcnow
from bot.utils.text import esc

log = logging.getLogger(__name__)

router = Router(name="dashboard")

HISTORY_PAGE_SIZE = 10
# Насколько далеко назад можно листать период (защита от поддельных offset: год < 1 и т.п.).
MAX_BACK_OFFSET = 500

USER_NOT_FOUND = "Сотрудник не найден."
EXPORT_PROMPT = (
    "📤 <b>Экспорт в Excel</b>\n\n"
    "Выберите период — пришлю файл с листами «Сводка» (KPI по сотрудникам), "
    "«Задачи» (план, факт, оценки) и «Журнал» (все изменения)."
)
EXPORT_FAILED = "⚠️ Не удалось подготовить отчёт. Попробуйте ещё раз чуть позже."
# Руководитель (например, недавно повышенный) набрал /kpi или нажал старую кнопку сотрудника.
MANAGER_KPI_HINT = (
    f"📈 «{BTN_MY_KPI}» — раздел сотрудника. Эффективность команды и каждого сотрудника — "
    f"«{BTN_TEAM}», отчёт в Excel — «{BTN_EXPORT}»."
)
EXPORT_SEND_FAILED = "⚠️ Не удалось отправить файл. Попробуйте ещё раз."


# --- Вспомогательное -----------------------------------------------------------------------


def _normalize_period(kind: str, offset: int) -> tuple[str, int]:
    """Защита от поддельных callback: неизвестный вид → неделя, будущее → текущий период."""
    if kind not in periods.PERIOD_KINDS:
        kind = "week"
    return kind, max(-MAX_BACK_OFFSET, min(int(offset), 0))


def _is_active(user: User | None) -> bool:
    return user is not None and user.is_active


def _can_view(viewer: User | None, target_id: int) -> bool:
    """Руководитель видит всех, сотрудник — только себя."""
    if not _is_active(viewer):
        return False
    assert viewer is not None
    return viewer.is_manager or viewer.id == target_id


def _chat_id(callback: CallbackQuery) -> int:
    # У InaccessibleMessage тоже есть chat, поэтому достаточно проверить на None.
    if callback.message is not None:
        return callback.message.chat.id
    return callback.from_user.id


def _my_card_kb(user: User, kind: str, offset: int) -> InlineKeyboardMarkup:
    """Клавиатура «Моей эффективности»: периоды scope="me" + история оценок + мои задачи.

    keyboards.employee_card_kb строит кнопки периодов только для scope="emp" (карточка
    для руководителя), поэтому для своей карточки клавиатура собирается здесь.
    """
    base = keyboards.period_kb("me", kind, offset)
    rows = [list(row) for row in base.inline_keyboard]
    rows.append(
        [
            InlineKeyboardButton(
                text="📜 История оценок",
                callback_data=UserCB(action="history", user_id=user.id).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="📋 Мои задачи",
                callback_data=ListCB(scope="my", status="open").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _team_screen(session: AsyncSession, kind: str, offset: int) -> tuple[str, InlineKeyboardMarkup]:
    now = utcnow()
    period = periods.get_period(kind, offset, now)
    rows = await kpi.kpi_for_team(session, period, now)
    text = render.team_dashboard(period, rows, kpi.team_kpi(rows))
    return text, keyboards.team_kb(rows, kind, offset)


async def _card_screen(
    session: AsyncSession, target: User, kind: str, offset: int, *, mine: bool
) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка эффективности: неделя + месяц + выбранный период.

    mine=True — своя карточка (кнопки периодов scope="me"), иначе — карточка для руководителя
    (employee_card_kb со scope="emp" и кнопкой «◀ К команде»).
    """
    now = utcnow()
    week_period = periods.get_period("week", 0, now)
    month_period = periods.get_period("month", 0, now)
    week = await kpi.kpi_for_user(session, target.id, week_period, now)
    month = await kpi.kpi_for_user(session, target.id, month_period, now)
    if (kind, offset) == ("week", 0):
        period, current = week_period, week
    elif (kind, offset) == ("month", 0):
        period, current = month_period, month
    else:
        period = periods.get_period(kind, offset, now)
        current = await kpi.kpi_for_user(session, target.id, period, now)
    text = render.employee_card(target, week, month, period, current)
    if mine:
        markup = _my_card_kb(target, kind, offset)
    else:
        markup = keyboards.employee_card_kb(target, kind, offset, back_to_team=True)
    return text, markup


async def _show_card(
    callback: CallbackQuery, session: AsyncSession, viewer: User | None, target_id: int, kind: str, offset: int
) -> None:
    """Общая часть UserCB("card") и PeriodCB("emp")."""
    if not _can_view(viewer, target_id):
        await common.deny(callback)
        return
    assert viewer is not None
    target = await users.get_user(session, target_id)
    if target is None:
        await callback.answer(USER_NOT_FOUND, show_alert=True)
        return
    # Сотрудник, открывший свою карточку (например, «◀ К карточке» из истории), получает свою клавиатуру.
    mine = not viewer.is_manager
    text, markup = await _card_screen(session, target, kind, offset, mine=mine)
    await common.edit_or_answer(callback, text, markup)
    await callback.answer()


async def _finish_progress(progress: Message, text: str) -> None:
    with suppress(TelegramAPIError):
        await progress.edit_text(text)


# --- 📊 Команда ----------------------------------------------------------------------------


@router.message(F.text == BTN_TEAM, IsManager())
@router.message(Command("team"), IsManager())
async def team_menu(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    text, markup = await _team_screen(session, "week", 0)
    await common.edit_or_answer(message, text, markup)


@router.callback_query(PeriodCB.filter(F.scope == "team"))
async def team_period(
    callback: CallbackQuery, callback_data: PeriodCB, session: AsyncSession, user: User | None
) -> None:
    if not common.is_manager(user):
        await common.deny(callback)
        return
    kind, offset = _normalize_period(callback_data.kind, callback_data.offset)
    text, markup = await _team_screen(session, kind, offset)
    await common.edit_or_answer(callback, text, markup)
    await callback.answer()


# --- 👤 Карточка сотрудника -----------------------------------------------------------------


@router.callback_query(UserCB.filter(F.action == "card"))
async def employee_card(
    callback: CallbackQuery, callback_data: UserCB, session: AsyncSession, user: User | None
) -> None:
    await _show_card(callback, session, user, callback_data.user_id, "week", 0)


@router.callback_query(PeriodCB.filter(F.scope == "emp"))
async def employee_period(
    callback: CallbackQuery, callback_data: PeriodCB, session: AsyncSession, user: User | None
) -> None:
    kind, offset = _normalize_period(callback_data.kind, callback_data.offset)
    await _show_card(callback, session, user, callback_data.user_id, kind, offset)


# --- 📜 История оценок ----------------------------------------------------------------------


@router.callback_query(UserCB.filter(F.action == "history"))
async def employee_history(
    callback: CallbackQuery, callback_data: UserCB, session: AsyncSession, user: User | None
) -> None:
    if not _can_view(user, callback_data.user_id):
        await common.deny(callback)
        return
    target = await users.get_user(session, callback_data.user_id)
    if target is None:
        await callback.answer(USER_NOT_FOUND, show_alert=True)
        return
    total = await tasks.count_tasks(session, assignee_id=target.id, statuses=[TaskStatus.DONE])
    pages = max(1, math.ceil(total / HISTORY_PAGE_SIZE))
    page = max(0, min(callback_data.page, pages - 1))
    items = await tasks.evaluated_history(
        session, target.id, limit=HISTORY_PAGE_SIZE, offset=page * HISTORY_PAGE_SIZE
    )
    text = render.history_text(target, items, page, total)
    await common.edit_or_answer(callback, text, keyboards.history_kb(target.id, page, total))
    await callback.answer()


# --- 📈 Моя эффективность -------------------------------------------------------------------


@router.message(F.text == BTN_MY_KPI, IsEmployee())
@router.message(Command("kpi"), IsEmployee())
async def my_kpi_menu(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    text, markup = await _card_screen(session, user, "week", 0, mine=True)
    await common.edit_or_answer(message, text, markup)


@router.message(F.text == BTN_MY_KPI, IsManager())
@router.message(Command("kpi"), IsManager())
async def manager_kpi_hint(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    await message.answer(MANAGER_KPI_HINT, reply_markup=keyboards.main_menu(user))


@router.callback_query(PeriodCB.filter(F.scope == "me"))
async def my_kpi_period(
    callback: CallbackQuery, callback_data: PeriodCB, session: AsyncSession, user: User | None
) -> None:
    # Только свои данные: user_id из callback игнорируется.
    if not _is_active(user):
        await common.deny(callback)
        return
    assert user is not None
    kind, offset = _normalize_period(callback_data.kind, callback_data.offset)
    text, markup = await _card_screen(session, user, kind, offset, mine=True)
    await common.edit_or_answer(callback, text, markup)
    await callback.answer()


# --- 📤 Экспорт -----------------------------------------------------------------------------


@router.message(F.text == BTN_EXPORT, IsManager())
@router.message(Command("export"), IsManager())
async def export_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await common.edit_or_answer(message, EXPORT_PROMPT, keyboards.export_kb())


@router.callback_query(PeriodCB.filter(F.scope == "export"))
async def export_report(
    callback: CallbackQuery, callback_data: PeriodCB, session: AsyncSession, user: User | None, bot: Bot
) -> None:
    if not common.is_manager(user):
        await common.deny(callback)
        return
    kind, offset = _normalize_period(callback_data.kind, callback_data.offset)
    now = utcnow()
    period = periods.get_period(kind, offset, now)
    # Отвечаем сразу: сборка файла может занять время, а повторный answer после ошибки невозможен.
    await callback.answer()

    chat_id = _chat_id(callback)
    progress = await bot.send_message(chat_id, f"⏳ Готовлю отчёт: {esc(period.label)}…")
    with suppress(TelegramAPIError):
        await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)

    try:
        data = await export_service.build_report_xlsx(session, period, now)
    except DomainError as exc:
        await _finish_progress(progress, f"⚠️ {esc(exc.message)}")
        return
    except Exception:
        log.exception("Не удалось собрать Excel-отчёт (%s, offset=%s)", kind, offset)
        await session.rollback()
        await _finish_progress(progress, EXPORT_FAILED)
        return

    # Дата начала периода — по местному времени (в UTC неделя начинается ещё «вчера»).
    filename = f"kpi_{kind}_{to_local(period.start):%Y%m%d}.xlsx"
    try:
        await bot.send_document(
            chat_id,
            BufferedInputFile(data, filename=filename),
            caption=f"📊 Отчёт: {esc(period.label)}",
        )
    except TelegramAPIError:
        log.exception("Не удалось отправить Excel-отчёт %s", filename)
        await _finish_progress(progress, EXPORT_SEND_FAILED)
        return

    with suppress(TelegramAPIError):
        await progress.delete()
