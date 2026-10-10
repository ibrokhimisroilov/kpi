"""Админ бота: начальник со всеми правами, которого нельзя понизить или заблокировать.

Админы хранятся в базе (``users.is_admin``) — назначить можно без правки настроек хостинга
(``python -m bot.tools.admin grant <Telegram ID>``); ADMIN_IDS из настроек действует по-прежнему.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from bot.db.base import init_db, make_engine, make_sessionmaker
from bot.db.models import Role, User, UserStatus
from bot.services import users
from bot.services.errors import DomainError
from bot.tools import admin as admin_tool

ADMIN_TG = 7001


async def test_grant_admin_promotes_existing_employee(session, manager: User, user_factory) -> None:
    person = await user_factory(ADMIN_TG, "Каримов Карим")
    assert not users.is_admin(person)

    granted = await users.grant_admin(session, ADMIN_TG)

    assert granted is person
    assert person.is_admin and users.is_admin(person)
    assert person.is_manager
    assert person.full_name == "Каримов Карим"


async def test_db_admin_cannot_be_demoted_or_blocked(session, manager: User, user_factory) -> None:
    person = await user_factory(ADMIN_TG, "Каримов Карим")
    await users.grant_admin(session, ADMIN_TG)

    with pytest.raises(DomainError, match="админ"):
        await users.set_role(session, person.id, Role.EMPLOYEE, manager)
    with pytest.raises(DomainError, match="админ"):
        await users.block_user(session, person.id, manager)
    assert person.is_manager


async def test_db_admin_regains_rights_on_start(session, user_factory) -> None:
    person = await user_factory(ADMIN_TG, "Каримов Карим")
    await users.grant_admin(session, ADMIN_TG)
    person.role, person.status = Role.EMPLOYEE, UserStatus.BLOCKED

    again, created = await users.register_or_get(session, ADMIN_TG, "karim", "Каримов Карим")

    assert again is person and not created
    assert again.is_manager


async def test_grant_admin_before_first_start(session, manager: User) -> None:
    """Человек ещё не писал боту: запись ждёт его первого /start и в списках не видна как начальник."""
    person = await users.grant_admin(session, ADMIN_TG)

    assert person.is_admin and not person.is_manager
    assert person not in await users.list_managers(session)
    assert await users.list_pending(session) == []

    started, created = await users.register_or_get(session, ADMIN_TG, "karim", " Каримов  Карим ")
    assert started is person and not created
    assert started.is_manager
    assert started.full_name == "Каримов Карим"


async def test_revoke_admin_keeps_manager_role(session, manager: User, user_factory) -> None:
    person = await user_factory(ADMIN_TG, "Каримов Карим")
    await users.grant_admin(session, ADMIN_TG)

    await users.revoke_admin(session, ADMIN_TG)

    assert not person.is_admin and person.is_manager
    await users.set_role(session, person.id, Role.EMPLOYEE, manager)  # теперь обычный начальник
    assert person.role == Role.EMPLOYEE


async def test_config_admin_is_admin_without_flag(session, manager: User) -> None:
    assert not manager.is_admin  # начальник из ADMIN_IDS (tg 1001) — флаг в базе не нужен
    assert users.is_admin(manager)


async def test_init_db_adds_new_columns_to_old_database() -> None:
    """База, созданная прежней версией бота (без users.is_admin): init_db добавляет колонку, строки целы."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, tg_id BIGINT NOT NULL, username VARCHAR(64), "
                "full_name VARCHAR(200) NOT NULL, position VARCHAR(200), role VARCHAR(20) NOT NULL, "
                "status VARCHAR(20) NOT NULL, created_at DATETIME NOT NULL)"
            ))
            await conn.execute(text(
                "INSERT INTO users (id, tg_id, full_name, role, status, created_at) "
                "VALUES (1, 2001, 'Иванов Иван', 'employee', 'active', '2026-10-01 10:00:00')"
            ))

        await init_db(engine)
        await init_db(engine)  # повторный запуск ничего не ломает

        async with make_sessionmaker(engine)() as session:
            user = await users.get_by_tg(session, 2001)
            assert user is not None and user.full_name == "Иванов Иван"
            assert user.is_admin is False
    finally:
        await engine.dispose()


async def test_admin_tool_grant_and_revoke(engine) -> None:
    async with make_sessionmaker(engine)() as session:
        session.add(User(tg_id=ADMIN_TG, full_name="Каримов Карим", role=Role.EMPLOYEE, status=UserStatus.ACTIVE))
        await session.commit()

    message = await admin_tool.run(engine, "grant", ADMIN_TG)
    assert "Каримов Карим" in message and "админ" in message.lower()
    async with make_sessionmaker(engine)() as session:
        user = await users.get_by_tg(session, ADMIN_TG)
        assert user is not None and user.is_admin and user.is_manager

    await admin_tool.run(engine, "revoke", ADMIN_TG)
    async with make_sessionmaker(engine)() as session:
        user = await users.get_by_tg(session, ADMIN_TG)
        assert user is not None and not user.is_admin
