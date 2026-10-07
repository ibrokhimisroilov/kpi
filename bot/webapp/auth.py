"""Вход в приложение по подписанной Telegram строке initData (docs/MINIAPP_SPEC.md §5).

Telegram передаёт Mini App строку ``Telegram.WebApp.initData``, подписанную токеном бота
(«Validating data received via the Mini App»). Приложение шлёт её в заголовке ``X-Telegram-Init-Data``
с каждым запросом, сервер проверяет подпись каждый раз — без cookie и серверных сессий.

* подпись: ``secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)``,
  ``hash = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))``, где data_check_string — ВСЕ поля,
  кроме ``hash``, отсортированные по имени, в виде ``key=value`` через «\\n» (поле ``signature`` из
  Bot API 8.0 входит в строку);
* сравнение — за постоянное время; любая испорченная строка — ``AuthError`` (никогда не 500);
* ``auth_date`` старше суток — «сессия устарела», из будущего больше чем на 5 минут — подделка.

Строка initData и hash никогда не пишутся в журнал.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, urlencode

from bot.services.dbsafe import is_db_id

__all__ = [
    "AUTH_MESSAGES",
    "FUTURE_SKEW_SEC",
    "INIT_DATA_HEADER",
    "MAX_AGE_SEC",
    "MAX_INIT_DATA_LEN",
    "AuthError",
    "InitData",
    "sign_init_data",
    "validate_init_data",
]

INIT_DATA_HEADER = "X-Telegram-Init-Data"
MAX_AGE_SEC = 24 * 60 * 60  # initData старше суток — «сессия устарела»
FUTURE_SKEW_SEC = 5 * 60  # auth_date из будущего больше чем на 5 мин — подделка
MAX_INIT_DATA_LEN = 8192

AuthCode = Literal["auth_missing", "auth_invalid", "auth_expired"]

AUTH_MESSAGES: dict[str, str] = {
    "auth_missing": "Откройте приложение из Telegram — кнопка «Открыть» в чате с ботом.",
    "auth_invalid": (
        "Не удалось подтвердить вход через Telegram. Закройте приложение и откройте его снова из чата с ботом."
    ),
    "auth_expired": "Сессия устарела. Закройте приложение и откройте его снова из чата с ботом.",
}

_AUTH_DATE_RE = re.compile(r"\d{1,12}")


class AuthError(Exception):
    """Вход не подтверждён: ``code`` — auth_missing | auth_invalid | auth_expired, ``message`` — текст для
    пользователя (§6.2). Причину подробнее не сообщаем ни пользователю, ни журналу."""

    def __init__(self, code: AuthCode) -> None:
        super().__init__(code)
        self.code: AuthCode = code
        self.message = AUTH_MESSAGES[code]


@dataclass(frozen=True)
class InitData:
    """Проверенные данные входа. Доверять им можно только для поиска пользователя по tg_id."""

    tg_id: int
    first_name: str
    last_name: str | None
    username: str | None
    language_code: str | None
    auth_date: int  # unix-время, с
    query_id: str | None
    start_param: str | None


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def _check_string(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(f"{key}={value}" for key, value in sorted(pairs))


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def validate_init_data(
    init_data: str, bot_token: str, *, now: float | None = None, max_age: int = MAX_AGE_SEC
) -> InitData:
    """Проверить строку initData. Неверная — ``AuthError`` (других исключений не бросает)."""
    if not isinstance(init_data, str):
        raise AuthError("auth_invalid")
    if not init_data.strip():
        raise AuthError("auth_missing")
    if len(init_data) > MAX_INIT_DATA_LEN or not isinstance(bot_token, str) or not bot_token:
        raise AuthError("auth_invalid")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise AuthError("auth_invalid") from None
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:  # повтор ключа — подделка или мусор
            raise AuthError("auth_invalid")
        fields[key] = value
    given = fields.pop("hash", None)
    if given is None:
        raise AuthError("auth_invalid")

    try:
        message = _check_string(list(fields.items())).encode("utf-8")
        given_bytes = given.encode("ascii")
    except UnicodeError:  # суррогаты из заголовка, не-ASCII в hash
        raise AuthError("auth_invalid") from None
    expected = hmac.new(_secret_key(bot_token), message, hashlib.sha256).hexdigest().encode("ascii")
    if not hmac.compare_digest(expected, given_bytes):
        raise AuthError("auth_invalid")

    raw_date = fields.get("auth_date", "")
    if not _AUTH_DATE_RE.fullmatch(raw_date):
        raise AuthError("auth_invalid")
    auth_date = int(raw_date)
    current = time.time() if now is None else now
    if current - auth_date > max_age:
        raise AuthError("auth_expired")
    if auth_date - current > FUTURE_SKEW_SEC:
        raise AuthError("auth_invalid")

    user = _parse_user(fields.get("user"))
    tg_id = user.get("id")
    first_name = user.get("first_name", "")
    if (
        isinstance(tg_id, bool)
        or not isinstance(tg_id, int)
        or tg_id <= 0
        or not is_db_id(tg_id, big=True)
        or not isinstance(first_name, str)
    ):
        raise AuthError("auth_invalid")
    return InitData(
        tg_id=tg_id,
        first_name=first_name,
        last_name=_optional_str(user.get("last_name")),
        username=_optional_str(user.get("username")),
        language_code=_optional_str(user.get("language_code")),
        auth_date=auth_date,
        query_id=_optional_str(fields.get("query_id")),
        start_param=_optional_str(fields.get("start_param")),
    )


def _parse_user(raw: str | None) -> dict[str, Any]:
    if raw is None:
        raise AuthError("auth_invalid")
    try:
        user = json.loads(raw)
    except (ValueError, RecursionError):
        raise AuthError("auth_invalid") from None
    if not isinstance(user, dict):
        raise AuthError("auth_invalid")
    return user


def sign_init_data(
    bot_token: str,
    *,
    tg_id: int,
    first_name: str = "Тест",
    last_name: str | None = None,
    username: str | None = None,
    auth_date: int | None = None,
    extra: Mapping[str, str] | None = None,
) -> str:
    """Подписанная строка initData, как её даёт Telegram (user — JSON, auth_date, query_id, hash).

    Для тестов и локального dev-сервера: подписывается тем же токеном, что проверяет сервер.
    ``extra`` — дополнительные поля (или замена стандартных), они тоже входят в подпись.
    """
    user: dict[str, Any] = {"id": tg_id, "first_name": first_name}
    if last_name:
        user["last_name"] = last_name
    if username:
        user["username"] = username
    user["language_code"] = "ru"
    fields: dict[str, str] = {
        "query_id": "AA" + secrets.token_hex(8),
        "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
    }
    fields.update(extra or {})
    fields.pop("hash", None)
    digest = hmac.new(
        _secret_key(bot_token), _check_string(list(fields.items())).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return urlencode([*fields.items(), ("hash", digest)], quote_via=quote)
