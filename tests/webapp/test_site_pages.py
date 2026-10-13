"""Страница сайта и её файлы (docs/MINIAPP_SPEC.md §15): то же приложение в обычном браузере.

* ``/`` — страница сайта: без скрипта Telegram, встраивать её в чужие страницы нельзя; тот же app.js и
  app.css плюс site.css; пометка ``web`` в настройках страницы;
* ``/app`` (Mini App) не меняется;
* сайт выключен или его файлов нет — ``/`` отвечает служебной строкой, как раньше;
* в коде, который уходит в браузер, нет секретов сервера.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from test_web import make_bot, make_settings, no_db, ping_dispatcher, serve

from bot import web as bot_web
from bot.webapp import STATIC_DIR, site

from .conftest import EMP, MiniApp

ROOT = Path(__file__).resolve().parents[2]
SITE_HTML = STATIC_DIR / "site.html"
SITE_CSS = STATIC_DIR / "site.css"
SIZE_LIMITS = {"site.html": 4 * 1024, "site.css": 24 * 1024}
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
BROWSER_FILES = ("index.html", "site.html", "app.js", "app.css", "site.css")


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline_scripts: list[tuple[dict[str, str | None], str]] = []
        self._script: dict[str, str | None] | None = None
        self._text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        self.tags.append((tag, data))
        if tag == "script":
            self._script, self._text = data, ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.inline_scripts.append((self._script, self._text))
            self._script = None

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._text += data


def _parse(text: str) -> _Page:
    page = _Page()
    page.feed(text)
    page.close()
    return page


def _config(text: str) -> dict[str, Any]:
    page = _parse(text)
    [(attrs, body)] = [(a, b) for a, b in page.inline_scripts if a.get("id") == "kpi-config"]
    assert attrs.get("type") == "application/json"
    return json.loads(body)


# --- Файлы ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SIZE_LIMITS))
def test_site_file_exists_and_fits_limit(name: str) -> None:
    path = STATIC_DIR / name
    assert path.is_file(), f"нет файла {path}"
    size = path.stat().st_size
    assert 0 < size <= SIZE_LIMITS[name], f"{name}: {size} байт, лимит {SIZE_LIMITS[name]}"


def test_site_page_markup() -> None:
    text = SITE_HTML.read_text(encoding="utf-8")
    page = _parse(text)
    assert "telegram" not in text.lower(), "страница сайта не подключает скрипт Telegram"
    assert text.count("__ASSET_VERSION__") == 3 and text.count("__KPI_CONFIG__") == 1

    scripts = [data for tag, data in page.tags if tag == "script"]
    assert [data.get("src") for data in scripts] == ["/app/static/app.js?v=__ASSET_VERSION__", None]
    assert "defer" in scripts[0]
    assert scripts[1].get("id") == "kpi-config" and scripts[1].get("type") == "application/json"
    styles = [data.get("href") for tag, data in page.tags if tag == "link" and data.get("rel") == "stylesheet"]
    assert styles == ["/app/static/app.css?v=__ASSET_VERSION__", "/app/static/site.css?v=__ASSET_VERSION__"]

    for tag, data in page.tags:  # никакого кода в разметке — этого требует политика страницы
        assert not [key for key in data if key.lower().startswith("on")], tag
        for key in ("src", "href"):
            value = data.get(key) or ""
            assert value.startswith(("/app/static/", "data:image/svg+xml,")) or not value, value
    ids = {data.get("id") for _tag, data in page.tags}
    assert {"app", "toast", "kpi-config"} <= ids
    assert any(tag == "body" and data.get("class") == "web" for tag, data in page.tags)
    viewport = next(data.get("content") or "" for tag, data in page.tags if tag == "meta" and data.get("name") == "viewport")
    assert "width=device-width" in viewport and "user-scalable" not in viewport and "maximum-scale" not in viewport
    assert any(tag == "meta" and data.get("name") == "robots" and "noindex" in (data.get("content") or "") for tag, data in page.tags)


def test_site_css_loads_nothing_and_uses_app_tokens() -> None:
    css = SITE_CSS.read_text(encoding="utf-8")
    assert "@import" not in css and "url(" not in css and "@font-face" not in css
    assert not re.search(r"https?://", css)
    # Цвета — токены app.css; свои цвета — только у QR-кода (он обязан быть чёрным на белом).
    body = css.split("}", 1)[1]  # всё после блока своих токенов :root
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), "цвет вне токенов"
    assert "var(--qr-dark)" in body and "var(--qr-light)" in body


# --- Секреты ------------------------------------------------------------------------------------------------

_SECRET_SHAPES = {
    "токен бота": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),
    "ключ Google": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "строка подключения к базе": re.compile(r"postgres(?:ql)?(?:\+\w+)?://", re.IGNORECASE),
    "ключ API": re.compile(r"\b(?:sk|gsk|pk)[-_][A-Za-z0-9]{20,}"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\."),
}


def _env_secrets() -> list[str]:
    """Значения секретов из настоящих .env проекта (если они есть на этом компьютере)."""
    values: list[str] = []
    for path in (ROOT / ".env", ROOT / "deploy" / "render.env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            value = value.strip().strip("\"'")
            if not sep or key.strip().startswith("#") or len(value) < 16:
                continue
            if re.search(r"TOKEN|KEY|SECRET|PASSWORD|DATABASE_URL", key.upper()):
                values.append(value)
    return values


@pytest.mark.parametrize("name", BROWSER_FILES)
def test_browser_files_hold_no_secrets(name: str) -> None:
    text = (STATIC_DIR / name).read_text(encoding="utf-8")
    for label, shape in _SECRET_SHAPES.items():
        assert not shape.search(text), f"{name}: похоже на «{label}»"
    for value in _env_secrets():
        assert value not in text, f"{name}: значение секрета из .env"
    assert "process.env" not in text and "BOT_TOKEN" not in text and "DATABASE_" not in text


async def test_served_pages_and_api_hold_no_secrets(ma: MiniApp) -> None:
    """Токен бота и секреты входа не попадают ни в страницу, ни в ответы, которые читает браузер."""
    await ma.seed_team(1)
    from .conftest import TOKEN

    me = await ma.get("/api/me", as_=EMP)
    started = await ma.post("/auth/start", headers={site.CSRF_HEADER: "1"})
    assert started.status == 200
    for resp in (me, started):
        assert TOKEN not in resp.text and "42:" not in resp.text
        for label, shape in _SECRET_SHAPES.items():
            assert not shape.search(resp.text), label
    assert set(started.data) == {"code", "link", "qr", "expires_in"}


# --- Страницы -----------------------------------------------------------------------------------------------


async def test_root_serves_the_site_without_database() -> None:
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings())  # type: ignore[arg-type]
    async with serve(app) as client:
        response = await client.get("/")
        text = await response.text()
        assert response.status == 200 and response.content_type == "text/html"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        csp = response.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp and "script-src 'self';" in csp and "telegram" not in csp
        assert "__ASSET_VERSION__" not in text and "__KPI_CONFIG__" not in text and "telegram.org" not in text

        config = _config(text)
        assert config["web"] is True and config["debug"] is False and re.fullmatch(r"[0-9a-f]{12}", config["version"])
        assert config.get("bot", "https://t.me/x").startswith("https://t.me/")
        version = config["version"]
        assert f"/app/static/site.css?v={version}" in text and f"/app/static/app.js?v={version}" in text

        response = await client.get(f"/app/static/site.css?v={version}")
        assert response.status == 200 and response.headers["Content-Type"] == "text/css; charset=utf-8"
        assert "immutable" in response.headers["Cache-Control"]
        assert await response.read() == SITE_CSS.read_bytes()
        response = await client.head("/")
        assert response.status == 200
        # Сайт использует тот же API и ту же версию приложения, что Mini App.
        response = await client.get("/api/me")
        assert response.status == 401 and response.headers["X-App-Version"] == version
    await bot.session.close()


async def test_mini_app_page_is_unchanged() -> None:
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings())  # type: ignore[arg-type]
    async with serve(app) as client:
        response = await client.get("/app")
        text = await response.text()
        assert response.status == 200
        assert "https://telegram.org/js/telegram-web-app.js" in text and "site.css" not in text
        csp = response.headers["Content-Security-Policy"]
        assert "https://telegram.org" in csp and "frame-ancestors 'self' https://web.telegram.org" in csp
        assert "X-Frame-Options" not in response.headers  # Telegram открывает Mini App во фрейме
        assert _config(text) == {"version": _config(text)["version"], "debug": False}
    await bot.session.close()


@pytest.mark.parametrize("overrides", [{"website_enabled": False}, {"webapp_enabled": False}], ids=["сайт-выключен", "приложение-выключено"])
async def test_root_is_the_service_line_when_the_site_is_off(overrides: dict[str, Any]) -> None:
    bot = make_bot()
    app = bot_web.build_web_app(bot, ping_dispatcher([]), no_db, make_settings(**overrides))  # type: ignore[arg-type]
    async with serve(app) as client:
        response = await client.get("/")
        assert response.status == 200 and await response.text() == "KPI bot is running"
        response = await client.post("/auth/start", headers={site.CSRF_HEADER: "1"})
        assert response.status == 404
    await bot.session.close()


async def test_root_is_the_service_line_without_site_files(ma: MiniApp) -> None:
    """Файлов сайта нет (в тестовой папке — только Mini App): страница сайта не отдаётся, Mini App работает."""
    request_like = type("R", (), {"app": ma.app})()
    assert await site.index(request_like) is None  # type: ignore[arg-type]
    assert (await ma.get("/app")).status == 200
    assert (await ma.get("/app/static/site.css")).status == 404


def test_settings_site_switch() -> None:
    assert make_settings().site_active is True and make_settings().cookies_secure is True
    assert make_settings(website_enabled=False).site_active is False
    assert make_settings(webapp_enabled=False).site_active is False
    assert make_settings(public_url="", render_external_url="").cookies_secure is False


async def test_cookies_are_host_bound_and_secure_over_https(ma: MiniApp) -> None:
    """В бою (адрес https) cookie сайта — с Secure и приставкой __Host-: их принимает и получает только
    этот адрес, и только по защищённому соединению."""
    await ma.seed_team(1)
    ma.ctx.settings = make_settings()  # webhook + https, как на хостинге
    assert ma.ctx.settings.cookies_secure is True
    started = await ma.post("/auth/start", headers={site.CSRF_HEADER: "1"})
    [line] = started.headers.getall("Set-Cookie")
    lowered = line.lower()
    assert line.startswith("__Host-kpi_login=") and "secure" in lowered and "httponly" in lowered
    assert "samesite=strict" in lowered and "path=/" in lowered and "domain=" not in lowered
