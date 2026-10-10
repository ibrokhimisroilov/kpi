"""Перевод на выходе в Telegram и язык текущего апдейта (SPEC.md §14).

* ``TranslateRequests`` — middleware сессии бота: у каждого запроса к Telegram тексты, подписи и надписи кнопок
  переводятся на язык получателя (``i18n.lang_of(chat_id)``; где чата нет — язык текущего апдейта) и
  освобождаются от меток слов пользователя. Через него идут и ответы диалогов, и уведомления другим людям, и
  напоминания по расписанию — поэтому сотрудник с узбекским получает уведомление по-узбекски, даже если
  кнопку нажал начальник с русским.
* ``LanguageMiddleware`` — для каждого апдейта: запомнить язык автора (``users.lang``; не выбран — по языку
  его Telegram, и это значение записывается), сделать его языком апдейта; надпись кнопки главного меню на
  узбекском вернуть к русской — хендлеры ждут русскую.

``install(bot)`` подключает перевод к боту; ``load_languages`` при запуске читает языки всех пользователей.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.methods.base import Response, TelegramType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonDefault,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
    WebAppInfo,
)
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import i18n
from bot.config import get_settings
from bot.db.models import User
from bot.utils.text import truncate

__all__ = [
    "LanguageMiddleware",
    "TranslateRequests",
    "install",
    "load_languages",
    "sync_menu_button",
    "translate_markup",
]

log = logging.getLogger(__name__)

_TEXT_FIELDS = ("text", "caption")
# Пределы Telegram: перевод бывает длиннее русского текста, который обрезали под предел заранее.
_LIMITS = {"text": 4096, "caption": 1024}
_ALERT_LIMIT = 200


def translate_markup(markup: Any, lang: str) -> Any:
    """Надписи кнопок inline- и reply-клавиатуры — на язык ``lang`` (без меток)."""
    if isinstance(markup, InlineKeyboardMarkup):
        rows = [
            [button.model_copy(update={"text": i18n.tr(button.text, lang)}) for button in row]
            for row in markup.inline_keyboard
        ]
        return markup.model_copy(update={"inline_keyboard": rows})
    if isinstance(markup, ReplyKeyboardMarkup):
        rows = [
            [
                button.model_copy(update={"text": i18n.tr(button.text, lang)}) if isinstance(button, KeyboardButton) else button
                for button in row
            ]
            for row in markup.keyboard
        ]
        update: dict[str, Any] = {"keyboard": rows}
        if markup.input_field_placeholder:
            update["input_field_placeholder"] = i18n.tr(markup.input_field_placeholder, lang)
        return markup.model_copy(update=update)
    return markup


def _translated(method: TelegramMethod[Any]) -> TelegramMethod[Any]:
    """Копия запроса с текстами на языке получателя; запрос без текстов возвращается как есть."""
    chat_id = getattr(method, "chat_id", None)
    # Личный чат: его номер — Telegram ID человека. Запрос без чата (ответ на нажатие кнопки) — язык апдейта.
    lang = i18n.lang_of(chat_id) if isinstance(chat_id, int) else i18n.current()
    update: dict[str, Any] = {}
    for name in _TEXT_FIELDS:
        value = getattr(method, name, None)
        if isinstance(value, str) and value:
            translated_text = i18n.tr(value, lang)
            if lang != i18n.RU and len(translated_text) > len(value):
                if isinstance(method, AnswerCallbackQuery):
                    if len(translated_text) > _ALERT_LIMIT:
                        translated_text = translated_text[: _ALERT_LIMIT - 1].rstrip() + "…"
                else:
                    translated_text = truncate(translated_text, _LIMITS[name])
            update[name] = translated_text
    markup = getattr(method, "reply_markup", None)
    if markup is not None:
        translated = translate_markup(markup, lang)
        if translated is not markup:
            update["reply_markup"] = translated
    media = getattr(method, "media", None)
    if isinstance(media, list):  # альбом: подписи у элементов
        items = []
        changed = False
        for item in media:
            caption = getattr(item, "caption", None)
            if isinstance(caption, str) and caption:
                items.append(item.model_copy(update={"caption": i18n.tr(caption, lang)}))
                changed = True
            else:
                items.append(item)
        if changed:
            update["media"] = items
    return method.model_copy(update=update) if update else method


class TranslateRequests(BaseRequestMiddleware):
    """Перевод текстов исходящих запросов на язык получателя (см. описание модуля)."""

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        try:
            method = _translated(method)
        except Exception:  # noqa: BLE001 - перевод не должен мешать отправке
            log.exception("Не удалось перевести запрос %s — отправляю как есть", type(method).__name__)
        return await make_request(bot, method)


def install(bot: Bot) -> None:
    """Подключить перевод исходящих запросов к боту (один раз на объект бота)."""
    session = bot.session
    if any(isinstance(item, TranslateRequests) for item in session.middleware):
        return
    session.middleware(TranslateRequests())


MENU_BUTTON_UZ = "Ochish"  # кнопка меню чата «Открыть» для тех, кто выбрал узбекский


async def sync_menu_button(bot: Bot | None, tg_id: int, lang: str) -> None:
    """Кнопка меню чата, которая открывает приложение, — на языке человека. Общая кнопка бота русская
    («Открыть», bot.main); узбекскому ставится своя для его чата, русскому — возвращается общая.
    Приложения нет (не webhook) — ничего не делается. Ошибка Telegram — только в лог: чат работает и так."""
    url = get_settings().webapp_url
    if bot is None or not url:
        return
    button: MenuButtonWebApp | MenuButtonDefault
    if i18n.normalize(lang) == i18n.UZ:
        button = MenuButtonWebApp(text=MENU_BUTTON_UZ, web_app=WebAppInfo(url=url))
    else:
        button = MenuButtonDefault()
    try:
        await bot.set_chat_menu_button(chat_id=tg_id, menu_button=button)
    except TelegramAPIError as exc:
        log.info("Не удалось обновить кнопку меню чата: %s", type(exc).__name__)


async def load_languages(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Запомнить языки всех пользователей (при запуске): уведомления сразу уходят на нужном языке."""
    async with sessionmaker() as session:
        rows = (await session.execute(select(User.tg_id, User.lang))).all()
    for tg_id, lang in rows:
        i18n.remember(tg_id, lang)  # язык не выбран — русский
    return len(rows)


