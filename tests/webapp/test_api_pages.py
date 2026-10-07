"""Страница и статика Mini App, монтирование пакета (docs/MINIAPP_SPEC.md §4.1, §4.3, §12.2 test_pages)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot.config import Settings
from bot.webapp import (
    CSP,
    CTX,
    TASKS,
    _kpi_config,
    pending_tasks,
    register_webapp,
    setup_webapp,
)

from .conftest import STUB_CSS, STUB_JS, TOKEN, MiniApp, write_static

VERSION = hashlib.sha256(STUB_JS + STUB_CSS).hexdigest()[:12]


def _no_db() -> Any:
    raise AssertionError("страница не должна обращаться к базе")


@asynccontextmanager
async def serve(static_dir: Path, **settings: Any) -> AsyncIterator[TestClient]:
    app = web.Application()
    bot = Bot(TOKEN)
    setup_webapp(app, bot=bot, sessionmaker=_no_db, settings=Settings(bot_token=TOKEN, **settings),  # type: ignore[arg-type]
                 static_dir=static_dir)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
        await bot.session.close()


def _config(html: str) -> dict[str, Any]:
    match = re.search(r'<script id="kpi-config" type="application/json">(.*?)</script>', html)
    assert match, html
    return json.loads(match.group(1))


@pytest.mark.parametrize("path", ["/app", "/app/"])
async def test_index_page(tmp_path: Path, path: str) -> None:
    async with serve(write_static(tmp_path), run_mode="polling") as client:
        resp = await client.get(path)
        html = await resp.text()
        assert resp.status == 200 and resp.content_type == "text/html" and resp.charset == "utf-8"
        assert resp.headers["Content-Security-Policy"] == CSP
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "no-referrer" and resp.headers["Cache-Control"] == "no-store"
        assert "X-Frame-Options" not in resp.headers
        assert "__ASSET_VERSION__" not in html and "__KPI_CONFIG__" not in html
        assert f"/app/static/app.js?v={VERSION}" in html and f"/app/static/app.css?v={VERSION}" in html
        assert _config(html) == {"version": VERSION, "debug": False}
        head = await client.head(path)
        assert head.status == 200


@pytest.mark.parametrize(
    ("settings", "debug"),
    [({"run_mode": "polling", "webapp_debug": True}, True), ({"run_mode": "webhook", "webapp_debug": True}, False),
     ({"run_mode": "polling", "webapp_debug": False}, False)],
    ids=["отладка", "webhook-игнорирует-отладку", "без-отладки"],
)
async def test_debug_flag(tmp_path: Path, settings: dict[str, Any], debug: bool) -> None:
    async with serve(write_static(tmp_path), **settings) as client:
        html = await (await client.get("/app")).text()
        assert _config(html)["debug"] is debug


async def test_debug_mode_rereads_files(tmp_path: Path) -> None:
    folder = write_static(tmp_path / "debug")
    async with serve(folder, run_mode="polling", webapp_debug=True) as client:
        (folder / "app.js").write_bytes(b"console.log('v2');")
        html = await (await client.get("/app")).text()
        new_version = hashlib.sha256(b"console.log('v2');" + STUB_CSS).hexdigest()[:12]
        assert _config(html)["version"] == new_version
        assert await (await client.get("/app/static/app.js")).read() == b"console.log('v2');"
    folder2 = write_static(tmp_path / "prod")
    async with serve(folder2, run_mode="polling") as client:
        (folder2 / "app.js").write_bytes(b"console.log('v3');")
        assert _config(await (await client.get("/app")).text())["version"] == VERSION


async def test_static_whitelist_types_and_cache(tmp_path: Path) -> None:
    async with serve(write_static(tmp_path)) as client:
        js = await client.get(f"/app/static/app.js?v={VERSION}")
        assert js.status == 200 and await js.read() == STUB_JS
        assert js.headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert js.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert js.headers["X-Content-Type-Options"] == "nosniff"
        css = await client.get("/app/static/app.css")
        assert css.headers["Content-Type"] == "text/css; charset=utf-8" and css.headers["Cache-Control"] == "no-cache"
        stale = await client.get("/app/static/app.css?v=000000000000")
        assert stale.headers["Cache-Control"] == "no-cache"
        assert (await client.head("/app/static/app.js")).status == 200


@pytest.mark.parametrize(
    "path",
    ["/app/static/index.html", "/app/static/../__init__.py", "/app/static/%2e%2e%2fapi.py", "/app/static/app.js/",
     "/app/static/sub/app.js", "/app/static/", "/app/static/APP.JS", "/app/static/.%2e/auth.py", "/app/x"],
)
async def test_unknown_files_and_traversal_are_404(tmp_path: Path, path: str) -> None:
    async with serve(write_static(tmp_path)) as client:
        resp = await client.get(path, allow_redirects=False)
        assert resp.status == 404, path


async def test_not_built_is_503(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    (folder / "index.html").write_text("<html></html>", encoding="utf-8")
    async with serve(folder) as client:
        resp = await client.get("/app")
        assert resp.status == 503 and resp.content_type == "text/plain"
        assert await resp.text() == "Приложение ещё не собрано"
        assert (await client.get("/app/static/app.js")).status == 404


def test_config_json_escapes_less_than() -> None:
    text = _kpi_config("</script><b>", True)
    assert "<" not in text and json.loads(text) == {"version": "</script><b>", "debug": True}


async def test_mount_once_register_and_pending(tmp_path: Path) -> None:
    app = web.Application()
    bot = Bot(TOKEN)
    try:
        assert pending_tasks(app) == set()
        register_webapp(app, bot, None, _no_db, Settings(bot_token=TOKEN), static_dir=write_static(tmp_path))
        assert CTX in app and TASKS in app
        with pytest.raises(RuntimeError):
            setup_webapp(app, bot=bot, sessionmaker=_no_db, settings=Settings(bot_token=TOKEN))  # type: ignore[arg-type]
        release = asyncio.Event()
        task = app[TASKS].spawn(release.wait(), name="test")
        assert pending_tasks(app) == {task}
        release.set()
        await app[TASKS].drain(timeout=5)
        assert pending_tasks(app) == set()
    finally:
        await bot.session.close()


async def test_background_errors_are_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    from bot.webapp import TaskRegistry

    async def boom() -> None:
        raise RuntimeError("сбой фоновой задачи")

    registry = TaskRegistry()
    registry.spawn(boom(), name="boom")
    await registry.drain(timeout=5)
    assert "фоновая задача boom завершилась ошибкой" in caplog.text


async def test_api_and_pages_together(ma: MiniApp) -> None:
    page = await ma.client.get("/app")
    assert page.status == 200
    resp = await ma.get("/api/me")
    assert resp.status == 401 and resp.headers["X-App-Version"] == ma.ctx.static.version
