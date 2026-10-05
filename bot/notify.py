"""Уведомления пользователям: задачи, решения руководителя, сданные результаты, заявки на доступ.

Функции notify_* и send_attachments никогда не бросают исключений: действие пользователя к этому
моменту уже сохранено, поэтому ошибка доставки только пишется в лог. Тексты — HTML
(parse_mode по умолчанию у Bot), пользовательский текст экранируется.

Уведомления одному человеку возвращают, доставлено ли сообщение (True/False; None — если упали
с ошибкой): руководителю нельзя писать «исполнитель получил уведомление», если тот заблокировал
бота или у него больше нет доступа. notify_proposal возвращает число уведомлённых руководителей.
"""

from __future__ import annotations

import asyncio
import enum
import functools
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    OPEN_STATUSES,
    Attachment,
    AttachmentKind,
    Priority,
    Role,
    Submission,
    Task,
    TaskStatus,
    User,
    UserStatus,
)
from bot.services import users as users_svc
from bot.ui import keyboards, render
from bot.ui.callbacks import TaskCB
from bot.utils.dates import fmt_deadline
from bot.utils.text import esc, fmt_num, truncate

__all__ = [
    "Delivery",
    "send_text",
    "safe_send",
    "send_attachments",
    "responsible_managers",
    "task_button_kb",
    "notify_new_task",
    "notify_proposal",
    "notify_proposal_decision",
    "notify_task_changed",
    "notify_task_cancelled",
    "notify_submission",
    "notify_review_result",
    "notify_rework",
    "notify_registration",
    "notify_user_decision",
]

log = logging.getLogger(__name__)

_MSG_LIMIT = 4000         # запас до лимита Telegram 4096 символов
_MAX_RETRY_WAIT = 60      # дольше ждать по флуд-лимиту не будем — повторит планировщик
_MEDIA_GROUP_SIZE = 10    # Telegram: в альбоме 2–10 элементов
_VALUE_LIMIT = 300        # сколько символов старого/нового значения показывать в «что изменилось»
_SEND_ERRORS = (TelegramAPIError, OSError)  # OSError покрывает и TimeoutError

_FIELD_LABELS = {
    "title": "Название",
    "expected_result": "Ожидаемый результат",
    "description": "Описание",
    "plan_value": "План",
    "plan_unit": "Единица плана",
    "deadline": "Срок",
    "priority": "Приоритет",
    "weight": "Вес",
}


class Delivery(enum.Enum):
    """Итог отправки. Планировщику нужно знать, есть ли смысл повторять напоминание."""

    SENT = "sent"
    BLOCKED = "blocked"    # пользователь заблокировал бота или чат недоступен — повтор не поможет
    REJECTED = "rejected"  # Telegram отклонил запрос (ошибка в данных) — повтор не поможет
    FAILED = "failed"      # сеть, сервер Telegram, флуд-лимит — можно повторить позже


# --- Отправка без исключений -------------------------------------------------------------------


def _reason(exc: BaseException) -> str:
    """Причина для лога. Текст сетевых ошибок не пишем: в нём может оказаться URL с токеном бота."""
    if isinstance(exc, TelegramAPIError) and not isinstance(exc, TelegramNetworkError):
        return exc.message
    return type(exc).__name__


def _failure(exc: BaseException, chat_id: int, what: str) -> tuple[None, Delivery]:
    if isinstance(exc, TelegramForbiddenError):
        log.info("Чат %s недоступен (бот заблокирован?) — %s не доставлено", chat_id, what)
        return None, Delivery.BLOCKED
    if isinstance(exc, TelegramBadRequest):
        log.warning("Telegram отклонил %s для чата %s: %s", what, chat_id, _reason(exc))
        return None, Delivery.REJECTED
    log.warning("Не удалось отправить %s в чат %s: %s", what, chat_id, _reason(exc))
    return None, Delivery.FAILED


async def _call[T](action: Callable[[], Awaitable[T]], chat_id: int, what: str) -> tuple[T | None, Delivery]:
    """Запрос к Telegram с обработкой ошибок; при флуд-лимите — подождать и повторить один раз."""
    try:
        return await action(), Delivery.SENT
    except TelegramRetryAfter as exc:
        if exc.retry_after > _MAX_RETRY_WAIT:
            return _failure(exc, chat_id, what)
        log.info("Флуд-лимит Telegram: ждём %s с и повторяем (%s, чат %s)", exc.retry_after, what, chat_id)
        await asyncio.sleep(exc.retry_after)
    except _SEND_ERRORS as exc:
        return _failure(exc, chat_id, what)
    try:
        return await action(), Delivery.SENT
    except _SEND_ERRORS as exc:
        return _failure(exc, chat_id, what)


