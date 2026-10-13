"""Вход на сайт через подтверждение в Telegram и сессии браузера (docs/MINIAPP_SPEC.md §15).

Как проходит вход:

1. сайт начинает попытку (``start``): случайный **код ссылки** (уходит в ``/start login_<код>`` бота) и
   случайный **секрет браузера** (остаётся в cookie браузера, недоступной скриптам), число для сверки;
2. человек открывает ссылку в боте (``open_in_bot``) — попытка привязывается к его Telegram ID, бот показывает
   три числа; верное число подтверждает вход, любое другое и «Это не я» — отменяют (``decide``);
3. браузер, начавший вход, забирает сессию (``collect``) — один раз и только по секрету из своей cookie.

Правила: попытка живёт ``ATTEMPT_TTL`` и срабатывает один раз; подтвердить её может только тот, кто открыл
ссылку; секреты в базе не хранятся (только SHA-256); каждое изменение состояния — условный UPDATE
(``WHERE state = …``), поэтому два одновременных запроса (и два экземпляра бота при обновлении на
хостинге) не выдадут две сессии. «Сейчас» передаёт вызывающий код — сервис сам время не читает.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User, UserStatus, WebLogin, WebLoginState, WebSession

__all__ = [
    "ATTEMPT_TTL",
    "SESSION_TTL",
    "START_PREFIX",
    "Collected",
    "Started",
    "cleanup",
    "collect",
    "csrf_for",
    "cancel",
    "decide",
    "end_all",
    "end_session",
    "open_in_bot",
    "pending_count",
    "refuse",
    "session_user",
    "start",
]

log = logging.getLogger(__name__)

ATTEMPT_TTL = timedelta(minutes=5)      # столько есть на подтверждение в Telegram
SESSION_TTL = timedelta(days=30)        # сессия без обращений к сайту
SESSION_MAX_AGE = timedelta(days=90)    # и не дольше этого с момента входа, как бы часто ей ни пользовались
SESSION_TOUCH = timedelta(days=1)       # продлевать не чаще (лишняя запись в базу на каждый запрос не нужна)
KEEP_EXPIRED = timedelta(hours=1)       # истёкшие попытки убираются не сразу: сайт ещё покажет «время вышло»
START_PREFIX = "login_"                 # /start login_<код>
OPTIONS = 3                             # чисел на кнопках бота
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_random = secrets.SystemRandom()


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def csrf_for(session_token: str) -> str:
    """Знак страницы для защиты от запросов с чужих сайтов: выводится из знака сессии, в базе не хранится.
    Страница читает его из своей cookie и шлёт в заголовке; чужой сайт прочитать cookie не может."""
    return hmac.new(session_token.encode("utf-8"), b"kpi-site-csrf", hashlib.sha256).hexdigest()


# --- Сайт начинает вход ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Started:
    token: str           # код ссылки: https://t.me/<бот>?start=login_<token>
    browser_secret: str  # секрет для cookie браузера
    code: str            # число, которое сайт показывает человеку
    expires_at: datetime


async def start(session: AsyncSession, *, device: str | None, now: datetime) -> Started:
    token = secrets.token_urlsafe(16)
    browser_secret = secrets.token_urlsafe(32)
    numbers = _random.sample(range(10, 100), OPTIONS)
    code = str(numbers[0])
    expires_at = now + ATTEMPT_TTL
    session.add(
        WebLogin(
            token_hash=_digest(token),
            browser_hash=_digest(browser_secret),
            code=code,
            options=",".join(str(n) for n in sorted(numbers)),  # по возрастанию: место кнопки ничего не подсказывает
            state=WebLoginState.NEW,
            device=(device or None),
            created_at=now,
            expires_at=expires_at,
        )
    )
    await session.flush()
    return Started(token=token, browser_secret=browser_secret, code=code, expires_at=expires_at)


_WAITING = (WebLoginState.NEW, WebLoginState.OPENED)


async def pending_count(session: AsyncSession, now: datetime) -> int:
    """Сколько попыток входа сейчас ждут подтверждения (общий потолок — защита базы от потока запросов).
    Решённые и использованные попытки в счёт не идут."""
    stmt = (
        select(func.count())
        .select_from(WebLogin)
        .where(WebLogin.expires_at > now, WebLogin.state.in_(_WAITING))
    )
    return int(await session.scalar(stmt) or 0)


async def cleanup(session: AsyncSession, now: datetime) -> None:
    """Убрать истёкшие попытки и сессии (вызывается при каждом новом входе — отдельного задания нет)."""
    await session.execute(delete(WebLogin).where(WebLogin.expires_at < now - KEEP_EXPIRED))
    await session.execute(delete(WebSession).where(WebSession.expires_at < now))


# --- Бот: человек открыл ссылку и нажал число ------------------------------------------------------------


async def _by_token(session: AsyncSession, token: str) -> WebLogin | None:
    if not _SECRET_RE.fullmatch(token or ""):
        return None
    return await session.scalar(select(WebLogin).where(WebLogin.token_hash == _digest(token)))


def _alive(attempt: WebLogin | None, now: datetime) -> bool:
    return attempt is not None and attempt.expires_at > now


async def _move(session: AsyncSession, attempt: WebLogin, expected: tuple[WebLoginState, ...], **values: Any) -> bool:
    """Условный переход состояния: True — эта транзакция его выполнила."""
    result = await session.execute(
        update(WebLogin)
        .where(WebLogin.id == attempt.id, WebLogin.state.in_(expected))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.refresh(attempt)
    return result.rowcount == 1  # type: ignore[attr-defined]


async def open_in_bot(session: AsyncSession, token: str, tg_id: int, now: datetime) -> tuple[str, WebLogin | None]:
    """Человек открыл ``/start login_<token>``. Ответ: ("ok", попытка) — показать числа;
    ("foreign", None) — ссылку уже открыл другой человек; ("gone", None) — ссылки нет, она истекла или уже
    использована."""
    attempt = await _by_token(session, token)
    if attempt is None or not _alive(attempt, now) or attempt.state not in (WebLoginState.NEW, WebLoginState.OPENED):
        return "gone", None
    if attempt.state == WebLoginState.NEW:
        await _move(session, attempt, (WebLoginState.NEW,), state=WebLoginState.OPENED, tg_id=tg_id)
    if attempt.state != WebLoginState.OPENED:
        return "gone", None
    return ("ok", attempt) if attempt.tg_id == tg_id else ("foreign", None)


async def refuse(session: AsyncSession, token: str, now: datetime) -> None:
    """Ссылку открыл человек без доступа (не зарегистрирован, заявка, заблокирован): попытка закрывается,
    сайт сразу показывает отказ, а не ждёт до истечения срока."""
    attempt = await _by_token(session, token)
    if attempt is not None and _alive(attempt, now):
        await _move(session, attempt, (WebLoginState.NEW,), state=WebLoginState.DENIED)


async def decide(session: AsyncSession, attempt_id: int, user: User, pick: str | None, now: datetime) -> str:
    """Нажата кнопка под сообщением бота: ``pick`` — число или None («Это не я»).
    Ответ: "approved" | "wrong" (число не то — вход отменён) | "denied" | "gone" (не его попытка, истекла,
    уже решена)."""
    attempt = await session.get(WebLogin, attempt_id)
    if (
        attempt is None
        or not _alive(attempt, now)
        or attempt.state != WebLoginState.OPENED
        or attempt.tg_id != user.tg_id
    ):
        return "gone"
    # Сравнение — по байтам: в подделанной кнопке может быть что угодно, а не число.
    approved = (
        pick is not None
        and user.is_active
        and hmac.compare_digest(pick.encode("utf-8"), attempt.code.encode("utf-8"))
    )
    values: dict[str, Any] = {"state": WebLoginState.APPROVED if approved else WebLoginState.DENIED}
    if approved:
        values["user_id"] = user.id
    if not await _move(session, attempt, (WebLoginState.OPENED,), **values):
        return "gone"
    if approved:
        return "approved"
    return "denied" if pick is None else "wrong"


# --- Сайт забирает сессию ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Collected:
    state: str                      # none | waiting | denied | expired | ok
    opened: bool = False            # ссылку уже открыли в боте (ждём нажатия числа)
    expires_in: int = 0             # секунд до конца попытки
    code: str | None = None         # число для сверки (страницу могли перезагрузить — показываем его снова)
    session_token: str | None = None


async def _by_browser(session: AsyncSession, browser_secret: str) -> WebLogin | None:
    if not _SECRET_RE.fullmatch(browser_secret or ""):
        return None
    return await session.scalar(select(WebLogin).where(WebLogin.browser_hash == _digest(browser_secret)))


async def cancel(session: AsyncSession, browser_secret: str, now: datetime) -> None:
    """Браузер отказался от своей попытки («Отмена» или вход начат заново): она закрывается, и кнопки под
    сообщением бота больше ничего не подтверждают — человек не подтвердит вход, который никто не получит."""
    attempt = await _by_browser(session, browser_secret)
    if attempt is not None and _alive(attempt, now):
        await _move(session, attempt, _WAITING, state=WebLoginState.DENIED)


async def collect(session: AsyncSession, browser_secret: str, *, now: datetime) -> Collected:
    """Состояние попытки для браузера, который её начал; подтверждённая попытка превращается в сессию —
    один раз. Секрет чужой или неизвестный — «expired» (тот же ответ, что у истёкшей: гадать по ответу
    нельзя)."""
    attempt = await _by_browser(session, browser_secret)
    if attempt is None:
        return Collected("expired")
    if attempt.state == WebLoginState.DENIED:
        return Collected("denied")
    if not _alive(attempt, now) or attempt.state == WebLoginState.USED:
        return Collected("expired")
    if attempt.state in (WebLoginState.NEW, WebLoginState.OPENED):
        left = max(1, int((attempt.expires_at - now).total_seconds()))
        return Collected("waiting", opened=attempt.state == WebLoginState.OPENED, expires_in=left, code=attempt.code)
    user_id, device = attempt.user_id, attempt.device
    if user_id is None or not await _move(session, attempt, (WebLoginState.APPROVED,), state=WebLoginState.USED):
        return Collected("expired")
    token = secrets.token_urlsafe(32)
    session.add(
        WebSession(
            token_hash=_digest(token),
            user_id=user_id,
            device=device,
            created_at=now,
            last_seen_at=now,
            expires_at=now + SESSION_TTL,
        )
    )
    await session.flush()
    return Collected("ok", session_token=token)


# --- Сессия -----------------------------------------------------------------------------------------------


async def session_user(session: AsyncSession, token: str, now: datetime) -> User | None:
    """Пользователь по знаку сессии из cookie; None — сессии нет или она истекла. Один запрос к базе
    (сессия и пользователь — одним SELECT), как вход Mini App; оборванное соединение пула — один повтор
    (как ``bot.middlewares.load_user``: это первый запрос, в сессии ещё ничего не сделано).

    Статус и роль пользователя здесь не проверяются — это делает API на каждом запросе (как для Mini App):
    заблокированный получает отказ сразу, а его сессия заодно удаляется.
    """
    if not _SECRET_RE.fullmatch(token or ""):
        return None
    stmt = select(WebSession).where(WebSession.token_hash == _digest(token))
    try:
        row = await session.scalar(stmt)
    except DBAPIError as exc:
        if not exc.connection_invalidated:
            raise
        log.info("Соединение с базой оборвалось (%s) — повторяю запрос на новом", type(exc.orig).__name__)
        await session.rollback()
        row = await session.scalar(stmt)
    if row is None or row.expires_at <= now or now - row.created_at >= SESSION_MAX_AGE:
        return None
    user = row.user
    if user.status == UserStatus.BLOCKED:
        await session.delete(row)
    elif now - row.last_seen_at >= SESSION_TOUCH:
        row.last_seen_at = now
        row.expires_at = min(now + SESSION_TTL, row.created_at + SESSION_MAX_AGE)
    return user


async def end_session(session: AsyncSession, token: str) -> None:
    """Выход: сессия удаляется — cookie с этим знаком больше ничего не открывает."""
    if _SECRET_RE.fullmatch(token or ""):
        await session.execute(delete(WebSession).where(WebSession.token_hash == _digest(token)))


async def end_all(session: AsyncSession, user_id: int) -> None:
    """Закрыть все сессии человека на сайте: кнопка «выйти везде» в боте и блокировка сотрудника."""
    await session.execute(delete(WebSession).where(WebSession.user_id == user_id))
