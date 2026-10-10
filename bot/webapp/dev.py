"""Dev-сервер приложения в Telegram для проверки в обычном браузере (docs/MINIAPP_SPEC.md §4.6).

    python -m bot.webapp.dev [--port 8081] [--db ПУТЬ] [--seed-demo] [--fake-voice] [--real-telegram]

* Только для разработки: слушает 127.0.0.1; база — отдельный файл SQLite во временной папке (не
  ``data/bot.db`` и не PostgreSQL); Telegram — фейковый (``tests/e2e/fakebot.py``: каждый «запрос к
  Telegram» печатается в консоль — видно, какие уведомления ушли); AI выключен. Секреты из ``.env`` не
  используются: всё нужное задаётся здесь до чтения настроек.
* Вход: ``/dev/`` — пользователи базы со ссылками «войти как …»; ``/dev/login?tg_id=…[&to=/route]``
  подписывает свежий initData тем же токеном, что проверяет сервер (``auth.sign_init_data``), и
  открывает ``/app?tg_debug_init=…`` — SPA в режиме отладки (``WEBAPP_DEBUG=1``) берёт initData из адреса
  (§5.4). Сервер проверяет подпись как обычно: исключений для отладки нет.
* ``--seed-demo`` — демо-команда (только в пустой базе): начальник, сотрудники, заявка на доступ,
  задачи всех статусов, сдачи с оценками и файлами, история оценок за несколько недель (для графика).
* ``--fake-voice`` — «включить» голосовой ввод без AI: любая запись «распознаётся» в заранее заданный текст
  (и поля задачи для «надиктовать целиком») — чтобы проверить кнопки микрофона в браузере.
* Никаких webhook, polling, фоновых заданий и кнопки меню: только ``/app``, ``/api`` и ``/dev``.
* ``--real-telegram`` — настоящий Bot с токеном из ``.env`` (только по явному флагу; уведомления уйдут
  настоящим людям). Подпись initData с этим токеном принимает и рабочий бот, поэтому вход закрыт
  ключом запуска (ссылка ``/dev/?key=…`` печатается в консоль) и только за пользователей dev-базы.
* Отвечает только на ``Host`` 127.0.0.1 / localhost (защита от DNS rebinding: чужой сайт в браузере
  разработчика не достучится до ``/dev/login``).

В Docker-образе папки ``tests`` нет — инструмент только для работы с исходниками проекта.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import math
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import web

if TYPE_CHECKING:
    from aiogram import Bot
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bot.config import Settings

__all__ = [
    "DEMO_APPLICANT",
    "DEMO_EMPLOYEES",
    "DEMO_MANAGER",
    "DEV_TOKEN",
    "EXTRA_EMPLOYEES",
    "DevConfigError",
    "access_key",
    "build_dev_app",
    "default_db_path",
    "dev_environment",
    "index_url",
    "login_url",
    "main",
    "make_fake_session",
    "resolve_db_path",
    "seed_demo",
]

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_DB = REPO_ROOT / "data" / "bot.db"
DEV_TOKEN = "42:DEV"
DEFAULT_PORT = 8081
HOST = "127.0.0.1"
DB_FILE_NAME = "kpi_webapp_dev.db"
# Ключи AI и пароль базы — пустые: dev-сервер не должен подхватить секреты из .env.
_BLANK_ENV = (
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "MISTRAL_API_KEY",
    "OPENROUTER_API_KEY",
    "DATABASE_PASSWORD",
    "PUBLIC_URL",
    "RENDER_EXTERNAL_URL",
)
_ROUTE_RE = re.compile(r"/[A-Za-z0-9_/?=&.-]{0,200}")
_TAG_RE = re.compile(r"<[^>]*>")
_SPACES_RE = re.compile(r"\s+")
ECHO_TEXT_LIMIT = 100


class DevConfigError(Exception):
    """Недопустимые параметры dev-сервера: выход с кодом 2 и понятным текстом."""


# --- Окружение и база --------------------------------------------------------------------------------------


def default_db_path() -> Path:
    """Файл базы по умолчанию: <временная папка>/kpi_webapp_dev.db."""
    return Path(tempfile.gettempdir()) / DB_FILE_NAME


def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def resolve_db_path(raw: str | None) -> Path:
    """``--db`` -> путь файла SQLite. Отказ (DevConfigError): адрес базы вместо файла (в т.ч. PostgreSQL)
    или база бота ``data/bot.db`` — dev-сервер её не трогает."""
    if raw is None or not raw.strip():
        return default_db_path()
    text = raw.strip()
    if "://" in text or text.lower().startswith(("postgres:", "postgresql")):
        raise DevConfigError(
            "Dev-сервер работает только с отдельным файлом SQLite: укажите путь к файлу, а не адрес базы "
            "(PostgreSQL не поддерживается)."
        )
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if _same_file(path, PROJECT_DB):
        raise DevConfigError(
            "Это база бота (data/bot.db) — dev-сервер её не трогает. Укажите другой файл или не задавайте --db."
        )
    return path


def dev_environment(db_path: Path, *, real_telegram: bool = False) -> dict[str, str]:
    """Переменные окружения dev-сервера: polling (без webhook), Mini App в режиме отладки, своя база.
    Без --real-telegram — тестовый токен, AI выключен, ключи пустые (значения из .env не используются)."""
    env = dict.fromkeys(_BLANK_ENV, "")
    env.update(
        {
            "RUN_MODE": "polling",
            "TAKEOVER_WEBHOOK": "0",
            "WEBAPP_ENABLED": "1",
            "WEBAPP_DEBUG": "1",
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}",
        }
    )
    if real_telegram:
        # Токен и AI — из .env (явный флаг); пароль базы и публичный адрес по-прежнему не нужны.
        return env
    env.update({"BOT_TOKEN": DEV_TOKEN, "AI_PROVIDER": "none", "ADMIN_IDS": str(DEMO_MANAGER.tg_id)})
    return env


def apply_environment(env: Mapping[str, str]) -> None:
    """Выставить окружение и сбросить кэш настроек (get_settings читает его заново)."""
    os.environ.update(env)
    from bot.config import get_settings

    get_settings.cache_clear()


# --- Фейковый Telegram ----------------------------------------------------------------------------------


def _fake_session_class() -> type[Any]:
    """FakeSession из tests/e2e/fakebot.py (tests добавляется в sys.path)."""
    tests_dir = REPO_ROOT / "tests"
    if not (tests_dir / "e2e" / "fakebot.py").is_file():
        raise DevConfigError(
            "Нет tests/e2e/fakebot.py (фейковый Telegram): dev-сервер работает только из исходников проекта."
        )
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    from e2e.fakebot import FakeSession

    return FakeSession  # type: ignore[no-any-return]


def describe_call(method: Any, error: BaseException | None = None) -> str:
    """Строка для консоли: метод, чат и начало текста (HTML убран)."""
    name = type(method).__name__
    chat = getattr(method, "chat_id", None)
    text = getattr(method, "text", None) or getattr(method, "caption", None) or ""
    snippet = _SPACES_RE.sub(" ", html.unescape(_TAG_RE.sub("", str(text)))).strip()
    if len(snippet) > ECHO_TEXT_LIMIT:
        snippet = snippet[: ECHO_TEXT_LIMIT - 1] + "…"
    line = f"Telegram ← {name}" + (f" chat={chat}" if chat is not None else "")
    if snippet:
        line += f": {snippet}"
    if error is not None:
        line += f"  [ошибка: {type(error).__name__}]"
    return line


def _console(line: str) -> None:
    print(line, flush=True)  # сразу в консоль, даже если вывод перенаправлен в файл


def make_fake_session(echo: Callable[[str], None] | None = _console) -> Any:
    """FakeSession, которая печатает каждый запрос бота к «Telegram» (echo=None — молча)."""
    base = _fake_session_class()

    class EchoSession(base):  # type: ignore[misc, valid-type]
        async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
            try:
                return await super().make_request(bot, method, timeout)
            finally:
                if echo is not None:
                    call = self.calls[-1] if self.calls else None
                    echo(describe_call(method, call.error if call is not None else None))

    return EchoSession()


# --- Приложение: /app, /api и /dev ------------------------------------------------------------------------

_SESSIONMAKER: web.AppKey[Any] = web.AppKey("kpi_dev_sessionmaker")
_TOKEN: web.AppKey[str] = web.AppKey("kpi_dev_token", str)
_KEY: web.AppKey[str] = web.AppKey("kpi_dev_key", str)
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_ROLE_TEXT = {"manager": "начальник", "employee": "сотрудник"}
_STATUS_TEXT = {"active": "активен", "pending": "заявка", "blocked": "заблокирован"}
_MANAGER_LINKS = (("📊 Команда", "/team"), ("📋 Задачи", "/tasks"), ("📝 Проверка", "/review"), ("➕ Новая", "/new"))
_EMPLOYEE_LINKS = (("📋 Мои задачи", "/my"), ("📤 Сдать", "/submit"), ("📈 Мой KPI", "/kpi"), ("➕ Поручение", "/propose"))
GUEST_TG_ID = 999_000_001  # «войти как незарегистрированный»: такого пользователя в базе нет

_PAGE_CSS = (
    "body{font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;margin:0;"
    "padding:16px;background:#f1f2f5;color:#111418}main{max-width:720px;margin:0 auto}"
    "h1{font-size:22px}li{background:#fff;border-radius:12px;padding:12px 14px;margin:8px 0;list-style:none}"
    "ul{padding:0}a{color:#1f6fd1}small{color:#5c6570}.links a{margin-right:12px;white-space:nowrap}"
    "@media (prefers-color-scheme:dark){body{background:#0e1621;color:#f5f5f5}li{background:#17212b}"
    "a{color:#6ab3f3}small{color:#9aa7b4}}"
)


def login_url(tg_id: int, route: str | None = None, key: str | None = None) -> str:
    """Адрес входа dev-сервера: /dev/login?tg_id=…[&to=/route][&key=…]."""
    url = f"/dev/login?tg_id={tg_id}"
    if route:
        url += f"&to={quote(route, safe='/')}"
    return url + (f"&key={quote(key, safe='')}" if key else "")


def index_url(key: str | None = None) -> str:
    """Адрес страницы входа: /dev/[?key=…]."""
    return "/dev/" + (f"?key={quote(key, safe='')}" if key else "")


def access_key(app: web.Application) -> str | None:
    """Ключ этого запуска для /dev/ и /dev/login (None — не нужен: тестовый токен)."""
    return app.get(_KEY)


@web.middleware
async def _local_host_only(request: web.Request, handler: Any) -> web.StreamResponse:
    """Только запросы к 127.0.0.1 / localhost: чужое имя в Host — это страница другого сайта, которая
    обращается к dev-серверу через подмену DNS (DNS rebinding)."""
    try:
        host = (request.url.host or "").strip("[]").lower()
    except ValueError:
        host = ""
    if host not in _LOCAL_HOSTS:
        return web.Response(status=403, text="Dev-сервер отвечает только на 127.0.0.1 и localhost.")
    return await handler(request)


def _key_ok(request: web.Request) -> bool:
    """Ключ запуска (если он нужен) передан и совпадает."""
    key = request.app.get(_KEY)
    return key is None or secrets.compare_digest((request.query.get("key") or "").encode(), key.encode())


_NO_KEY_TEXT = (
    "Нужен ключ запуска: откройте ссылку /dev/?key=… из консоли dev-сервера "
    "(с настоящим токеном бота вход без ключа закрыт)."
)


async def _dev_root(request: web.Request) -> web.Response:
    raise web.HTTPFound(index_url(request.query.get("key") or None))


async def _dev_index(request: web.Request) -> web.Response:
    """Простая страница: пользователи базы и ссылки «войти как …» (данные — только через html.escape)."""
    from sqlalchemy import select

    from bot.db.models import User

    if not _key_ok(request):
        return web.Response(status=403, text=_NO_KEY_TEXT)
    key = request.app.get(_KEY)
    async with request.app[_SESSIONMAKER]() as session:
        users = list(await session.scalars(select(User)))
    users.sort(key=lambda u: (u.role != "manager", u.status != "active", u.full_name, u.id))
    items: list[str] = []
    for user in users:
        role = _ROLE_TEXT.get(str(user.role), str(user.role))
        status = _STATUS_TEXT.get(str(user.status), str(user.status))
        links = [f'<a href="{html.escape(login_url(user.tg_id, key=key))}">войти как {html.escape(user.short_name)}</a>']
        if user.status == "active":
            routes = _MANAGER_LINKS if user.role == "manager" else _EMPLOYEE_LINKS
            links += [
                f'<a href="{html.escape(login_url(user.tg_id, route, key))}">{html.escape(text)}</a>'
                for text, route in routes
            ]
        items.append(
            f"<li><b>{html.escape(user.full_name)}</b> <small>· {html.escape(user.position or '—')} · {role} · "
            f"{status} · tg_id {user.tg_id}</small><div class=\"links\">{' '.join(links)}</div></li>"
        )
    if not items:
        items.append("<li>В базе пока нет пользователей. Перезапустите с <code>--seed-demo</code>.</li>")
    if key is None:  # с настоящим токеном вход только за пользователей dev-базы
        items.append(
            f'<li><a href="{html.escape(login_url(GUEST_TG_ID))}">войти как незарегистрированный</a> '
            "<small>· экран «Нужна регистрация»</small></li>"
        )
    body = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Mini App · отладка</title><style>{_PAGE_CSS}</style></head><body><main>"
        "<h1>Mini App · локальная отладка</h1>"
        "<p><small>Telegram здесь фейковый: уведомления печатаются в консоль dev-сервера. AI выключен — "
        "оценки по правилам.</small></p>"
        f"<ul>{''.join(items)}</ul></main></body></html>"
    )
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store"})


def _init_names(user: Any) -> tuple[str, str | None]:
    """Имя и фамилия для initData из ФИО базы («Иванов Иван Иванович» -> «Иван», «Иванов»)."""
    if user is None:
        return "Гость", None
    parts = str(user.full_name).split()
    if len(parts) > 1:
        return parts[1], parts[0]
    return (parts[0] if parts else "Гость"), None


async def _dev_login(request: web.Request) -> web.Response:
    """302 на /app?tg_debug_init=<свежий подписанный initData>[#route]."""
    from sqlalchemy import select

    from bot.db.models import User
    from bot.webapp.auth import sign_init_data

    if not _key_ok(request):
        return web.Response(status=403, text=_NO_KEY_TEXT)
    raw = (request.query.get("tg_id") or "").strip()
    if not raw.isdigit() or not 0 < int(raw) < 2**63:
        return web.Response(status=400, text="tg_id: укажите положительное целое число")
    tg_id = int(raw)
    route = (request.query.get("to") or "").strip()
    if route and not _ROUTE_RE.fullmatch(route):
        return web.Response(status=400, text="to: маршрут приложения, например /team или /task/12")
    async with request.app[_SESSIONMAKER]() as session:
        user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None and request.app.get(_KEY) is not None:
        # Настоящий токен: подпись годится и для рабочего бота — только пользователи dev-базы.
        return web.Response(status=404, text="В базе dev-сервера нет пользователя с таким tg_id.")
    first, last = _init_names(user)
    init_data = sign_init_data(request.app[_TOKEN], tg_id=tg_id, first_name=first, last_name=last)
    location = "/app?tg_debug_init=" + quote(init_data, safe="") + (f"#{route}" if route else "")
    raise web.HTTPFound(location, headers={"Cache-Control": "no-store"})


def build_dev_app(
    *,
    bot: Bot,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    key: str | None = None,
) -> web.Application:
    """web.Application + setup_webapp (/app, /api) + маршруты отладки /dev (только здесь, не в setup_webapp).
    Сеть и база при сборке не нужны.

    Защита входа: запросы с чужим именем в Host — 403 (DNS rebinding). С настоящим токеном бота
    (не ``DEV_TOKEN``; ``--real-telegram``) подписанный initData годится и для рабочего бота, поэтому
    /dev/ и /dev/login требуют ключ этого запуска (``key``; не задан — случайный, ``access_key(app)``),
    а войти можно только за пользователя dev-базы.
    """
    from bot.webapp import setup_webapp

    if not settings.webapp_debug_active:
        raise DevConfigError("Dev-сервер работает только в режиме отладки: WEBAPP_DEBUG=1 и RUN_MODE=polling.")
    token = settings.bot_token.strip()
    app = web.Application(middlewares=[_local_host_only])
    setup_webapp(app, bot=bot, sessionmaker=sessionmaker, settings=settings)
    app[_SESSIONMAKER] = sessionmaker
    app[_TOKEN] = token
    if token != DEV_TOKEN:
        app[_KEY] = key or secrets.token_urlsafe(18)
    app.router.add_get("/", _dev_root)
    app.router.add_get("/dev", _dev_root)
    app.router.add_get("/dev/", _dev_index)
    app.router.add_get("/dev/login", _dev_login)
    return app


# --- Демо-данные (--seed-demo) ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoPerson:
    tg_id: int
    full_name: str
    position: str | None


DEMO_MANAGER = DemoPerson(1001, "Петрова Анна Сергеевна", "Начальник отдела")
DEMO_EMPLOYEES = (
    DemoPerson(2001, "Иванов Иван Иванович", "Юрист"),
    DemoPerson(2002, "Сидоров Пётр Ильич", "Экономист"),
    DemoPerson(2003, "Кузнецова Анна Сергеевна", "Аналитик"),
)
DEMO_APPLICANT = DemoPerson(2004, "Смирнов Олег Викторович", "Менеджер по продажам")
# Ещё два сотрудника для «большой» демо-команды (seed_demo(extended=True)): лидер по KPI и новичок без оценок.
EXTRA_EMPLOYEES = (
    DemoPerson(2005, "Ахмедова Дилноза Рустамовна", "Бухгалтер"),
    DemoPerson(2006, "Каримов Тимур Бахтиёрович", "Специалист по закупкам"),
)

# Тексты задач: (название, ожидаемый результат, план, единица).
_Text = tuple[str, str, float | None, str | None]


@dataclass(frozen=True)
class _Done:
    text: _Text
    week: int  # смещение недели срока: 0 — текущая, -1 — прошлая …
    score: float  # окончательная оценка начальника
    weight: int
    late: bool = False


@dataclass(frozen=True)
class _Profile:
    """Набор задач сотрудника (описанный в §4.6 + история оценок для графика)."""

    active: _Text
    new: _Text
    overdue: _Text
    rework: _Text
    rework_fact: float | None
    rework_comment: str
    submitted: _Text
    submitted_fact: float | None
    submitted_file: str
    done: tuple[_Done, ...]
    proposed: _Text
    cancelled: _Text
    overdue_weight: int = 15


_PROFILES: dict[int, _Profile] = {
    2001: _Profile(
        active=("Договор аренды склада", "Подготовить и согласовать с арендодателем проект договора аренды склада", None, None),
        new=("Проверка доверенностей филиалов", "Проверить 40 доверенностей филиалов и составить перечень просроченных", 40, "доверенностей"),
        overdue=("Ответ на претензию ООО «Восток-Трейд»", "Направить контрагенту мотивированный ответ на претензию", None, None),
        rework=("Анализ договоров поставщиков", "Проверить 100 договоров поставщиков и представить отчёт о нарушениях", 100, "договоров"),
        rework_fact=80,
        rework_comment="Не хватает проверки оставшихся 20 договоров и выводов по рискам. Дополните отчёт.",
        submitted=("Обновление типовых форм договоров", "Обновить 12 типовых форм договоров под новые требования закона", 12, "форм"),
        submitted_fact=12,
        submitted_file="Типовые_формы_2026.docx",
        done=(
            _Done(("Претензионная работа за сентябрь", "Закрыть 20 претензий клиентов за сентябрь", 20, "претензий"), 0, 110, 25),
            _Done(("Экспертиза кредитного договора", "Дать заключение по рискам кредитного договора с банком", None, None), 0, 90, 20),
            _Done(("Архив судебных дел", "Оцифровать и разложить по делам 150 судебных материалов", 150, "дел"), -1, 100, 30),
            _Done(("Проверка договоров аренды", "Проверить 30 договоров аренды на соответствие закону", 30, "договоров"), -2, 105, 20),
            _Done(("Согласование договоров поставки", "Согласовать 25 договоров поставки", 25, "договоров"), -5, 95, 25),
        ),
        proposed=("Памятка по электронному документообороту", "Подготовить памятку для отдела продаж по правилам ЭДО", None, None),
        cancelled=("Регистрация товарного знака", "Подать заявку на регистрацию товарного знака", None, None),
        overdue_weight=15,
    ),
    2002: _Profile(
        active=("Бюджет отдела на IV квартал", "Подготовить проект бюджета отдела на IV квартал с пояснительной запиской", None, None),
        new=("Себестоимость продукции", "Рассчитать себестоимость 15 позиций продукции", 15, "позиций"),
        overdue=("Отчёт о движении денежных средств", "Сформировать отчёт о движении денежных средств за сентябрь", None, None),
        rework=("Сверка расчётов с поставщиками", "Провести сверку расчётов с 60 поставщиками", 60, "поставщиков"),
        rework_fact=45,
        rework_comment="Сверка неполная: нет актов по 15 поставщикам. Приложите подписанные акты.",
        submitted=("Прогноз выручки на ноябрь", "Подготовить прогноз выручки по 8 направлениям", 8, "направлений"),
        submitted_fact=8,
        submitted_file="Прогноз_выручки_ноябрь.xlsx",
        done=(
            _Done(("Дебиторская задолженность", "Проанализировать задолженность 50 клиентов", 50, "клиентов"), 0, 100, 30),
            _Done(("Премиальный фонд за III квартал", "Рассчитать премиальный фонд за III квартал", None, None), 0, 110, 20),
            _Done(("Сверка с банком", "Сверить 120 банковских операций за месяц", 120, "операций"), -1, 90, 25, late=True),
            _Done(("Свод затрат за квартал", "Свести затраты по 12 статьям", 12, "статей"), -3, 100, 20),
            _Done(("План закупок на октябрь", "Согласовать план закупок с начальниками направлений", None, None), -6, 85, 25),
        ),
        proposed=("Сравнение тарифов банков", "Сравнить тарифы 5 банков на расчётное обслуживание", 5, "банков"),
        cancelled=("Аудит командировочных расходов", "Проверить авансовые отчёты за август", None, None),
        overdue_weight=5,
    ),
    2003: _Profile(
        active=("Дашборд продаж для руководства", "Собрать дашборд продаж по регионам с обновлением раз в неделю", None, None),
        new=("Опрос удовлетворённости клиентов", "Провести опрос 200 клиентов и подготовить сводку", 200, "клиентов"),
        overdue=("Анализ оттока клиентов", "Определить причины оттока клиентов за III квартал", None, None),
        rework=("Сегментация клиентской базы", "Разделить 1 200 клиентов на сегменты по выручке", 1200, "клиентов"),
        rework_fact=900,
        rework_comment="Сегменты пересекаются, а 300 клиентов не распределены. Уточните правила разбиения.",
        submitted=("Анализ конкурентов", "Сравнить цены и ассортимент 10 конкурентов", 10, "конкурентов"),
        submitted_fact=11,
        submitted_file="Анализ_конкурентов.pdf",
        done=(
            _Done(("Еженедельный отчёт по продажам", "Подготовить отчёт по продажам за неделю", None, None), 0, 90, 20),
            _Done(("Прогноз спроса на ноябрь", "Спрогнозировать спрос по 25 товарным группам", 25, "групп"), -1, 110, 20),
            _Done(("Чистка CRM", "Удалить дубли и исправить 300 карточек клиентов", 300, "карточек"), 0, 100, 15),
            _Done(("Отчёт по остаткам", "Проверить остатки по 6 складам", 6, "складов"), -4, 110, 20),
            _Done(("Анализ акции «Осень»", "Оценить эффект акции по 4 регионам", 4, "регионам"), -7, 95, 25),
        ),
        proposed=("Автоматизация отчёта по остаткам", "Настроить автоматическую выгрузку остатков по складам", None, None),
        cancelled=("Исследование рынка Казахстана", "Подготовить обзор рынка Казахстана", None, None),
        overdue_weight=25,
    ),
}

_AI_MODEL = "gemini-3.6-flash"
_AI_RATIONALE = (
    "Сверены все 45 счетов из плана, расхождения по трём счетам описаны с причинами и предложениями по "
    "исправлению. Отчёт сдан до срока, акт сверки приложен. Сверх плана подготовлен реестр закрывающих "
    "документов. Небольшой минус — не указаны сроки устранения расхождений."
)


async def seed_demo(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime | None = None, extended: bool = False
) -> bool:
    """Демо-команда прямо в БД (как tests/perf: записи через ORM). Только в пустой базе: если пользователи
    уже есть — ничего не делает и возвращает False.

    Начальник 1001; сотрудники 2001–2003 (у каждого: принятая и непринятая задачи в работе, просроченная,
    на доработке, на проверке с оценкой по правилам и файлом, три выполненные с оценками 90/100/110 на этой и
    прошлой неделях, поручение на подтверждении, отменённая — и две оценённые задачи в прошлых неделях);
    заявка 2004. extended — ещё сотрудники 2005 (лидер, оценка AI с обоснованием) и 2006 (новичок).
    """
    from sqlalchemy import func, select

    from bot.db.models import Role, User, UserStatus
    from bot.utils.dates import utcnow

    now = now or utcnow()
    async with sessionmaker() as session:
        if await session.scalar(select(func.count()).select_from(User)):
            return False
        manager = User(
            tg_id=DEMO_MANAGER.tg_id,
            full_name=DEMO_MANAGER.full_name,
            position=DEMO_MANAGER.position,
            role=Role.MANAGER,
            status=UserStatus.ACTIVE,
            created_at=now - timedelta(days=90),
        )
        people = [*DEMO_EMPLOYEES, *(EXTRA_EMPLOYEES if extended else ())]
        staff = {
            person.tg_id: User(
                tg_id=person.tg_id,
                full_name=person.full_name,
                position=person.position,
                role=Role.EMPLOYEE,
                status=UserStatus.ACTIVE,
                created_at=now - timedelta(days=80),
            )
            for person in people
        }
        applicant = User(
            tg_id=DEMO_APPLICANT.tg_id,
            full_name=DEMO_APPLICANT.full_name,
            position=DEMO_APPLICANT.position,
            role=Role.EMPLOYEE,
            status=UserStatus.PENDING,
            created_at=now - timedelta(hours=3),
        )
        session.add_all([manager, *staff.values(), applicant])
        await session.flush()
        seeder = _Seeder(session, manager, now)
        for tg_id, profile in _PROFILES.items():
            await seeder.employee(staff[tg_id], profile)
        if extended:
            await seeder.leader(staff[EXTRA_EMPLOYEES[0].tg_id])
            await seeder.newcomer(staff[EXTRA_EMPLOYEES[1].tg_id])
        await session.commit()
    return True


class _Seeder:
    """Задачи, сдачи, файлы и журнал демо-команды. Время — относительно now (naive UTC)."""

    def __init__(self, session: AsyncSession, manager: Any, now: datetime) -> None:
        from bot.services import periods

        self.session = session
        self.manager = manager
        self.now = now
        self._week = lambda offset: periods.get_period("week", offset, now)

    # --- Сроки ---------------------------------------------------------------------------------------------

    def _deadline_in_week(self, offset: int, fraction: float) -> datetime:
        """Срок внутри недели offset; у текущей недели — до «сейчас» (задача уже оценена)."""
        week = self._week(offset)
        end = min(week.end, self.now - timedelta(minutes=30)) if offset == 0 else week.end
        span = max(end - week.start, timedelta(minutes=10))
        moment = week.start + span * fraction
        return moment.replace(second=0, microsecond=0)

    # --- Записи --------------------------------------------------------------------------------------------

    async def _task(self, assignee: Any, text: _Text, **fields: Any) -> Any:
        from bot.db.models import Priority, Task, TaskSource, TaskStatus

        title, expected, plan_value, plan_unit = text
        source = fields.pop("source", TaskSource.MANAGER)
        employee_source = source == TaskSource.EMPLOYEE
        task = Task(
            title=title,
            expected_result=expected,
            plan_value=plan_value,
            plan_unit=plan_unit,
            priority=fields.pop("priority", Priority.MEDIUM),
            weight=fields.pop("weight", 20),
            status=fields.pop("status", TaskStatus.ACTIVE),
            source=source,
            assignee_id=assignee.id,
            created_by_id=assignee.id if employee_source else self.manager.id,
            manager_id=None if employee_source else self.manager.id,
            rework_count=0,
            **fields,
        )
        task.updated_at = task.created_at
        self.session.add(task)
        await self.session.flush()
        return task

    def _event(self, task: Any, actor: Any, kind: Any, at: datetime, **data: Any) -> None:
        from bot.db.models import TaskEvent

        clean = {key: (value.isoformat() if isinstance(value, datetime) else value) for key, value in data.items()}
        self.session.add(
            TaskEvent(task_id=task.id, actor_id=actor.id if actor is not None else None, type=kind, data=clean, created_at=at)
        )

    def _created(self, task: Any, accepted_by: Any | None = None) -> None:
        from bot.db.models import EventType

        self._event(
            task,
            self.manager,
            EventType.CREATED,
            task.created_at,
            deadline=task.deadline,
            weight=task.weight,
            priority=str(task.priority),
        )
        if accepted_by is not None and task.accepted_at is not None:
            self._event(task, accepted_by, EventType.ACCEPTED, task.accepted_at)

    def _submission(
        self,
        task: Any,
        employee: Any,
        *,
        at: datetime,
        fact_text: str,
        result_text: str | None,
        fact_value: float | None,
        attempt: int = 1,
        files: Sequence[tuple[str, str, str, str, int]] = (),
        ai: tuple[float, str, str, str | None] | None = None,
    ) -> Any:
        """Сдача + файлы + журнал (сдана, оценка). files — (вид, file_id, имя, MIME, размер);
        ai — (оценка, обоснование, источник, модель), None — по правилам (как без AI)."""
        from bot.ai.evaluate import RULES_PREFIX, rules_score
        from bot.db.models import Attachment, AttachmentKind, EventType, Submission

        is_late = at > task.deadline
        late_days = round((at - task.deadline) / timedelta(days=1), 1) if is_late else 0.0
        if ai is None:
            raw_score, explanation = rules_score(task.plan_value, fact_value, late_days)
            # Как tasks.record_evaluation: оценка округляется до целого.
            ai = (float(math.floor(raw_score + 0.5)), RULES_PREFIX + explanation, "rules", None)
        score, rationale, source, model = ai
        attachments = [
            Attachment(
                kind=AttachmentKind(kind),
                file_id=file_id,
                file_unique_id=f"u-{file_id}",
                file_name=name,
                mime_type=mime,
                file_size=size,
                created_at=at,
            )
            for kind, file_id, name, mime, size in files
        ]
        sub = Submission(
            task_id=task.id,
            attempt=attempt,
            fact_text=fact_text,
            result_text=result_text,
            fact_value=fact_value,
            created_at=at,
            deadline_at_submit=task.deadline,
            is_late=is_late,
            late_days=late_days,
            ai_score=score,
            ai_rationale=rationale,
            ai_source=source,
            ai_model=model,
            attachments=attachments,
        )
        self.session.add(sub)
        task.submitted_at = at
        task.ai_score = score
        self._event(task, employee, EventType.SUBMITTED, at, attempt=attempt, is_late=is_late, late_days=late_days, files=len(attachments))
        self._event(task, None, EventType.AI_EVALUATED, at + timedelta(seconds=40), attempt=attempt, score=score, source=source, model=model)
        return sub

    @staticmethod
    def _fact_for(text: _Text, score: float) -> float | None:
        plan = text[2]
        return round(plan * score / 100) if plan is not None else None

    @staticmethod
    def _fact_text(text: _Text, fact: float | None) -> str:
        plan, unit = text[2], text[3] or ""
        if fact is None or plan is None:
            return "Работа выполнена в полном объёме, итоговый документ согласован и приложен."

        def number(value: float) -> str:
            return f"{value:,.0f}".replace(",", " ")

        return f"Выполнено: {number(fact)} {unit} при плане {number(plan)}. Итоги — в отчёте.".replace("  ", " ")

    # --- Наборы задач --------------------------------------------------------------------------------------

    async def employee(self, emp: Any, p: _Profile) -> None:
        """Набор §4.6 для одного сотрудника + оценённые задачи прошлых недель (для графика)."""
        from bot.db.models import EventType, Priority, ReviewDecision, TaskSource, TaskStatus

        now = self.now
        # В работе, принята.
        task = await self._task(
            emp, p.active, deadline=now + timedelta(days=2), weight=25, priority=Priority.HIGH,
            created_at=now - timedelta(days=3), accepted_at=now - timedelta(days=2, hours=20),
        )
        self._created(task, emp)
        # В работе, ещё не принята.
        task = await self._task(emp, p.new, deadline=now + timedelta(days=4), weight=15, created_at=now - timedelta(hours=2))
        self._created(task)
        # Просрочена.
        task = await self._task(
            emp, p.overdue, deadline=(now - timedelta(days=1)).replace(second=0, microsecond=0), weight=p.overdue_weight,
            priority=Priority.HIGH, created_at=now - timedelta(days=6), accepted_at=now - timedelta(days=5, hours=22),
        )
        self._created(task, emp)
        # На доработке: первая сдача возвращена с комментарием.
        task = await self._task(
            emp, p.rework, deadline=now + timedelta(days=2), weight=20, status=TaskStatus.REWORK,
            created_at=now - timedelta(days=7), accepted_at=now - timedelta(days=6, hours=21),
        )
        self._created(task, emp)
        sub = self._submission(
            task, emp, at=now - timedelta(days=1, hours=2), fact_text=self._fact_text(p.rework, p.rework_fact),
            result_text="Промежуточный отчёт приложен.", fact_value=p.rework_fact,
            files=[("document", "demo-doc-rework", "Промежуточный_отчёт.pdf", "application/pdf", 182_400)],
        )
        sub.decision = ReviewDecision.REWORK
        sub.review_comment = p.rework_comment
        sub.reviewer_id = self.manager.id
        sub.reviewed_at = now - timedelta(hours=20)
        task.rework_count = 1
        self._event(task, self.manager, EventType.REWORK, sub.reviewed_at, comment=p.rework_comment)
        # На проверке: оценка по правилам с обоснованием и один документ.
        task = await self._task(
            emp, p.submitted, deadline=now + timedelta(days=1), weight=20, priority=Priority.HIGH,
            status=TaskStatus.SUBMITTED, created_at=now - timedelta(days=6), accepted_at=now - timedelta(days=5, hours=23),
        )
        self._created(task, emp)
        self._submission(
            task, emp, at=now - timedelta(hours=3), fact_text=self._fact_text(p.submitted, p.submitted_fact),
            result_text="Документ согласован с финансовым отделом, замечаний нет.", fact_value=p.submitted_fact,
            files=[("document", "demo-doc-1", p.submitted_file, "application/octet-stream", 245_760)],
        )
        # Выполненные с оценками (этой и прошлых недель).
        for index, done in enumerate(p.done):
            await self._done(emp, done, index)
        # Поручение сотрудника на подтверждении.
        task = await self._task(
            emp, p.proposed, deadline=now + timedelta(days=6), weight=10, status=TaskStatus.PROPOSED,
            source=TaskSource.EMPLOYEE, created_at=now - timedelta(hours=5),
        )
        self._event(task, emp, EventType.PROPOSED, task.created_at, deadline=task.deadline)
        # Отменена.
        task = await self._task(
            emp, p.cancelled, deadline=now + timedelta(days=3), weight=10, priority=Priority.LOW,
            status=TaskStatus.CANCELLED, created_at=now - timedelta(days=4), accepted_at=now - timedelta(days=3, hours=20),
        )
        self._created(task, emp)
        self._event(task, self.manager, EventType.CANCELLED, now - timedelta(days=1), reason="Задача больше не актуальна")

    async def _done(self, emp: Any, done: _Done, index: int) -> Any:
        from bot.db.models import EventType, Priority, ReviewDecision, TaskStatus

        fraction = (0.35, 0.6, 0.8, 0.5, 0.45)[index % 5]
        deadline = self._deadline_in_week(done.week, fraction)
        submitted_at = deadline + timedelta(hours=6) if done.late else deadline - timedelta(hours=4)
        task = await self._task(
            emp, done.text, deadline=deadline, weight=done.weight, priority=(Priority.MEDIUM, Priority.HIGH, Priority.LOW)[index % 3],
            status=TaskStatus.DONE, created_at=deadline - timedelta(days=5), accepted_at=deadline - timedelta(days=4, hours=20),
        )
        self._created(task, emp)
        fact = self._fact_for(done.text, done.score)
        sub = self._submission(
            task, emp, at=submitted_at, fact_text=self._fact_text(done.text, fact), result_text=None, fact_value=fact,
            files=[("document", f"demo-done-{task.id}", f"Отчёт_{task.id}.pdf", "application/pdf", 96_000 + task.id * 1_000)] if index % 2 == 0 else (),
        )
        reviewed = submitted_at + timedelta(hours=2)
        sub.final_score = float(done.score)
        sub.reviewer_id = self.manager.id
        sub.reviewed_at = reviewed
        task.final_score = float(done.score)
        task.completed_at = reviewed
        if sub.ai_score == done.score:
            sub.decision = ReviewDecision.APPROVED
            self._event(task, self.manager, EventType.SCORE_CONFIRMED, reviewed, score=done.score)
        else:
            sub.decision = ReviewDecision.CHANGED
            sub.review_comment = "Сделано больше, чем требовалось, — оценка повышена." if done.score > (sub.ai_score or 0) else "Есть замечания к качеству — оценка снижена."
            self._event(task, self.manager, EventType.SCORE_CHANGED, reviewed, ai_score=sub.ai_score, score=done.score, comment=sub.review_comment)
        return task

    async def leader(self, emp: Any) -> None:
        """Сотрудник-лидер: высокие оценки и сдача на проверке с оценкой AI (обоснование, модель, фото + PDF)."""
        from bot.db.models import Priority, TaskStatus

        now = self.now
        for index, done in enumerate(
            (
                _Done(("Закрытие месяца: сентябрь", "Закрыть 45 счетов за сентябрь", 45, "счетов"), 0, 120, 30),
                _Done(("Начисление зарплаты", "Начислить зарплату 64 сотрудникам без ошибок", 64, "сотрудникам"), -1, 115, 25),
                _Done(("Инвентаризация основных средств", "Провести инвентаризацию 210 объектов", 210, "объектов"), -2, 110, 20),
                _Done(("Отчётность по НДС", "Сдать декларацию по НДС за квартал", None, None), -4, 120, 25),
                _Done(("Акты сверки с клиентами", "Подписать 30 актов сверки", 30, "актов"), -6, 105, 20),
            )
        ):
            await self._done(emp, done, index)
        task = await self._task(
            emp, ("Сверка счетов с контрагентами", "Сверить 45 счетов с контрагентами и описать расхождения", 45, "счетов"),
            deadline=now + timedelta(days=2), weight=25, priority=Priority.HIGH, status=TaskStatus.SUBMITTED,
            created_at=now - timedelta(days=5), accepted_at=now - timedelta(days=4, hours=22),
        )
        self._created(task, emp)
        self._submission(
            task, emp, at=now - timedelta(hours=1, minutes=20),
            fact_text="Сверены все 45 счетов, по трём найдены расхождения — причины и исправления описаны в отчёте.",
            result_text="Подготовлен реестр закрывающих документов и акт сверки, согласован с контрагентами.",
            fact_value=45,
            files=[
                ("photo", "demo-photo-1", "Акт_сверки.jpg", "image/jpeg", 1_843_200),
                ("document", "demo-doc-2", "Реестр_документов.pdf", "application/pdf", 512_000),
            ],
            ai=(105.0, _AI_RATIONALE, "ai", _AI_MODEL),
        )
        task = await self._task(
            emp, ("Налоговый календарь на ноябрь", "Составить календарь платежей и отчётов на ноябрь", None, None),
            deadline=now + timedelta(days=5), weight=10, created_at=now - timedelta(days=1), accepted_at=now - timedelta(hours=20),
        )
        self._created(task, emp)

    async def newcomer(self, emp: Any) -> None:
        """Новичок: оценённых задач нет (KPI «нет данных»), одна задача ещё не принята."""
        from bot.db.models import Priority

        now = self.now
        task = await self._task(
            emp, ("Тендер на упаковку", "Провести тендер среди 5 поставщиков упаковки и выбрать лучшее предложение", 5, "поставщиков"),
            deadline=now + timedelta(days=3), weight=30, priority=Priority.HIGH,
            created_at=now - timedelta(days=1), accepted_at=now - timedelta(hours=22),
        )
        self._created(task, emp)
        task = await self._task(
            emp, ("Реестр поставщиков", "Обновить реестр 80 поставщиков: контакты, условия оплаты, сроки поставки", 80, "поставщиков"),
            deadline=now + timedelta(days=9), weight=20, created_at=now - timedelta(minutes=40),
        )
        self._created(task)


# --- Запуск -------------------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m bot.webapp.dev",
        description="Dev-сервер Mini App для проверки в браузере (только 127.0.0.1, своя база SQLite, фейковый Telegram).",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"порт (по умолчанию {DEFAULT_PORT})")
    parser.add_argument("--db", default=None, help=f"файл SQLite (по умолчанию <temp>/{DB_FILE_NAME})")
    parser.add_argument("--seed-demo", action="store_true", help="заполнить пустую базу демо-командой")
    parser.add_argument(
        "--fake-voice", action="store_true", help="голосовой ввод без AI: запись «распознаётся» в заданный текст"
    )
    parser.add_argument(
        "--real-telegram", action="store_true", help="настоящий Telegram с токеном из .env (уведомления уйдут людям!)"
    )
    return parser.parse_args(argv)


