"""Пользователи и роли: регистрация, подтверждение заявок, блокировка, смена роли.

Признак «регистрация завершена» отдельной колонкой не хранится. Правило:
новый сотрудник создаётся с пустым ``full_name`` (``""``), ФИО заполняет
``complete_registration``. Поэтому заявка считается поданной, когда пользователь
в статусе PENDING и ``full_name`` не пустой (см. ``list_pending``).
Начальники из ``settings.admin_ids`` сразу получают имя из Telegram.

Админ бота — начальник со всеми правами, которого нельзя понизить или заблокировать: Telegram ID из
настройки ADMIN_IDS или пользователь с отметкой ``users.is_admin`` (``grant_admin`` — команда
``python -m bot.tools.admin``; так админа назначают без правки настроек хостинга). ``is_admin`` — проверка.

Смена статуса (подтвердить/отклонить заявку, заблокировать/разблокировать) — атомарный условный
UPDATE ``… WHERE id=? AND status=<ожидаемый>``: если два начальника решают одновременно,
сработает только первое решение, второй получит DomainError («Заявка уже обработана»).

PostgreSQL: строки обрезаются по длине колонок, NUL-символы убираются (bot.services.dbsafe),
id вне диапазона INTEGER дают «не найден», а не ошибку базы.
"""

from __future__ import annotations

import logging

from sqlalchemy import ColumnElement, func, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from bot.config import get_settings
from bot.db.models import Role, User, UserStatus
from bot.services.dbsafe import clean_text, clip, column_length, is_db_id
from bot.services.errors import DomainError

logger = logging.getLogger(__name__)

_MAX_NAME_LEN = 200  # длина колонок full_name и position
_MAX_USERNAME_LEN = column_length(User.username)
_STATUS_ORDER = {UserStatus.PENDING: 0, UserStatus.ACTIVE: 1, UserStatus.BLOCKED: 2}
_ROLE_ORDER = {Role.MANAGER: 0, Role.EMPLOYEE: 1}
_ALREADY_PROCESSED = "Заявка уже обработана"
_CHANGED_MEANWHILE = "Статус пользователя только что изменился — откройте карточку заново"
ADMIN_PROTECTED = "Это админ бота — понизить или заблокировать его нельзя"


def require_manager(actor: User | None) -> None:
    """Проверить, что действие выполняет активный начальник (используется и в tasks)."""
    if actor is None or not actor.is_manager:
        raise DomainError("Действие доступно только начальнику")


# --- Чтение ---------------------------------------------------------------------------------


async def get_by_tg(session: AsyncSession, tg_id: int) -> User | None:
    """Пользователь по Telegram ID. Уже загруженный в эту сессию (его читает UserMiddleware в начале
    каждого апдейта) возвращается без повторного запроса: обмен с облачной базой — 130–190 мс."""
    if not is_db_id(tg_id, big=True):
        return None
    cached = _loaded_by_tg(session, tg_id)
    if cached is not None:
        return cached
    return await session.scalar(select(User).where(User.tg_id == tg_id))


def _loaded_by_tg(session: AsyncSession, tg_id: int) -> User | None:
    """Пользователь с этим tg_id среди объектов сессии (identity map) — как его вернул бы запрос."""
    for obj in session.sync_session.identity_map.values():
        if not isinstance(obj, User):
            continue
        state = inspect(obj)
        if state.expired or state.deleted or state.was_deleted or "tg_id" in state.unloaded:
            continue
        if obj.tg_id == tg_id:
            return obj
    return None


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    if not is_db_id(user_id):  # подделанная кнопка: id не поместится в INTEGER
        return None
    return await session.get(User, user_id)


async def list_employees(session: AsyncSession) -> list[User]:
    """Активные сотрудники, по ФИО."""
    return await _list_sorted(session, User.status == UserStatus.ACTIVE, User.role == Role.EMPLOYEE)


async def list_managers(session: AsyncSession) -> list[User]:
    """Активные начальники, по ФИО."""
    return await _list_sorted(session, User.status == UserStatus.ACTIVE, User.role == Role.MANAGER)


async def list_pending(session: AsyncSession) -> list[User]:
    """Заявки на доступ: PENDING с заполненным ФИО (регистрация завершена), старые сверху."""
    stmt = (
        select(User)
        .where(User.status == UserStatus.PENDING, User.full_name != "")
        .order_by(User.created_at, User.id)
    )
    return list(await session.scalars(stmt))


async def list_all(session: AsyncSession) -> list[User]:
    """Все пользователи: сначала заявки, затем активные, затем заблокированные; начальники выше."""
    users = await session.scalars(select(User))
    return sorted(users, key=lambda u: (_STATUS_ORDER[u.status], _ROLE_ORDER[u.role], _name_key(u)))


# --- Регистрация ----------------------------------------------------------------------------


