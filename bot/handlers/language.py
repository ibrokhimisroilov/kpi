"""Выбор языка интерфейса: «🌐 Til / Язык» в меню, /lang (/til) и кнопки под первым сообщением (SPEC.md §14).

Язык хранится у пользователя (``users.lang``) и действует сразу: ответ на выбор и главное меню приходят уже
на новом языке. Тексты пишутся по-русски — на узбекский их переводит слой ``bot.i18n`` при отправке.
Роутер подключается первым: кнопка и команда работают посреди любого диалога и во время регистрации
(диалог при этом не прерывается).
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import i18n
from bot.db.models import User
from bot.handlers import common
from bot.i18n.telegram import sync_menu_button
from bot.services import users
from bot.ui import keyboards
from bot.ui.callbacks import LangCB
from bot.ui.texts import BTN_LANG, TXT_CHOOSE_LANG

log = logging.getLogger(__name__)

router = Router(name="language")

# По-русски — как все тексты бота; для узбекского каталог переводит эту строку в «✅ Til: Oʻzbekcha.».
LANG_SET = "✅ Язык: Русский."
MENU_TEXT = "🏠 Главное меню. Выберите действие на клавиатуре ниже 👇"


@router.message(F.text == BTN_LANG)
@router.message(Command("lang", "til"))
async def choose_language(message: Message) -> None:
    await message.answer(TXT_CHOOSE_LANG, reply_markup=keyboards.language_kb())


@router.callback_query(LangCB.filter())
async def language_picked(
    callback: CallbackQuery,
    callback_data: LangCB,
    session: AsyncSession,
    state: FSMContext,
    user: User | None,
) -> None:
    lang = i18n.normalize(callback_data.lang)
    if user is not None:
        await users.set_lang(session, user, lang)
        await session.commit()
    i18n.remember(callback.from_user.id, lang)
    i18n.set_current(lang)  # ответы на это нажатие — уже на выбранном языке
    await callback.answer()
    await common.edit_or_answer(callback, LANG_SET)
    await sync_menu_button(callback.bot, callback.from_user.id, lang)
    if user is not None and user.is_active:
        await common.send_new(callback, MENU_TEXT, keyboards.main_menu(user))
        return
    # Регистрация: вопрос анкеты повторяем на выбранном языке (диалог остаётся на том же шаге).
    from bot.handlers import start

    if await state.get_state() != start.RegistrationSG.full_name.state:
        return
    question_id = (await state.get_data()).get("q_msg_id")
    chat_id = callback.from_user.id
    if question_id and callback.bot is not None:
        try:  # тот же вопрос — на выбранном языке, без второго сообщения
            await callback.bot.edit_message_text(
                start.TXT_ASK_NAME, chat_id=chat_id, message_id=question_id, reply_markup=keyboards.cancel_kb()
            )
            return
        except TelegramAPIError as exc:
            if "message is not modified" in str(exc).lower():
                return  # выбран тот же язык — вопрос уже на нём
            log.debug("Вопрос анкеты не отредактирован (%s) — отправляю заново", exc)
    question = await common.send_new(callback, start.TXT_ASK_NAME, keyboards.cancel_kb())
    if question is not None:
        await state.update_data(q_msg_id=question.message_id)
