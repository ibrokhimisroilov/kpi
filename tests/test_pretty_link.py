"""Красивая ссылка на сайт: https://samaradorlik.onrender.com (docs/DEPLOY_RENDER.md, «Красивая ссылка»).

Это отдельный бесплатный статический сайт Render из одной страницы: она сразу переводит человека на адрес
бота. Сам бот, Mini App и сайт от неё не зависят — пропадёт ссылка, всё остальное работает.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "deploy" / "link" / "index.html"
SITE = "https://kpi-bot-ui7q.onrender.com/"
NAME = "samaradorlik"


def _link_service() -> str:
    """Блок статического сайта из render.yaml (от «- type: web» с именем ссылки до конца файла)."""
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^  - type: ", text)[1:]
    [block] = [b for b in blocks if re.search(rf"(?m)^\s+name: {NAME}$", b)]
    return block


def test_blueprint_has_a_free_static_site_for_the_link() -> None:
    block = _link_service()
    assert block.startswith("web\n")
    assert re.search(r"(?m)^\s+runtime: static$", block)
    assert re.search(r"(?m)^\s+staticPublishPath: \./deploy/link$", block)
    assert re.search(r"(?m)^\s+buildCommand: \S", block)  # обязателен для статического сайта, собирать нечего
    # У статического сайта нет тарифа, секретов и переменных: платного здесь быть не может.
    assert not re.search(r"(?m)^\s+(plan|envVars|region|dockerfilePath):", block)


def test_bot_service_is_untouched() -> None:
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert len(re.findall(r"(?m)^  - type: web$", text)) == 2
    assert len(re.findall(r"(?m)^\s+name: kpi-bot$", text)) == 1
    assert len(re.findall(r"(?m)^\s+plan: free$", text)) == 1


def test_page_sends_the_visitor_to_the_site() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert f'<meta http-equiv="refresh" content="0; url={SITE}">' in page  # без скриптов — тоже переход
    assert f"location.replace('{SITE}' + location.hash)" in page  # раздел после «#» не теряется
    assert f'href="{SITE}"' in page  # и обычная ссылка, если переход не сработал
    assert "<title>" in page and 'lang="ru"' in page


def test_page_loads_nothing_and_holds_no_secrets() -> None:
    page = PAGE.read_text(encoding="utf-8")
    addresses = set(re.findall(r"https?://[^\s\"'<>]+", page))
    assert addresses == {SITE}, addresses
    assert not re.search(r"<(link|img|iframe)\b[^>]*\b(href|src)=\"(?!https://kpi-bot-ui7q\.onrender\.com/)", page)
    assert "<script src" not in page
    assert not re.search(r"\d{8,10}:[A-Za-z0-9_-]{35}|AIza[0-9A-Za-z_-]{35}", page)
    assert len(page.encode("utf-8")) < 4096