async def send_text(bot: Bot, chat_id: int, text: str, **kwargs: Any) -> tuple[Message | None, Delivery]:
    """Отправить сообщение и вернуть (сообщение или None, итог доставки). Не бросает ошибок Telegram."""
    return await _call(lambda: bot.send_message(chat_id, text, **kwargs), chat_id, "сообщение")


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs: Any) -> Message | None:
    """Отправить сообщение; при любой ошибке Telegram — запись в лог и None."""
    message, _ = await send_text(bot, chat_id, text, **kwargs)
    return message


def _never_raise[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R | None]]:
    """Уведомление не должно ломать уже сохранённое действие: любая ошибка только логируется
    (тогда результат — None, то есть «не доставлено»)."""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
        try:
            return await func(*args, **kwargs)
        except Exception:
            log.exception("Ошибка при отправке уведомления (%s)", func.__name__)
            return None

    return wrapper


# --- Файлы-подтверждения -----------------------------------------------------------------------


def _file_name(att: Attachment, number: int) -> str:
    if att.file_name:
        return att.file_name
    return f"Видео {number}" if att.kind == AttachmentKind.VIDEO else f"Файл {number}"


async def _send_photos(bot: Bot, chat_id: int, photos: Sequence[Attachment], caption: str) -> Delivery:
    """Одно фото — send_photo, несколько — альбомом (подпись у первого)."""
    if len(photos) == 1:
        _, status = await _call(
            lambda: bot.send_photo(chat_id, photos[0].file_id, caption=caption), chat_id, "фото"
        )
        return status
    media = [
        InputMediaPhoto(media=att.file_id, caption=caption if index == 0 else None)
        for index, att in enumerate(photos)
    ]
    _, status = await _call(lambda: bot.send_media_group(chat_id, media), chat_id, "альбом фото")
    return status


async def _send_file(bot: Bot, chat_id: int, att: Attachment, caption: str, name: str) -> Delivery:
    """Видео — send_video, всё остальное — send_document (по file_id, без скачивания)."""
    if att.kind == AttachmentKind.VIDEO:
        _, status = await _call(lambda: bot.send_video(chat_id, att.file_id, caption=caption), chat_id, name)
    else:
        _, status = await _call(lambda: bot.send_document(chat_id, att.file_id, caption=caption), chat_id, name)
    return status


@_never_raise
async def send_attachments(bot: Bot, chat_id: int, sub: Submission) -> None:
    """Переслать файлы сдачи: фото — альбомами по 10, документы и видео — по одному с подписью."""
    where = f"задача #{sub.task_id}, попытка {sub.attempt}"
    photos = [att for att in sub.attachments if att.kind == AttachmentKind.PHOTO]
    others = [att for att in sub.attachments if att.kind != AttachmentKind.PHOTO]
    for start in range(0, len(photos), _MEDIA_GROUP_SIZE):
        chunk = photos[start : start + _MEDIA_GROUP_SIZE]
        if await _send_photos(bot, chat_id, chunk, f"📷 Фото · {where}") is Delivery.BLOCKED:
            return
    for number, att in enumerate(others, start=1):
        name = _file_name(att, number)
        caption = f"📎 {esc(name)}\n{where}"
        if await _send_file(bot, chat_id, att, caption, name) is Delivery.BLOCKED:
            return


# --- Получатели и общие кусочки текста ---------------------------------------------------------


def _reachable(user: User | None) -> bool:
    """Писать стоит только активным пользователям."""
    return user is not None and user.status == UserStatus.ACTIVE


def responsible_managers(task: Task, managers: Sequence[User]) -> list[User]:
    """Ответственный руководитель задачи, если он среди активных; иначе — все активные руководители."""
    own = [manager for manager in managers if manager.id == task.manager_id]
    return own or list(managers)


def task_button_kb(task: Task, text: str, action: str) -> InlineKeyboardMarkup:
    """Одна кнопка действия с задачей: «📋 Открыть» (open), «🔍 Проверить» (review) и т. п."""
    button = InlineKeyboardButton(text=text, callback_data=TaskCB(action=action, task_id=task.id).pack())
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def _open_kb(task: Task) -> InlineKeyboardMarkup:
    """Исполнителю: открытую задачу можно сразу сдать, остальные — только открыть."""
    if task.status in OPEN_STATUSES:
        return keyboards.submit_kb(task)
    return task_button_kb(task, "📋 Открыть", "open")


