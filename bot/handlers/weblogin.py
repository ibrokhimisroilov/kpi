"""Подтверждение входа на сайт (docs/MINIAPP_SPEC.md §15): ``/start login_<код>`` и кнопки под сообщением.

Сайт показывает число и ссылку на бота (кнопкой и QR-кодом). Человек открывает ссылку — бот присылает три
числа и «Это не я». Верное число подтверждает вход в том браузере, который его начал; неверное число и
«Это не я» вход отменяют. Подтвердить может только тот, кто открыл ссылку, и только активный пользователь.

Роутер подключается раньше ``start``: обычный ``/start`` он не трогает, а диалог, в котором человек сейчас
находится (постановка задачи, сдача), не прерывает.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User, UserStatus, WebLogin
from bot.handlers import common
from bot.handlers.start import TXT_BLOCKED, TXT_PENDING
from bot.services import weblogin
from bot.ui.callbacks import WebLoginCB, WebOutCB
from bot.utils.dates import utcnow
from bot.utils.text import esc

log = logging.getLogger(__name__)

router = Router(name="weblogin")

TXT_PROMPT = (
    "🔐 <b>Вход на сайт «Эффективность»</b>\n"
    "{device}\n"
    "Нажмите число, которое показано на сайте.\n"
    "Если сайт открывали не вы — нажмите «Это не я».\n"
    "По чужой просьбе вход не подтверждайте: этот человек откроет сайт от вашего имени."
)
TXT_DEVICE = "Устройство: {device}\n"
TXT_GONE = "⏱ Ссылка для входа устарела. Вернитесь на сайт и нажмите «Войти через Telegram» ещё раз."
TXT_FOREIGN = "⛔ Эта ссылка для входа уже открыта другим человеком. Начните вход на сайте заново."
TXT_APPROVED = "✅ Вход на сайт подтверждён. Вернитесь на сайт — он откроется сам."
TXT_WRONG = "⚠️ Число не совпало — вход отменён. Начните вход на сайте заново."
TXT_DENIED = "🚫 Вход на сайт отменён. Если сайт открывали не вы, больше ничего делать не нужно."
TXT_NEED_START = "Вход на сайт доступен после регистрации в боте. Нажмите /start."
BTN_NOT_ME = "🚫 Это не я"
BTN_SIGN_OUT_ALL = "🚪 Выйти с сайта на всех устройствах"
TXT_SIGNED_OUT_ALL = (
    "🚪 Все входы на сайт завершены. Чтобы войти снова, откройте сайт и нажмите «Войти через Telegram»."
)
PICK_NOT_ME = "no"

_OUTCOME_TEXT = {"approved": TXT_APPROVED, "wrong": TXT_WRONG, "denied": TXT_DENIED, "gone": TXT_GONE}


def _no_access_text(user: User | None) -> str | None:
    """Почему человеку нельзя входить на сайт; None — можно (активный пользователь)."""
    if user is None or (user.status == UserStatus.PENDING and not user.full_name):
        return TXT_NEED_START
    if user.status == UserStatus.PENDING:
        return TXT_PENDING
    if user.status == UserStatus.BLOCKED:
        return TXT_BLOCKED
    return None


def _keyboard(attempt: WebLogin) -> InlineKeyboardMarkup:
    numbers = [
        InlineKeyboardButton(text=number, callback_data=WebLoginCB(login_id=attempt.id, pick=number).pack())
        for number in attempt.options.split(",")
    ]
    not_me = InlineKeyboardButton(
        text=BTN_NOT_ME, callback_data=WebLoginCB(login_id=attempt.id, pick=PICK_NOT_ME).pack()
    )
    return InlineKeyboardMarkup(inline_keyboard=[numbers, [not_me]])


@router.message(CommandStart(deep_link=True, magic=F.args.startswith(weblogin.START_PREFIX)))
async def login_link_opened(
    message: Message, command: CommandObject, session: AsyncSession, user: User | None
) -> None:
    token = (command.args or "")[len(weblogin.START_PREFIX) :]
    now = utcnow()
    refusal = _no_access_text(user)
    if refusal is not None or message.from_user is None:
        await weblogin.refuse(session, token, now)
        await session.commit()
        await message.answer(refusal or TXT_GONE)
        return
    outcome, attempt = await weblogin.open_in_bot(session, token, message.from_user.id, now)
    await session.commit()
    if attempt is None:
        await message.answer(TXT_FOREIGN if outcome == "foreign" else TXT_GONE)
        return
    device = TXT_DEVICE.format(device=esc(attempt.device)) if attempt.device else ""
    await message.answer(TXT_PROMPT.format(device=device), reply_markup=_keyboard(attempt))


@router.callback_query(WebLoginCB.filter())
async def login_decided(
    callback: CallbackQuery, callback_data: WebLoginCB, session: AsyncSession, user: User | None
) -> None:
    refusal = _no_access_text(user)
    pick = None if callback_data.pick == PICK_NOT_ME else callback_data.pick
    if user is None:
        outcome = "gone"
    else:
        # Заблокированный между открытием ссылки и нажатием числа вход не получит: decide подтверждает
        # только активному пользователю (попытка при этом закрывается).
        outcome = await weblogin.decide(session, callback_data.login_id, user, pick, utcnow())
    await session.commit()
    await callback.answer()
    text = refusal if refusal is not None and outcome != "gone" else _OUTCOME_TEXT[outcome]
    # Под подтверждённым входом остаётся кнопка: подтвердил по ошибке или потерял устройство — выйти везде.
    markup = _sign_out_keyboard() if outcome == "approved" else None
    await common.edit_or_answer(callback, text, markup)
    if outcome == "approved":
        log.info("Вход на сайт подтверждён в боте")


def _sign_out_keyboard() -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text=BTN_SIGN_OUT_ALL, callback_data=WebOutCB().pack())
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


@router.callback_query(WebOutCB.filter())
async def sign_out_everywhere(callback: CallbackQuery, session: AsyncSession, user: User | None) -> None:
    """Закрыть все сессии этого человека на сайте. Приложение в Telegram при этом работает как раньше."""
    if user is not None:
        await weblogin.end_all(session, user.id)
        await session.commit()
        log.info("Все входы на сайт завершены из бота")
    await callback.answer()
    await common.edit_or_answer(callback, TXT_SIGNED_OUT_ALL)
