"""Вход по подписанному initData (docs/MINIAPP_SPEC.md §5, §12.2 test_auth): векторы подписи, сроки, мусор."""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import parse_qsl, quote, urlencode

import pytest
from aiogram.utils.web_app import check_webapp_signature

from bot.webapp import auth
from bot.webapp.auth import (
    FUTURE_SKEW_SEC,
    INIT_DATA_HEADER,
    MAX_AGE_SEC,
    MAX_INIT_DATA_LEN,
    AuthError,
    sign_init_data,
    validate_init_data,
)

from .conftest import MGR, TOKEN, MiniApp

NOW = 1_790_000_000.0


def _signed(**kwargs: object) -> str:
    kwargs.setdefault("auth_date", int(NOW))
    return sign_init_data(TOKEN, tg_id=kwargs.pop("tg_id", 1001), **kwargs)  # type: ignore[arg-type]


def _resign(fields: list[tuple[str, str]], token: str = TOKEN) -> str:
    """Подписать произвольный набор полей (как Telegram) — для векторов с испорченным содержимым."""
    import hashlib
    import hmac

    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields))
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode([*fields, ("hash", digest)], quote_via=quote)


def _code(init_data: object, token: str = TOKEN, **kwargs: object) -> str:
    with pytest.raises(AuthError) as info:
        validate_init_data(init_data, token, now=kwargs.pop("now", NOW), **kwargs)  # type: ignore[arg-type]
    return info.value.code


# --- Функция validate_init_data ----------------------------------------------------------------------


def test_valid_init_data_gives_user_fields() -> None:
    data = _signed(tg_id=2001, first_name="Иван", last_name="Иванов", username="ivan", extra={"start_param": "x"})
    init = validate_init_data(data, TOKEN, now=NOW)
    assert (init.tg_id, init.first_name, init.last_name, init.username) == (2001, "Иван", "Иванов", "ivan")
    assert init.auth_date == int(NOW) and init.start_param == "x" and init.query_id
    assert init.language_code == "ru"


def test_missing_and_empty() -> None:
    assert _code("") == "auth_missing"
    assert _code("   ") == "auth_missing"
    assert _code(None) == "auth_invalid"
    assert _code(b"abc") == "auth_invalid"


def test_changed_hash_or_field_or_token_is_invalid() -> None:
    data = _signed()
    pairs = parse_qsl(data)
    good_hash = dict(pairs)["hash"]
    flipped = good_hash[:-1] + ("0" if good_hash[-1] != "0" else "1")
    assert _code(data.replace(good_hash, flipped)) == "auth_invalid"
    # user.id подменён, hash старый
    changed = [(k, v.replace('"id":1001', '"id":1002') if k == "user" else v) for k, v in pairs]
    assert _code(urlencode(changed, quote_via=quote)) == "auth_invalid"
    # подписано другим токеном
    assert _code(sign_init_data("43:OTHER", tg_id=1001, auth_date=int(NOW))) == "auth_invalid"
    assert _code(data, token="") == "auth_invalid"


def test_expiry_and_future() -> None:
    expired = _signed(auth_date=int(NOW) - MAX_AGE_SEC - 1)
    assert _code(expired) == "auth_expired"
    fresh = _signed(auth_date=int(NOW) - MAX_AGE_SEC + 60)
    assert validate_init_data(fresh, TOKEN, now=NOW).tg_id == 1001
    future = _signed(auth_date=int(NOW) + 10 * 60)
    assert _code(future) == "auth_invalid"
    near_future = _signed(auth_date=int(NOW) + FUTURE_SKEW_SEC - 10)
    assert validate_init_data(near_future, TOKEN, now=NOW).tg_id == 1001


@pytest.mark.parametrize(
    "user",
    [
        None,
        "не json",
        "[1, 2]",
        '{"id": "1001", "first_name": "A"}',
        '{"id": 0, "first_name": "A"}',
        '{"id": -5, "first_name": "A"}',
        '{"id": true, "first_name": "A"}',
        '{"id": 1.5, "first_name": "A"}',
        '{"id": 1001, "first_name": 5}',
        '{"id": 99999999999999999999999, "first_name": "A"}',
    ],
    ids=["нет", "не-json", "массив", "id-строка", "id-0", "id-отриц", "id-true", "id-дробь", "имя-число", "id-огромный"],
)
def test_bad_user_field(user: str | None) -> None:
    fields = [("auth_date", str(int(NOW))), ("query_id", "AAx")]
    if user is not None:
        fields.append(("user", user))
    assert _code(_resign(fields)) == "auth_invalid"


def test_empty_first_name_is_allowed() -> None:
    data = _resign([("auth_date", str(int(NOW))), ("user", '{"id":1001,"first_name":""}')])
    assert validate_init_data(data, TOKEN, now=NOW).first_name == ""