def _compose(*blocks: str) -> str:
    """Склеить блоки текста и уложиться в лимит Telegram."""
    return truncate("\n\n".join(block for block in blocks if block), _MSG_LIMIT)


def _title(task: Task) -> str:
    return f"📌 <b>#{task.id}</b> «{esc(task.title)}»"


def _clip(value: object, limit: int = _VALUE_LIMIT) -> str:
    text = " ".join(str(value).split())
    return esc(text if len(text) <= limit else text[: limit - 1].rstrip() + "…")


def _change_value(field: str, value: object, unit: str | None = None) -> str:
    """Старое/новое значение поля задачи в читаемом виде (HTML-экранировано).

    unit — единица плана для plan_value («100 договоров», а не голое «100»).
    """
    if value is None or value == "":
        return "—"
    if isinstance(value, datetime):
        return fmt_deadline(value)
    if field == "priority":
        try:
            return render.PRIORITY_LABELS[Priority(value)]
        except ValueError:
            return _clip(value)
    if field == "weight":
        return f"{esc(value)} %"
    if field == "plan_value" and isinstance(value, int | float):
        return f"{fmt_num(value)} {_clip(unit, 64)}" if unit else fmt_num(value)
    return f"«{_clip(value)}»"


def _pair(change: Any) -> tuple[Any, Any]:
    if isinstance(change, tuple | list) and len(change) == 2:
        return change[0], change[1]
    return None, change


def _change_lines(changes: dict[str, Any], unit: str | None = None) -> list[str]:
    """Строки «• Поле: было → стало». unit — текущая единица плана задачи: план показывается
    с единицей; если единица менялась вместе с числом, отдельной строкой она не дублируется."""
    old_unit, new_unit = _pair(changes["plan_unit"]) if "plan_unit" in changes else (unit, unit)
    lines = []
    for field, change in changes.items():
        if field == "plan_unit" and "plan_value" in changes:
            continue
        old, new = _pair(change)
        label = _FIELD_LABELS.get(field, esc(field))
        old_text = _change_value(field, old, old_unit)
        new_text = _change_value(field, new, new_unit)
        lines.append(f"• {label}: {old_text} → <b>{new_text}</b>")
    return lines


# --- Задачи ------------------------------------------------------------------------------------


@_never_raise
async def notify_new_task(bot: Bot, task: Task) -> bool:
    """Исполнителю: карточка новой задачи + [✅ Принял в работу] [📋 Открыть]. -> доставлено ли."""
    if not _reachable(task.assignee):
        return False
    text = _compose(
        "🆕 <b>Вам поставлена новая задача</b>",
        render.task_card(task, show_assignee=False),
        "Нажмите «✅ Принял в работу», чтобы подтвердить получение.",
    )
    return await safe_send(bot, task.assignee.tg_id, text, reply_markup=keyboards.new_task_kb(task)) is not None


@_never_raise
async def notify_proposal(bot: Bot, session: AsyncSession, task: Task) -> int:
    """Всем активным руководителям: поручение сотрудника + [✅ Подтвердить] [✏️ Изменить] [❌ Отклонить].

    -> сколько руководителей получили уведомление.
    """
    text = _compose(
        "📥 <b>Сотрудник внёс поручение — нужно ваше решение</b>",
        render.task_card(task),
        "Подтвердите, измените или отклоните поручение.",
    )
    delivered = 0
    for manager in await users_svc.list_managers(session):
        if await safe_send(bot, manager.tg_id, text, reply_markup=keyboards.proposal_kb(task)) is not None:
            delivered += 1
    return delivered


@_never_raise
async def notify_proposal_decision(bot: Bot, task: Task, approved: bool, reason: str | None = None) -> bool:
    """Сотруднику: поручение подтверждено (карточка) или отклонено (с причиной). -> доставлено ли."""
    if not _reachable(task.assignee):
        return False
    if approved:
        text = _compose(
            "✅ <b>Руководитель подтвердил ваше поручение</b>",
            render.task_card(task, show_assignee=False),
        )
        markup: InlineKeyboardMarkup | None = _open_kb(task)
    else:
        lines = ["❌ <b>Руководитель отклонил ваше поручение</b>", _title(task)]
        if reason:
            lines.append(f"💬 Причина: {_clip(reason, 1000)}")
        text, markup = _compose("\n".join(lines)), None
    return await safe_send(bot, task.assignee.tg_id, text, reply_markup=markup) is not None