class LanguageMiddleware(BaseMiddleware):
    """Язык автора апдейта (см. описание модуля). Ставится после UserMiddleware: ему нужен ``data["user"]``."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        bot = data.get("bot")
        if isinstance(bot, Bot):
            install(bot)
        tg_user: TgUser | None = data.get("event_from_user")
        user: User | None = data.get("user")
        if user is not None and user.lang:
            lang = i18n.normalize(user.lang)
        else:
            lang = i18n.from_telegram(tg_user.language_code if tg_user is not None else None)
            if user is not None and lang != i18n.RU:
                # Язык не выбран, а Telegram у человека на узбекском: запоминаем — уведомления пойдут на нём
                # и после перезапуска бота. Русский — значение по умолчанию, его записывать незачем.
                user.lang = lang
                if tg_user is not None:
                    await sync_menu_button(bot if isinstance(bot, Bot) else None, tg_user.id, lang)
        if tg_user is not None:
            i18n.remember(tg_user.id, lang)
        i18n.set_current(lang)
        return await handler(event, data)


class MenuTextMiddleware(BaseMiddleware):
    """Кнопка главного меню с узбекской надписью -> русская надпись, которую ждут хендлеры."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.text:
            source = i18n.menu_source(event.text)
            if source is not None:
                event = event.model_copy(update={"text": source})
        return await handler(event, data)
