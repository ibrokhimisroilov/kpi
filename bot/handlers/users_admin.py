"""Сотрудники (руководитель): список, заявки на доступ, роли, блокировка. SPEC 7.2.

* «👥 Сотрудники» / /staff — список всех пользователей (сначала заявки), по кнопке на каждого.
* UserCB("staff", 0, page) — листание списка (действие этого модуля, в SPEC не описано).
* UserCB("manage", user_id, page) — карточка пользователя с `keyboards.user_manage_kb`.
* UserCB approve / reject / block / unblock / role_mgr / role_emp — сервис -> commit ->
  обновлённая карточка. Те же хендлеры обслуживают кнопки уведомления о новой заявке
  (`keyboards.registration_kb`): сообщение-уведомление превращается в карточку.
* После блокировки и смены роли незаконченный диалог пользователя сбрасывается (он ему
  больше не подходит). Руководителю из ADMIN_IDS кнопки «понизить»/«заблокировать» не показываются.

Права проверяются внутри хендлеров (без фильтра роли на декораторе), чтобы у
неподходящего пользователя кнопка не «висела», а получила alert.
"""

from __future__ import annotations

import asyncio
import logging
import weakref

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notify
from bot.config import get_settings
from bot.db.models import Role, User, UserStatus
from bot.filters import IsManager
from bot.handlers import common
from bot.services import users as users_svc
from bot.services.errors import DomainError
from bot.ui import keyboards, render
from bot.ui.callbacks import UserCB
from bot.ui.texts import BTN_STAFF
from bot.utils.dates import fmt_date
from bot.utils.text import esc, truncate

log = logging.getLogger(__name__)

router = Router(name="users_admin")

PAGE_SIZE = 20
TEXT_LIMIT = 4000  # с запасом до лимита Telegram 4096

ACT_STAFF = "staff"    # листание списка (UserCB(action="staff", user_id=0, page=n))
ACT_MANAGE = "manage"  # открыть карточку пользователя
ACTIONS = ("approve", "reject", "block", "unblock", "role_mgr", "role_emp")
DECISIONS = ("approve", "reject")

ALREADY_PROCESSED = "Заявка уже обработана"
USER_NOT_FOUND = "Пользователь не найден."
ADMIN_NOTE = (
    "🔒 Руководитель указан в настройках бота (ADMIN_IDS) — понизить или заблокировать его нельзя."
)
# Кнопки, которые не показываются у руководителя из ADMIN_IDS.
_ADMIN_LOCKED = ("role_emp", "block")
# После этих действий незаконченный диалог пользователя сбрасывается (см. _reset_dialog).
RESET_DIALOG = ("block", "role_mgr", "role_emp")
DIALOG_DROPPED = "Незавершённое действие в боте отменено — начните его заново из меню."

# Действия с сотрудниками выполняются по одному. Апдейты разных руководителей обрабатываются
# параллельно, и без замка оба увидели бы старый статус: заявку одновременно подтвердили бы
# и отклонили, два руководителя понизили/заблокировали бы друг друга и оставили бот без
# руководителя. Бот работает одним процессом, так что замка в памяти достаточно.
# asyncio.Lock привязывается к event loop, поэтому замок свой на каждый цикл (тесты их меняют).
_action_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _action_lock() -> asyncio.Lock:
    return _action_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())

ROLE_LABELS = {
    Role.MANAGER: "👔 Руководитель",
    Role.EMPLOYEE: "👤 Сотрудник",
}
STATUS_LABELS = {
    UserStatus.PENDING: "⏳ Ждёт подтверждения",
    UserStatus.ACTIVE: "✅ Активен",
    UserStatus.BLOCKED: "🚫 Заблокирован",
}

# Группы в списке: (заголовок, значок на кнопке)
_GROUPS = (
    ("📥 <b>Заявки на доступ</b>", "⏳"),
    ("👤 <b>Сотрудники</b>", "👤"),
    ("👔 <b>Руководители</b>", "👔"),
    ("🚫 <b>Заблокированы</b>", "🚫"),
)