@pytest.mark.parametrize(
    "raw",
    [
        "%%%",
        "a=b&&",
        "user=1&hash=abc",
        "auth_date=1&auth_date=2&hash=abc",
        "auth_date=12&user=%7B%7D",
        "hash=" + "Ж" * 64,
        "hash=zz&auth_date=1",
        "x" * (MAX_INIT_DATA_LEN + 1),
        "=&=",
        "hash",
        "\udcff=1&hash=00",
    ],
    ids=["проценты", "пустая-пара", "мусорный-hash", "повтор-ключа", "нет-hash", "не-ascii-hash", "не-hex",
         "слишком-длинно", "пустые-ключи", "без-равно", "суррогат"],
)
def test_garbage_is_always_auth_error(raw: str) -> None:
    assert _code(raw) in ("auth_invalid", "auth_missing")


def test_duplicate_key_with_valid_signature_is_invalid() -> None:
    data = _signed()
    assert _code(data + "&auth_date=" + str(int(NOW))) == "auth_invalid"


def test_non_integer_auth_date_is_invalid() -> None:
    for value in ("abc", "1.5", " 12", "+12", "1_000", ""):
        data = _resign([("auth_date", value), ("user", '{"id":1001,"first_name":"A"}')])
        assert _code(data) == "auth_invalid", value


def test_signature_field_takes_part_in_check() -> None:
    """Bot API 8.0: поле signature входит в data_check_string."""
    data = _signed(extra={"signature": "abc-signature"})
    assert validate_init_data(data, TOKEN, now=NOW).tg_id == 1001
    tampered = data.replace("abc-signature", "abd-signature")
    assert _code(tampered) == "auth_invalid"


def test_agrees_with_aiogram_check_webapp_signature() -> None:
    good = [_signed(tg_id=n, first_name=name, extra={"signature": "s"}) for n, name in ((1, "A"), (2, "Ж Ё"), (3, "x&y=z"))]
    bad = [
        good[0].replace("hash=", "hash=0"),
        sign_init_data("1:WRONG", tg_id=1, auth_date=int(NOW)),
        good[1].replace("%D0%96", "%D0%97"),
        good[2] + "&extra=1",
    ]
    for value in good:
        assert check_webapp_signature(TOKEN, value) is True
        validate_init_data(value, TOKEN, now=NOW)
    for value in bad:
        assert check_webapp_signature(TOKEN, value) is False
        assert _code(value) == "auth_invalid"


def test_sign_init_data_looks_like_telegram() -> None:
    data = sign_init_data(TOKEN, tg_id=7, first_name="Анна", username="anna")
    fields = dict(parse_qsl(data))
    assert set(fields) >= {"user", "auth_date", "query_id", "hash"}
    assert json.loads(fields["user"])["id"] == 7
    assert abs(int(fields["auth_date"]) - time.time()) < 5
    assert len(fields["hash"]) == 64


# --- Через HTTP ------------------------------------------------------------------------------------------


async def test_http_valid_and_missing(ma: MiniApp) -> None:
    resp = await ma.get("/api/me", as_=MGR)
    assert resp.status == 200 and resp["access"] == "unregistered"
    resp = await ma.get("/api/me")
    assert resp.status == 401 and resp.code == "auth_missing"
    assert resp.error == auth.AUTH_MESSAGES["auth_missing"]


async def test_http_expired_and_garbage_never_500(ma: MiniApp) -> None:
    old = int(time.time()) - MAX_AGE_SEC - 1
    resp = await ma.get("/api/me", headers={INIT_DATA_HEADER: ma.init_data(MGR, auth_date=old)})
    assert resp.status == 401 and resp.code == "auth_expired"
    for raw in ("%%%", "a=b&&", "hash=" + "Ж" * 3, "a=" + "x" * 4000, "user=%7B&hash=1&auth_date=1"):
        resp = await ma.get("/api/me", headers={INIT_DATA_HEADER: raw})
        assert resp.status == 401, raw[:40]
        assert resp.code == "auth_invalid"
    # Байты, которые не декодируются как UTF-8 (aiohttp отдаёт их суррогатами)
    resp = await ma.client.get("/api/me", headers={INIT_DATA_HEADER: "hash=\udcff".encode("utf-8", "surrogateescape").decode("latin-1")})
    assert resp.status == 401


async def test_empty_bot_token_always_401(ma: MiniApp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ma.ctx.settings, "bot_token", "")
    resp = await ma.get("/api/me", as_=MGR)
    assert resp.status == 401 and resp.code == "auth_invalid"


async def test_init_data_and_hash_never_logged(ma: MiniApp, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    data = ma.init_data(MGR)
    digest = dict(parse_qsl(data))["hash"]
    await ma.get("/api/me", headers={INIT_DATA_HEADER: data})
    await ma.get("/api/tasks", headers={INIT_DATA_HEADER: data})
    await ma.get("/api/me", headers={INIT_DATA_HEADER: data.replace(digest, "0" * 64)})
    text = caplog.text
    assert digest not in text and data not in text and "0" * 64 not in text
    assert TOKEN not in text
    assert "API 401 auth_invalid" in text