async def register_or_get(
    session: AsyncSession, tg_id: int, username: str | None, tg_full_name: str
) -> tuple[User, bool]:
    """Найти пользователя по tg_id или создать при первом /start. Возвращает (user, created).

    Новый пользователь — сотрудник в статусе PENDING с пустым full_name (ФИО он введёт сам).
    Админ (Telegram ID из settings.admin_ids или отметка users.is_admin) сразу (и при каждом повторном
    /start) становится активным начальником; пустое ФИО у него заполняется именем из Telegram.
    """
    user = await get_by_tg(session, tg_id)
    created = user is None
    username = clip(username, _MAX_USERNAME_LEN) or None
    if user is None:
        user = User(
            tg_id=tg_id,
            username=username,
            full_name="",
            role=Role.EMPLOYEE,
            status=UserStatus.PENDING,
        )
        session.add(user)
    else:
        user.username = username

    if is_admin(user):
        _promote_admin(user, tg_full_name)

    await session.flush()
    return user, created


async def complete_registration(
    session: AsyncSession, user: User, full_name: str, position: str | None
) -> User:
    """Сохранить ФИО и должность. Статус не меняется: заявка ждёт решения начальника."""
    if user.status == UserStatus.BLOCKED:
        raise DomainError("Доступ закрыт. Обратитесь к начальнику")
    name = _clean_name(full_name, "ФИО")
    if not name:
        raise DomainError("Укажите фамилию и имя")
    user.full_name = name
    user.position = _clean_name(position, "Должность") or None
    await session.flush()
    return user


# --- Админы ---------------------------------------------------------------------------------


def is_admin(user: User | None) -> bool:
    """Админ бота: отмечен в базе (users.is_admin) или его Telegram ID указан в настройке ADMIN_IDS."""
    return user is not None and (bool(user.is_admin) or user.tg_id in get_settings().admin_ids)


async def grant_admin(session: AsyncSession, tg_id: int) -> User:
    """Сделать человека с этим Telegram ID админом бота (отметка users.is_admin).

    Он уже зарегистрирован (есть ФИО) — сразу становится активным начальником. Ещё не писал боту или не
    закончил анкету — запись ждёт его /start: ``register_or_get`` выдаст права и возьмёт имя из Telegram
    (до тех пор его нет ни среди начальников, ни среди заявок).
    """
    if isinstance(tg_id, bool) or not isinstance(tg_id, int) or tg_id <= 0 or not is_db_id(tg_id, big=True):
        raise DomainError("Telegram ID — положительное число, например 123456789")
    user = await get_by_tg(session, tg_id)
    if user is None:
        user = User(tg_id=tg_id, username=None, full_name="", role=Role.EMPLOYEE, status=UserStatus.PENDING)
        session.add(user)
    user.is_admin = True
    if user.full_name:
        _promote_admin(user, user.full_name)
    await session.flush()
    return user


async def revoke_admin(session: AsyncSession, tg_id: int) -> User:
    """Снять отметку админа. Роль не меняется: человек остаётся обычным начальником."""
    user = await get_by_tg(session, tg_id)
    if user is None or not user.is_admin:
        raise DomainError("Среди админов бота такого Telegram ID нет")
    user.is_admin = False
    await session.flush()
    return user


async def list_admins(session: AsyncSession) -> list[User]:
    """Админы, отмеченные в базе (без тех, кто указан только в настройке ADMIN_IDS), по ФИО."""
    return await _list_sorted(session, User.is_admin.is_(True))


# --- Решения начальника -------------------------------------------------------------------


async def approve_user(session: AsyncSession, user_id: int, actor: User) -> User:
    """Подтвердить заявку: PENDING -> ACTIVE (роль не меняется)."""
    require_manager(actor)
    target = await _target_or_error(session, user_id)
    _ensure_pending(target)
    if not target.full_name:
        raise DomainError("Пользователь ещё не указал ФИО — подтвердить заявку нельзя")
    if not await _set_status(session, target, UserStatus.PENDING, UserStatus.ACTIVE, actor):
        raise DomainError(_ALREADY_PROCESSED)
    return target


async def reject_user(session: AsyncSession, user_id: int, actor: User) -> User:
    """Отклонить заявку: PENDING -> BLOCKED."""
    require_manager(actor)
    target = await _target_or_error(session, user_id)
    _ensure_pending(target)
    if not await _set_status(session, target, UserStatus.PENDING, UserStatus.BLOCKED, actor):
        raise DomainError(_ALREADY_PROCESSED)
    return target


