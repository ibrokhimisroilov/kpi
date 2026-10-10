"""Фильтры ролей и пользовательского ввода.

Объект пользователя (`user: User | None`) кладёт в data bot.middlewares.UserMiddleware.
"""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.db.models import Role, User, UserStatus
from bot.ui.texts import MENU_BUTTONS


class IsActiveUser(BaseFilter):
    """Зарегистрированный и подтверждённый пользователь (любая роль)."""

    async def __call__(self, event: TelegramObject, user: User | None = None) -> bool:
        return user is not None and user.status == UserStatus.ACTIVE


class IsManager(BaseFilter):
    """Активный начальник."""

    async def __call__(self, event: TelegramObject, user: User | None = None) -> bool:
        return user is not None and user.status == UserStatus.ACTIVE and user.role == Role.MANAGER


class IsEmployee(BaseFilter):
    """Активный сотрудник."""

    async def __call__(self, event: TelegramObject, user: User | None = None) -> bool:
        return user is not None and user.status == UserStatus.ACTIVE and user.role == Role.EMPLOYEE


class TextInput(BaseFilter):
    """Обычный текст от пользователя: не команда и не кнопка главного меню.

    Используется во всех FSM-хендлерах, ожидающих ввод текста.
    """

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if not isinstance(event, Message) or not event.text:
            return False
        text = event.text.strip()
        return bool(text) and not text.startswith("/") and text not in MENU_BUTTONS