@_never_raise
async def notify_task_changed(bot: Bot, task: Task, changes: dict[str, tuple]) -> bool:
    """Исполнителю: какие поля задачи изменил руководитель (было → стало). -> доставлено ли."""
    if not changes or not _reachable(task.assignee):
        return False
    if task.status == TaskStatus.PROPOSED:
        header = "✏️ <b>Руководитель скорректировал ваше поручение</b>"
    else:
        header = "✏️ <b>Руководитель изменил задачу</b>"
    text = _compose(f"{header}\n{_title(task)}", "\n".join(_change_lines(changes, task.plan_unit)))
    return await safe_send(bot, task.assignee.tg_id, text, reply_markup=_open_kb(task)) is not None


@_never_raise
async def notify_task_cancelled(bot: Bot, task: Task, reason: str | None = None) -> bool:
    """Исполнителю: задача отменена, сдавать результат не нужно. -> доставлено ли."""
    if not _reachable(task.assignee):
        return False
    lines = ["🚫 <b>Задача отменена руководителем</b>", _title(task)]
    if reason:
        lines.append(f"💬 Причина: {_clip(reason, 1000)}")
    lines.append("Сдавать результат по ней не нужно.")
    return await safe_send(bot, task.assignee.tg_id, _compose("\n".join(lines))) is not None


# --- Сдача и проверка результата ---------------------------------------------------------------


@_never_raise
async def notify_submission(bot: Bot, session: AsyncSession, task: Task, sub: Submission) -> None:
    """Руководителю задачи (или всем активным руководителям): план ↔ факт + кнопки проверки, затем файлы."""
    recipients = responsible_managers(task, await users_svc.list_managers(session))
    if not recipients:
        log.warning("Некому проверить результат по задаче #%s: нет активных руководителей", task.id)
        return
    text = render.submission_text(task, sub)
    markup = keyboards.review_kb(sub)
    for manager in recipients:
        message = await safe_send(bot, manager.tg_id, text, reply_markup=markup)
        if message is not None and sub.attachments:
            await send_attachments(bot, manager.tg_id, sub)


@_never_raise
async def notify_review_result(bot: Bot, task: Task, sub: Submission) -> bool:
    """Исполнителю: итоговая оценка, решение и комментарий руководителя. -> доставлено ли."""
    if not _reachable(task.assignee):
        return False
    text = render.review_result_text(task, sub)
    return await safe_send(bot, task.assignee.tg_id, text, reply_markup=_open_kb(task)) is not None


@_never_raise
async def notify_rework(bot: Bot, task: Task, sub: Submission) -> bool:
    """Исполнителю: что доработать и срок (возможно, новый) + [📤 Сдать результат] [📋 Открыть].

    -> доставлено ли.
    """
    if not _reachable(task.assignee):
        return False
    text = render.review_result_text(task, sub)
    return await safe_send(bot, task.assignee.tg_id, text, reply_markup=keyboards.submit_kb(task)) is not None


# --- Пользователи ------------------------------------------------------------------------------


@_never_raise
async def notify_registration(bot: Bot, session: AsyncSession, user: User) -> None:
    """Всем активным руководителям: новая заявка на доступ + [✅ Подтвердить] [❌ Отклонить]."""
    lines = [
        "👤 <b>Новая заявка на доступ к боту</b>",
        "",
        f"ФИО: <b>{_clip(user.full_name, 200)}</b>",
        f"Должность: {_clip(user.position, 200) if user.position else 'не указана'}",
    ]
    if user.username:
        lines.append(f"Telegram: @{esc(user.username)}")
    lines += ["", "Подтвердите, если это ваш сотрудник."]
    text = _compose("\n".join(lines))
    for manager in await users_svc.list_managers(session):
        await safe_send(bot, manager.tg_id, text, reply_markup=keyboards.registration_kb(user))


async def notify_user_decision(bot: Bot, user: User, approved: bool) -> bool:
    """Пользователю: доступ открыт (+ главное меню) или заявка отклонена (меню убирается).

    Возвращает True, если сообщение доставлено: руководителю не стоит писать «пользователю отправлено
    уведомление», если тот заблокировал бота. Исключений не бросает.
    """
    if approved:
        role = "руководитель" if user.role == Role.MANAGER else "сотрудник"
        text = (
            "✅ <b>Доступ к боту открыт!</b>\n"
            f"Ваша роль: {role}.\n\n"
            "Воспользуйтесь меню внизу 👇 Как всё устроено — /help."
        )
    else:
        text = "❌ Заявка на доступ отклонена руководителем.\nЕсли это ошибка — обратитесь к руководителю."
    try:
        return await safe_send(bot, user.tg_id, text, reply_markup=keyboards.main_menu(user)) is not None
    except Exception:
        log.exception("Ошибка при отправке уведомления (notify_user_decision)")
        return False