# action -> (короткий ответ на кнопку, строка над обновлённой карточкой)
_RESULTS: dict[str, tuple[str, str]] = {
    "approve": (
        "✅ Заявка подтверждена",
        "✅ <b>Заявка подтверждена</b> — доступ к боту открыт, пользователю отправлено уведомление.",
    ),
    "reject": (
        "❌ Заявка отклонена",
        "❌ <b>Заявка отклонена</b> — пользователю отправлено уведомление.",
    ),
    "block": (
        "🚫 Пользователь заблокирован",
        "🚫 <b>Пользователь заблокирован</b> — доступ к боту закрыт.",
    ),
    "unblock": (
        "✅ Пользователь разблокирован",
        "✅ <b>Пользователь разблокирован</b> — доступ к боту открыт.",
    ),
    "role_mgr": (
        "👔 Назначен руководителем",
        "👔 <b>Назначен руководителем</b> — теперь может ставить задачи, проверять результаты и смотреть отчёты.",
    ),
    "role_emp": (
        "👤 Роль изменена на «Сотрудник»",
        "👤 <b>Роль изменена на «Сотрудник»</b> — права руководителя сняты.",
    ),
}
# Решение по заявке не дошло до человека (заблокировал бота) — вместо «отправлено уведомление».
_UNDELIVERED: dict[str, str] = {
    "approve": (
        "✅ <b>Заявка подтверждена</b> — доступ к боту открыт.\n"
        "⚠️ Уведомление не доставлено: пользователь заблокировал бота. Сообщите ему лично — "
        "после /start у него появится меню."
    ),
    "reject": (
        "❌ <b>Заявка отклонена</b>.\n"
        "⚠️ Уведомление не доставлено: пользователь заблокировал бота."
    ),
}


# --- Список сотрудников ---------------------------------------------------------------------


def _group(u: User) -> int:
    """Порядок групп: заявки, активные сотрудники, активные руководители, заблокированные."""
    if u.status == UserStatus.PENDING:
        return 0
    if u.status == UserStatus.BLOCKED:
        return 3
    return 1 if u.role == Role.EMPLOYEE else 2


def _button_text(u: User) -> str:
    name = " ".join(u.full_name.split()) or f"ID {u.tg_id}"
    if len(name) > 48:
        name = name[:47] + "…"
    return f"{_GROUPS[_group(u)][1]} {name}"