async def block_user(session: AsyncSession, user_id: int, actor: User) -> User:
    """Заблокировать пользователя. Нельзя себя, админа и последнего начальника."""
    require_manager(actor)
    target = await _target_or_error(session, user_id)
    if target.id == actor.id:
        raise DomainError("Нельзя заблокировать самого себя")
    _ensure_not_blocked(target)
    _protect_admin(target)
    await _ensure_not_last_manager(session, target)
    if not await _set_status(session, target, target.status, UserStatus.BLOCKED, actor):
        _ensure_not_blocked(target)  # уже перечитан: заблокировал другой начальник
        raise DomainError(_CHANGED_MEANWHILE)
    return target


async def unblock_user(session: AsyncSession, user_id: int, actor: User) -> User:
    """Разблокировать: BLOCKED -> ACTIVE."""
    require_manager(actor)
    target = await _target_or_error(session, user_id)
    _ensure_blocked(target)
    if not await _set_status(session, target, UserStatus.BLOCKED, UserStatus.ACTIVE, actor):
        _ensure_blocked(target)  # уже перечитан: разблокировал другой начальник
        raise DomainError(_CHANGED_MEANWHILE)
    return target


async def set_role(session: AsyncSession, user_id: int, role: Role, actor: User) -> User:
    """Сменить роль. Нельзя понизить себя, админа и последнего начальника."""
    require_manager(actor)
    role = Role(role)
    target = await _target_or_error(session, user_id)
    if target.role == role:
        raise DomainError("У пользователя уже эта роль")
    if role == Role.EMPLOYEE:
        if target.id == actor.id:
            raise DomainError("Нельзя понизить самого себя")
        _protect_admin(target)
        await _ensure_not_last_manager(session, target)
    target.role = role
    await session.flush()
    logger.info("Роль пользователя %s изменена на %s (начальник %s)", target.id, role.value, actor.id)
    return target


# --- Приватные хелперы ----------------------------------------------------------------------


def _promote_admin(user: User, tg_full_name: str) -> None:
    """Сделать админа активным начальником."""
    if not user.is_manager:
        logger.info("Админ (Telegram ID %s) назначен начальником", user.tg_id)
    user.role = Role.MANAGER
    user.status = UserStatus.ACTIVE
    if not user.full_name:
        name = " ".join(clean_text(tg_full_name or "").split())[:_MAX_NAME_LEN]
        user.full_name = name or f"Начальник {user.tg_id}"


def _clean_name(value: str | None, label: str) -> str:
    """Схлопнуть пробелы; слишком длинное значение — ошибка."""
    cleaned = " ".join(clean_text(value or "").split())
    if len(cleaned) > _MAX_NAME_LEN:
        raise DomainError(f"{label}: слишком длинное значение (до {_MAX_NAME_LEN} символов)")
    return cleaned


def _name_key(user: User) -> str:
    """Ключ сортировки по ФИО без учёта регистра (ё = е)."""
    return user.full_name.casefold().replace("ё", "е")


async def _list_sorted(session: AsyncSession, *conditions: ColumnElement[bool]) -> list[User]:
    users = await session.scalars(select(User).where(*conditions))
    return sorted(users, key=_name_key)


async def _target_or_error(session: AsyncSession, user_id: int) -> User:
    target = await get_user(session, user_id)
    if target is None:
        raise DomainError("Пользователь не найден")
    return target


def _ensure_pending(target: User) -> None:
    if target.status != UserStatus.PENDING:
        raise DomainError(_ALREADY_PROCESSED)


def _ensure_not_blocked(target: User) -> None:
    if target.status == UserStatus.BLOCKED:
        raise DomainError("Пользователь уже заблокирован")


def _ensure_blocked(target: User) -> None:
    if target.status != UserStatus.BLOCKED:
        raise DomainError("Пользователь не заблокирован")


def _protect_admin(target: User) -> None:
    """Админа нельзя заблокировать или понизить: /start всё равно вернёт ему права."""
    if is_admin(target):
        raise DomainError(ADMIN_PROTECTED)


async def _ensure_not_last_manager(session: AsyncSession, target: User) -> None:
    """Не дать оставить систему без активного начальника."""
    if not target.is_manager:
        return
    managers = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == Role.MANAGER, User.status == UserStatus.ACTIVE)
    )
    if (managers or 0) <= 1:
        raise DomainError("Это последний активный начальник — сначала назначьте другого")


async def _set_status(
    session: AsyncSession, target: User, expected: UserStatus, status: UserStatus, actor: User
) -> bool:
    """Атомарно сменить статус: ``UPDATE users SET status=… WHERE id=? AND status=expected``.

    False — статус уже изменили в другой сессии (решение другого начальника): объект target
    перечитан из БД, вызывающий код объясняет отказ по актуальному статусу.
    """
    result = await session.execute(
        update(User)
        .where(User.id == target.id, User.status == expected)
        .values(status=status)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.execute(
            select(User).where(User.id == target.id).execution_options(populate_existing=True)
        )
        return False
    set_committed_value(target, "status", status)
    logger.info("Статус пользователя %s: %s (начальник %s)", target.id, status.value, actor.id)
    return True
