"""Статический контракт SPA Mini App (docs/MINIAPP_SPEC.md §11, §12.4) — без браузера и сервера.

* index.html ссылается только на существующие файлы из белого списка pages.py, содержит заглушки
  сервера и не содержит кода в разметке (CSP);
* app.js обращается только к маршрутам API из таблицы §8.1 (и к bot.webapp.api.ROUTES, когда пакет
  API уже есть), не строит разметку из строк и не ходит на внешние адреса;
* app.css задаёт токены темы §11.3 для светлой и тёмной палитры;
* размеры файлов — в лимитах §11.1.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "bot" / "webapp" / "static"
SPEC = ROOT / "docs" / "MINIAPP_SPEC.md"
INDEX = STATIC / "index.html"
APP_JS = STATIC / "app.js"
APP_CSS = STATIC / "app.css"

TELEGRAM_JS = "https://telegram.org/js/telegram-web-app.js"
# Идентификатор пространства имён для document.createElementNS, а не сетевой адрес: браузер по нему не ходит.
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
STATIC_WHITELIST = {"app.js", "app.css"}  # pages.py отдаёт из /app/static/ только эти файлы (§4.3)
SIZE_LIMITS = {"index.html": 4 * 1024, "app.css": 40 * 1024, "app.js": 200 * 1024}

TAB_LABELS = ("Команда", "Задачи", "Проверка", "Новая", "Мои задачи", "Сдать", "Мой KPI", "Поручение")
TAB_ROOTS = ("#/team", "#/tasks", "#/review", "#/new", "#/my", "#/submit", "#/kpi", "#/propose")
THEME_TOKENS = {
    "--bg": "--tg-theme-bg-color",
    "--bg2": "--tg-theme-secondary-bg-color",
    "--section": "--tg-theme-section-bg-color",
    "--text": "--tg-theme-text-color",
    "--hint": "--tg-theme-hint-color",
    "--link": "--tg-theme-link-color",
    "--accent": "--tg-theme-button-color",
    "--accent-text": "--tg-theme-button-text-color",
    "--destructive": "--tg-theme-destructive-text-color",
    "--separator": "--tg-theme-section-separator-color",
}
OWN_TOKENS = ("--good", "--warn", "--bad")
LAYOUT_TOKENS = ("--radius", "--gap", "--tap")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- Разбор index.html -----------------------------------------------------------------------------


class _Page(HTMLParser):
    """Собирает теги с атрибутами (по порядку) и содержимое <script> без src."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[dict[str, str | None]] = []
        self.script_text: dict[int, str] = {}
        self.text_by_id: dict[str, str] = {}
        self._open_script: int | None = None
        self._open_ids: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        self.tags.append((tag, data))
        if tag == "script":
            self.scripts.append(data)
            self._open_script = len(self.scripts) - 1
            self.script_text[self._open_script] = ""
        if data.get("id"):
            self._open_ids.append((tag, str(data["id"])))
            self.text_by_id.setdefault(str(data["id"]), "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._open_script = None
        if self._open_ids and self._open_ids[-1][0] == tag:
            self._open_ids.pop()

    def handle_data(self, data: str) -> None:
        if self._open_script is not None:
            self.script_text[self._open_script] += data
        for _, element_id in self._open_ids:
            self.text_by_id[element_id] += data


@pytest.fixture(scope="module")
def page() -> _Page:
    parser = _Page()
    parser.feed(_read(INDEX))
    parser.close()
    return parser


def _first(page: _Page, tag: str, **attrs: str) -> dict[str, str | None]:
    for name, data in page.tags:
        if name == tag and all(data.get(key) == value for key, value in attrs.items()):
            return data
    raise AssertionError(f"В index.html нет <{tag} {attrs}>")


# --- Файлы и размеры --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SIZE_LIMITS))
def test_static_file_exists_and_fits_limit(name: str) -> None:
    path = STATIC / name
    assert path.is_file(), f"нет файла {path}"
    size = path.stat().st_size
    assert 0 < size <= SIZE_LIMITS[name], f"{name}: {size} байт, лимит {SIZE_LIMITS[name]}"
    _read(path)  # UTF-8 без ошибок


# --- index.html ------------------------------------------------------------------------------------


def test_index_head_contract(page: _Page) -> None:
    html = _first(page, "html")
    assert html.get("lang") == "ru"
    assert any(tag == "meta" and (data.get("charset") or "").lower() == "utf-8" for tag, data in page.tags)

    viewport = _first(page, "meta", name="viewport").get("content") or ""
    assert "width=device-width" in viewport
    assert "initial-scale=1" in viewport
    assert "viewport-fit=cover" in viewport
    compact = viewport.replace(" ", "").lower()
    assert "user-scalable=no" not in compact and "user-scalable=0" not in compact, "масштабирование не запрещать"
    assert "maximum-scale" not in compact, "масштабирование не запрещать"
    assert _first(page, "meta", name="color-scheme").get("content") == "light dark"

    assert page.scripts, "в index.html нет скриптов"
    assert page.scripts[0].get("src") == TELEGRAM_JS, "первым скриптом должен идти telegram-web-app.js"

    css = _first(page, "link", rel="stylesheet")
    assert css.get("href") == "/app/static/app.css?v=__ASSET_VERSION__"
    app = _first(page, "script", src="/app/static/app.js?v=__ASSET_VERSION__")
    assert "defer" in app, "app.js подключается с defer (без модулей)"
    assert app.get("type") in (None, "text/javascript")

    config_index = next(i for i, data in enumerate(page.scripts) if data.get("id") == "kpi-config")
    assert page.scripts[config_index].get("type") == "application/json"
    assert page.script_text[config_index].strip() == "__KPI_CONFIG__"


def test_index_placeholders_exact(page: _Page) -> None:
    text = _read(INDEX)
    assert text.count("__ASSET_VERSION__") == 2, "версия ассетов — ровно в src app.js и href app.css"
    assert text.count("__KPI_CONFIG__") == 1


def test_index_body_mount_points(page: _Page) -> None:
    app = _first(page, "div", id="app")
    assert app is not None
    assert "Загрузка…" in page.text_by_id.get("app", "")
    toast = _first(page, "div", id="toast")
    assert toast.get("role") == "status"
    assert toast.get("aria-live") == "polite"


def test_index_has_no_inline_code(page: _Page) -> None:
    for tag, data in page.tags:
        handlers = [key for key in data if key.lower().startswith("on")]
        assert not handlers, f"<{tag}> с обработчиком {handlers} — запрещено CSP"
        for key, value in data.items():
            assert not (value or "").strip().lower().startswith("javascript:"), f"<{tag} {key}> с javascript:"
    for index, data in enumerate(page.scripts):
        if data.get("src"):
            assert not page.script_text[index].strip(), "у скрипта с src не должно быть кода внутри"
        else:
            assert data.get("type") == "application/json", "встроенные скрипты — только JSON-данные"


def test_index_references_existing_static_files(page: _Page) -> None:
    referenced: set[str] = set()
    for _tag, data in page.tags:
        for key in ("src", "href"):
            value = data.get(key)
            if not value or value == TELEGRAM_JS:
                continue
            assert value.startswith("/app/static/"), f"путь «{value}» должен быть абсолютным /app/static/…"
            name = value.removeprefix("/app/static/").split("?", 1)[0]
            assert name in STATIC_WHITELIST, f"«{name}» нет в белом списке статики pages.py"
            assert (STATIC / name).is_file(), f"index.html ссылается на несуществующий {name}"
            referenced.add(name)
    assert referenced == STATIC_WHITELIST


# --- Внешние адреса и опасные конструкции -----------------------------------------------------------


_URL_RE = re.compile(r"https?://[^\s\"'`<>)]+", re.IGNORECASE)
_PROTOCOL_RELATIVE_RE = re.compile(r"""["'`(]\s*//[a-z0-9]""", re.IGNORECASE)


@pytest.mark.parametrize(
    ("name", "allowed"),
    [("index.html", {TELEGRAM_JS}), ("app.js", {SVG_NAMESPACE}), ("app.css", set())],
)
def test_no_external_addresses(name: str, allowed: set[str]) -> None:
    text = _read(STATIC / name)
    found = set(_URL_RE.findall(text))
    assert found <= allowed, f"{name}: внешние адреса {sorted(found - allowed)}"
    assert not _PROTOCOL_RELATIVE_RE.search(text), f"{name}: адрес вида //host"


def test_css_loads_nothing() -> None:
    css = _read(APP_CSS)
    assert "@import" not in css
    assert "url(" not in css, "картинки и шрифты извне не грузим (иконки — эмодзи)"
    assert "@font-face" not in css


@pytest.mark.parametrize(
    "pattern",
    [r"\binnerHTML\b", r"\bouterHTML\b", r"\binsertAdjacentHTML\b", r"\bdocument\.write", r"\beval\s*\(",
     r"\bnew\s+Function\b", r"\bsetTimeout\s*\(\s*['\"`]", r"\bsetInterval\s*\(\s*['\"`]", r"\bimport\s*\("],
)
def test_js_has_no_forbidden_constructs(pattern: str) -> None:
    assert not re.search(pattern, _read(APP_JS)), f"в app.js найдено {pattern}"


# --- Маршруты API -----------------------------------------------------------------------------------


def _norm(path: str) -> str:
    """«/api/tasks/{id}» и «/api/tasks/{task_id}» -> «/api/tasks/{}»."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def _spec_routes() -> set[tuple[str, str]]:
    """Маршруты из сводной таблицы §8.1 спецификации: строки «| `METHOD /api/…` | …»."""
    pairs = re.findall(r"^\|\s*`(GET|POST|PATCH|PUT|DELETE) (/api/[^`\s?]+)`", _read(SPEC), flags=re.MULTILINE)
    return {(method, _norm(path)) for method, path in pairs}


def _js_endpoints() -> list[tuple[str, str]]:
    """Пары [метод, шаблон пути] из объекта EP в app.js."""
    return re.findall(r"\[\s*'(GET|POST|PATCH|PUT|DELETE)'\s*,\s*'(/api/[^']*)'\s*\]", _read(APP_JS))


def _js_api_literals() -> list[str]:
    """Все строковые литералы app.js, начинающиеся с /api/ (шаблон ${…} -> параметр, без ?…)."""
    text = _read(APP_JS)
    literals = [m.group(2) for m in re.finditer(r"(['\"`])(/api/[^'\"`]*)\1", text)]
    return [re.sub(r"\$\{[^}]*\}", "{}", value).split("?", 1)[0] for value in literals]


def test_spec_route_table_is_parsed() -> None:
    routes = _spec_routes()
    assert len(routes) >= 20, routes
    assert ("GET", "/api/me") in routes
    assert ("POST", "/api/tasks/{}/submit") in routes


def test_js_endpoints_exist_in_spec() -> None:
    endpoints = _js_endpoints()
    assert endpoints, "в app.js не найден объект EP с маршрутами"
    unknown = sorted({(m, p) for m, p in endpoints if (m, _norm(p)) not in _spec_routes()})
    assert not unknown, f"app.js вызывает маршруты, которых нет в MINIAPP_SPEC §8.1: {unknown}"


def test_js_covers_every_spec_route() -> None:
    used = {(m, _norm(p)) for m, p in _js_endpoints()}
    missing = sorted(_spec_routes() - used)
    assert not missing, f"в SPA нет вызова маршрутов из §8.1: {missing}"


def test_every_api_literal_is_a_known_route() -> None:
    spec_paths = {path for _method, path in _spec_routes()}
    literals = _js_api_literals()
    assert literals
    unknown = sorted({lit for lit in literals if _norm(lit) not in spec_paths})
    assert not unknown, f"литералы /api/… без маршрута в спецификации: {unknown}"


def test_js_endpoint_placeholders_use_route_param_names() -> None:
    """Имена параметров — как в ROUTES (§8.2): task_id, sub_id, user_id."""
    names = {name for _m, path in _js_endpoints() for name in re.findall(r"\{([^}]*)\}", path)}
    assert names <= {"task_id", "sub_id", "user_id"}, names


def test_js_endpoints_exist_in_api_routes() -> None:
    """Сверка с bot.webapp.api.ROUTES — источником таблицы маршрутов сервера (§8.2, §12.4)."""
    if importlib.util.find_spec("bot.webapp") is None or importlib.util.find_spec("bot.webapp.api") is None:
        pytest.skip("пакет API (bot/webapp/api.py) ещё не создан — сверка только со спецификацией")
    api = importlib.import_module("bot.webapp.api")
    routes = {(method.upper(), path) for method, path in api.ROUTES}
    unknown = sorted({(m, p) for m, p in _js_endpoints() if (m, p) not in routes})
    assert not unknown, f"app.js вызывает маршруты, которых нет в bot.webapp.api.ROUTES: {unknown}"


# --- Тема, вкладки, интеграция с Telegram -------------------------------------------------------------


def _block(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, flags=re.DOTALL)
    assert match, f"в app.css нет блока {selector}"
    return match.group(1)


def _token_value(block: str, token: str) -> str:
    match = re.search(rf"(?<![\w-]){re.escape(token)}\s*:\s*([^;]+);", block)
    assert match, f"токен {token} не задан"
    return match.group(1).strip()


@pytest.mark.parametrize("selector", [":root", ':root[data-theme="dark"]'])
def test_css_theme_tokens(selector: str) -> None:
    block = _block(_read(APP_CSS), selector)
    for token, telegram_var in THEME_TOKENS.items():
        value = _token_value(block, token)
        assert value.startswith(f"var({telegram_var},"), f"{selector} {token}: {value} — нужна запасная палитра"
    for token in OWN_TOKENS:
        assert re.match(r"#[0-9a-fA-F]{3,8}$", _token_value(block, token)), token
    if selector == ":root":
        for token in LAYOUT_TOKENS:
            _token_value(block, token)


def test_css_layout_rules() -> None:
    css = _read(APP_CSS)
    assert "--tg-viewport-stable-height" in css
    assert "safe-area-inset-bottom" in css
    assert "max-width: 640px" in css
    assert "prefers-reduced-motion" in css
    assert ":focus-visible" in css
    assert re.search(r"body\s*\{[^}]*background:\s*var\(--bg2\)", css), "фон страницы — var(--bg2)"


def _contrast(first: str, second: str) -> float:
    """Контраст WCAG двух цветов #rrggbb."""

    def luminance(hex_color: str) -> float:
        raw = hex_color.lstrip("#")
        channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    a, b = sorted((luminance(first), luminance(second)), reverse=True)
    return (a + 0.05) / (b + 0.05)


@pytest.mark.parametrize("selector", [":root", ':root[data-theme="dark"]'])
def test_danger_button_text_is_readable(selector: str) -> None:
    """Красная основная кнопка («Отменить задачу», «Отклонить») вне Telegram: текст ≥ 4.5:1 в обеих темах."""
    css = _read(APP_CSS)
    block = _block(css, selector)
    background = re.search(r"#[0-9a-fA-F]{6}", _token_value(block, "--destructive"))
    assert background, "у --destructive нет запасного цвета"
    assert _contrast(background.group(0), _token_value(block, "--danger-text")) >= 4.5
    assert "color: var(--danger-text)" in _block(css, ".primary-fallback.is-danger .btn")


def test_segmented_controls_are_touch_sized() -> None:
    """Сегменты (период, «Результаты / Поручения», приоритет) — цель касания ≥ 44 px (§11.10)."""
    css = _read(APP_CSS)
    assert _token_value(_block(css, ":root"), "--tap") == "44px"
    assert re.search(r"min-height:\s*var\(--tap\)", _block(css, '.seg [role="tab"], .seg [role="radio"]'))


def test_js_has_all_tabs() -> None:
    js = _read(APP_JS)
    for label in TAB_LABELS:
        assert f"label: '{label}'" in js, f"нет вкладки «{label}»"
    for root in TAB_ROOTS:
        assert f"'{root}'" in js, f"нет маршрута {root}"


@pytest.mark.parametrize(
    "needle",
    [
        "X-Telegram-Init-Data", "X-App-Version", "tg_debug_init", "kpi_debug_init", "kpi-config",
        "MainButton", "BackButton", "HapticFeedback", "enableClosingConfirmation", "disableClosingConfirmation",
        "disableVerticalSwipes", "isVersionAtLeast", "themeChanged", "showConfirm", "tg.ready()", "tg.expand()",
        "XMLHttpRequest", "upload.onprogress",
    ],
)
def test_js_integrates_with_telegram(needle: str) -> None:
    assert needle in _read(APP_JS), f"в app.js нет «{needle}»"


def test_debug_init_data_only_in_debug_mode() -> None:
    """?tg_debug_init= читается только при CONFIG.debug (§5.4): в бою ссылка с чужим initData не сработает."""
    js = _read(APP_JS)
    body = js[js.index("function resolveInitData"):]
    body = body[: body.index("\n  }\n")]
    assert body.index("if (!CONFIG.debug) return ''") < body.index("tg_debug_init")