def _staff_screen(all_users: list[User], page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура страницы списка. all_users — результат users.list_all."""
    # PENDING без ФИО — регистрация не завершена: подтверждать нечего, в список не берём.
    incomplete = sum(1 for u in all_users if u.status == UserStatus.PENDING and not u.full_name.strip())
    people = sorted(
        (u for u in all_users if not (u.status == UserStatus.PENDING and not u.full_name.strip())),
        key=_group,  # сортировка устойчивая: внутри группы сохраняется порядок list_all (по ФИО)
    )
    counts = [0, 0, 0, 0]
    for u in people:
        counts[_group(u)] += 1

    pages = max(1, (len(people) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    chunk = people[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    head = ["👥 <b>Сотрудники</b>"]
    head.append(
        f"Заявок: {counts[0]} · Сотрудников: {counts[1]} · "
        f"Руководителей: {counts[2]} · Заблокировано: {counts[3]}"
    )
    if counts[0]:
        head.append("⏳ <b>Есть новые заявки</b> — нажмите на имя, чтобы подтвердить или отклонить.")

    tail: list[str] = []
    if not counts[0] and not counts[1]:
        # Ни заявок, ни активных сотрудников (например, в системе только руководители).
        tail.append("")
        tail.append(
            "Пока в системе нет сотрудников. Попросите их найти бота в Telegram и нажать /start — "
            "заявки появятся здесь."
        )
    if incomplete:
        tail.append("")
        tail.append(f"ℹ️ Ещё {incomplete} чел. начали регистрацию, но пока не указали ФИО.")
    tail.append("")
    if pages > 1:
        tail.append(f"Страница {page + 1} из {pages}.")
    tail.append("Нажмите на человека, чтобы открыть карточку: подтвердить заявку, сменить роль или заблокировать.")

    lines = [*head, *_people_lines(chunk, page * PAGE_SIZE + 1, compact=False), *tail]
    if len("\n".join(lines)) > TEXT_LIMIT:
        # Очень длинные ФИО/должности: подробные строки не влезают в сообщение — тогда коротко,
        # чтобы на странице были видны все люди, номер страницы и подсказка.
        lines = [*head, *_people_lines(chunk, page * PAGE_SIZE + 1, compact=True), *tail]

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=_button_text(u),
                callback_data=UserCB(action=ACT_MANAGE, user_id=u.id, page=page).pack(),
            )
        ]
        for u in chunk
    ]
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(_staff_button("◀", page - 1))
        nav.append(_staff_button(f"🔄 {page + 1}/{pages}", page))
        if page < pages - 1:
            nav.append(_staff_button("▶", page + 1))
        rows.append(nav)
    else:
        rows.append([_staff_button("🔄 Обновить", page)])
    return truncate("\n".join(lines), TEXT_LIMIT), InlineKeyboardMarkup(inline_keyboard=rows)


def _people_lines(chunk: list[User], start: int, *, compact: bool) -> list[str]:
    """Строки списка по группам: «1. 🟢 Иванов Иван · сотрудник · …» (compact — только имя)."""
    lines: list[str] = []
    current_group: int | None = None
    for idx, u in enumerate(chunk, start=start):
        group = _group(u)
        if group != current_group:
            current_group = group
            lines.append("")
            lines.append(_GROUPS[group][0])
        lines.append(f"{idx}. {esc(_button_text(u)) if compact else render.user_line(u)}")
    return lines


def _staff_button(text: str, page: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=UserCB(action=ACT_STAFF, user_id=0, page=page).pack()
    )


# --- Карточка пользователя ------------------------------------------------------------------


def _card_text(target: User, viewer: User, note: str | None = None) -> str:
    lines: list[str] = []
    if note:
        lines += [note, ""]
    lines.append(render.user_line(target))
    lines.append("")
    lines.append(f"<b>Должность:</b> {esc(target.position) if target.position else '—'}")
    lines.append(f"<b>Роль:</b> {ROLE_LABELS.get(target.role, esc(target.role))}")
    lines.append(f"<b>Статус:</b> {STATUS_LABELS.get(target.status, esc(target.status))}")
    if target.created_at:
        lines.append(f"<b>Дата регистрации:</b> {fmt_date(target.created_at)}")

    if target.id == viewer.id:
        lines += ["", "Это вы."]
    elif _is_config_admin(target):
        lines += ["", ADMIN_NOTE]
    elif target.status == UserStatus.PENDING:
        lines += ["", "Подтвердите заявку, чтобы открыть доступ к боту, или отклоните её."]
    elif target.status == UserStatus.BLOCKED:
        lines += ["", "У пользователя нет доступа к боту. Его можно разблокировать."]
    return truncate("\n".join(lines))


def _card_kb(target: User, viewer: User, page: int) -> InlineKeyboardMarkup:
    """keyboards.user_manage_kb + кнопка возврата к списку.

    `user_manage_kb` не знает о странице списка, поэтому кнопкам действий этого модуля
    проставляем `page`: после действия «◀ К списку» вернёт на ту же страницу.
    """
    base = keyboards.user_manage_kb(target, viewer)
    hidden = _ADMIN_LOCKED if _is_config_admin(target) else ()
    rows = [
        [_with_page(button, page) for button in row if _action_of(button) not in hidden]
        for row in base.inline_keyboard
    ]
    rows = [row for row in rows if row]
    rows.append([_staff_button("◀ К списку сотрудников", page)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _is_config_admin(target: User) -> bool:
    """Руководитель из ADMIN_IDS: понизить и заблокировать его нельзя (сервис всё равно откажет)."""
    return target.tg_id in get_settings().admin_ids


def _action_of(button: InlineKeyboardButton) -> str | None:
    """Действие UserCB кнопки или None (кнопка другого типа)."""
    data = button.callback_data
    if not data or not data.startswith(f"{UserCB.__prefix__}{UserCB.__separator__}"):
        return None
    return UserCB.unpack(data).action


def _with_page(button: InlineKeyboardButton, page: int) -> InlineKeyboardButton:
    """Кнопка действия (approve…role_emp) с номером страницы; остальные (например, «📊 Карточка») — как есть."""
    if _action_of(button) not in ACTIONS:
        return button
    cb = UserCB.unpack(button.callback_data or "")
    return button.model_copy(update={"callback_data": cb.model_copy(update={"page": page}).pack()})


async def _show_card(
    callback: CallbackQuery, target: User, viewer: User, page: int, note: str | None = None
) -> None:
    await common.edit_or_answer(callback, _card_text(target, viewer, note), _card_kb(target, viewer, page))


# --- Хендлеры -------------------------------------------------------------------------------


@router.message(F.text == BTN_STAFF, IsManager())
@router.message(Command("staff"), IsManager())
async def staff_menu(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    text, kb = _staff_screen(await users_svc.list_all(session), 0)
    await common.edit_or_answer(message, text, kb)


@router.callback_query(UserCB.filter(F.action == ACT_STAFF))
async def staff_page(
    callback: CallbackQuery, callback_data: UserCB, session: AsyncSession, user: User | None
) -> None:
    if not common.is_manager(user):
        await common.deny(callback)
        return
    await callback.answer()
    text, kb = _staff_screen(await users_svc.list_all(session), callback_data.page)
    await common.edit_or_answer(callback, text, kb)


@router.callback_query(UserCB.filter(F.action == ACT_MANAGE))
async def manage_user(
    callback: CallbackQuery, callback_data: UserCB, session: AsyncSession, user: User | None
) -> None:
    if user is None or not common.is_manager(user):
        await common.deny(callback)
        return
    target = await users_svc.get_user(session, callback_data.user_id)
    if target is None:
        await callback.answer(USER_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await _show_card(callback, target, user, callback_data.page)


@router.callback_query(UserCB.filter(F.action.in_(ACTIONS)))
async def user_action(
    callback: CallbackQuery,
    callback_data: UserCB,
    session: AsyncSession,
    user: User | None,
    bot: Bot,
    fsm_storage: BaseStorage | None = None,
) -> None:
    """approve / reject / block / unblock / role_mgr / role_emp — из карточки и из уведомления о заявке."""
    if user is None or not common.is_manager(user):
        await common.deny(callback)
        return
    action = callback_data.action
    page = callback_data.page
    # Чтение статуса, проверка и изменение — под замком (см. _action_lock): так второй
    # руководитель читает статус уже после коммита первого.
    async with _action_lock():
        # Права руководителя тоже перечитываем под замком: пока ждали, его могли понизить
        # или заблокировать (UserMiddleware загрузил его до этого).
        await session.refresh(user)
        if not common.is_manager(user):
            await common.deny(callback)
            return
        target = await users_svc.get_user(session, callback_data.user_id)
        if target is None:
            await callback.answer(USER_NOT_FOUND, show_alert=True)
            return

        # Заявку уже решил другой руководитель (или эта же кнопка нажата повторно).
        if action in DECISIONS and target.status != UserStatus.PENDING:
            await callback.answer(ALREADY_PROCESSED, show_alert=True)
            await _show_card(
                callback, target, user, page,
                note=f"ℹ️ {ALREADY_PROCESSED}. Текущий статус: {STATUS_LABELS.get(target.status, esc(target.status))}.",
            )
            return

        try:
            target = await _apply(session, action, target.id, user)
        except DomainError as exc:
            # Сервисы проверяют всё до изменений, так что откатывать нечего:
            # показываем причину (себя, последнего руководителя и т. п.) и актуальную карточку.
            await callback.answer(exc.message, show_alert=True)
            await _show_card(callback, target, user, page)
            return

        await session.commit()

    toast, note = _RESULTS[action]
    await callback.answer(toast)
    # Сначала уведомить человека: в карточке руководителю — правда о доставке решения по заявке.
    dialog_dropped = action in RESET_DIALOG and await _reset_dialog(fsm_storage, bot, target)
    delivered = await _notify_target(bot, target, action, dialog_dropped)
    if action in _UNDELIVERED and not delivered:
        note = _UNDELIVERED[action]
    await _show_card(callback, target, user, page, note=note)


async def _reset_dialog(storage: BaseStorage | None, bot: Bot, target: User) -> bool:
    """Сбросить незаконченный диалог пользователя (поручение, сдача результата, постановка задачи…).

    После блокировки или смены роли начатый диалог ему уже не подходит: заблокированный не должен
    его продолжать, а бывший сотрудник/руководитель потерял бы введённое на последнем шаге из-за
    отказа сервиса. Возвращает True, если диалог был.
    """
    if storage is None:
        return False
    key = StorageKey(bot_id=bot.id, chat_id=target.tg_id, user_id=target.tg_id)
    try:
        had_dialog = await storage.get_state(key) is not None
        await storage.set_state(key, None)
        await storage.set_data(key, {})
    except Exception:  # noqa: BLE001 — хранилище FSM не должно ломать уже выполненное действие
        log.exception("Не удалось сбросить диалог пользователя %s", target.id)
        return False
    return had_dialog


async def _apply(session: AsyncSession, action: str, user_id: int, actor: User) -> User:
    if action == "approve":
        return await users_svc.approve_user(session, user_id, actor)
    if action == "reject":
        return await users_svc.reject_user(session, user_id, actor)
    if action == "block":
        return await users_svc.block_user(session, user_id, actor)
    if action == "unblock":
        return await users_svc.unblock_user(session, user_id, actor)
    if action == "role_mgr":
        return await users_svc.set_role(session, user_id, Role.MANAGER, actor)
    if action == "role_emp":
        return await users_svc.set_role(session, user_id, Role.EMPLOYEE, actor)
    raise DomainError("Неизвестное действие")


async def _notify_target(bot: Bot, target: User, action: str, dialog_dropped: bool = False) -> bool:
    """Сообщить пользователю о решении -> доставлено ли сообщение.

    Изменение уже сохранено — ошибки доставки только логируем.
    """
    dropped = f"\n\n{DIALOG_DROPPED}" if dialog_dropped else ""
    try:
        if action in DECISIONS:
            return await notify.notify_user_decision(bot, target, action == "approve")
        if action == "unblock":
            # Не «снова»: разблокировать могут и отклонённую заявку — доступа у человека ещё не было.
            sent = await notify.safe_send(
                bot,
                target.tg_id,
                "✅ Доступ к боту открыт. Воспользуйтесь меню 👇",
                reply_markup=keyboards.main_menu(target),
            )
            return sent is not None
        if action == "block":
            sent = await notify.safe_send(
                bot,
                target.tg_id,
                "🚫 Доступ к боту закрыт руководителем.",
                reply_markup=keyboards.main_menu(target),  # неактивному — убрать клавиатуру
            )
            return sent is not None
        if action in ("role_mgr", "role_emp") and target.status == UserStatus.ACTIVE:
            text = (
                "👔 Вам назначена роль <b>руководителя</b>: теперь можно ставить задачи, "
                "проверять результаты и смотреть отчёты. Меню обновлено 👇"
                if action == "role_mgr"
                else "👤 Ваша роль изменена на <b>«Сотрудник»</b>. Меню обновлено 👇"
            )
            sent = await notify.safe_send(bot, target.tg_id, text + dropped, reply_markup=keyboards.main_menu(target))
            return sent is not None
    except Exception:  # noqa: BLE001 — уведомление не должно ломать уже выполненное действие
        log.exception("Не удалось уведомить пользователя %s о действии %s", target.id, action)
    return False