FAKE_VOICE_TEXT = "Проверить 100 договоров поставщиков и подготовить отчёт о нарушениях"


def enable_fake_voice() -> None:
    """Подставной распознаватель речи для проверки интерфейса: AI не нужен, ответ всегда один и тот же."""
    from datetime import timedelta

    from bot.ai import dictate
    from bot.utils.dates import to_local, utcnow

    async def fake_generate_json(**kwargs: Any) -> tuple[dict[str, Any], str]:
        if "transcript" not in kwargs["schema"]["properties"]:
            return {"text": FAKE_VOICE_TEXT, "language": "ru"}, "fake-voice"
        deadline = to_local(utcnow() + timedelta(days=3)).strftime("%Y-%m-%dT18:00")
        return {
            "transcript": FAKE_VOICE_TEXT,
            "assignee_id": None,
            "title": "Проверка договоров поставщиков",
            "expected_result": "Проверить 100 договоров поставщиков и представить отчёт о нарушениях",
            "plan_value": 100,
            "plan_unit": "договоров",
            "deadline": deadline,
        }, "fake-voice"

    dictate.ai_available = lambda: True  # type: ignore[assignment]
    dictate.generate_json = fake_generate_json  # type: ignore[assignment]


async def serve(args: argparse.Namespace, db_path: Path) -> None:
    """Поднять сервер и ждать Ctrl+C."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties

    from bot.config import get_settings
    from bot.db.base import init_db, make_engine, make_sessionmaker

    settings = get_settings()
    engine = make_engine(settings.database_url)
    bot: Bot | None = None
    runner: web.AppRunner | None = None
    try:
        await init_db(engine)
        sessionmaker = make_sessionmaker(engine)
        if args.seed_demo:
            seeded = await seed_demo(sessionmaker)
            print("Демо-данные созданы." if seeded else "В базе уже есть пользователи — демо-данные не добавлены.", flush=True)
        if getattr(args, "fake_voice", False):
            enable_fake_voice()
            print("Голосовой ввод: подставной распознаватель (--fake-voice).", flush=True)
        session = None if args.real_telegram else make_fake_session()
        bot = Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
        app = build_dev_app(bot=bot, sessionmaker=sessionmaker, settings=settings)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, HOST, args.port).start()
        print(f"База: {db_path}", flush=True)
        print(f"Mini App (отладка): http://{HOST}:{args.port}{index_url(access_key(app))}", flush=True)
        await asyncio.Event().wait()
    finally:
        if runner is not None:
            await runner.cleanup()
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if not 0 < args.port < 65536:
            raise DevConfigError("--port: число от 1 до 65535")
        db_path = resolve_db_path(args.db)
        apply_environment(dev_environment(db_path, real_telegram=args.real_telegram))
        if not args.real_telegram:
            _fake_session_class()  # проверить заранее, что фейковый Telegram доступен
    except DevConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    with _suppress_encoding_errors():
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        try:
            asyncio.run(serve(args, db_path))
        except KeyboardInterrupt:
            print("Остановлено.")
    return 0


class _suppress_encoding_errors:  # noqa: N801 - контекст-менеджер
    """Консоль Windows без UTF-8: эмодзи в выводе заменяются «?», а не роняют сервер."""

    def __enter__(self) -> None:
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(errors="replace")
                except (OSError, ValueError):
                    pass

    def __exit__(self, *exc: object) -> None:
        return None


if __name__ == "__main__":
    sys.exit(main())
