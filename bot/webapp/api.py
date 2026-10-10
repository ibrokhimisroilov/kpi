"""JSON-API приложения в Telegram: sub-app ``/api`` (docs/MINIAPP_SPEC.md §4.4, §6, §8, §10).

Каждый запрос:

1. ``error_middleware`` — любая ошибка становится JSON ``{"error": "<русский текст>", "code": "<код>"}``
   (§6.2); ко всем ответам — ``Cache-Control: no-store`` и ``X-App-Version``;
2. ``auth_session_middleware`` — проверка подписанного initData (``bot.webapp.auth``); тело запроса
   (JSON, ≤ 1 МиБ) читается целиком за ``BODY_READ_TIMEOUT_SEC`` ДО открытия сессии БД — клиент, не
   приславший тело, не держит соединение пула (кроме сдачи: её multipart читает обработчик потоком и
   после commit); затем одна сессия БД на запрос и пользователь по ``tg_id``
   (``bot.middlewares.load_user`` — та же функция, что у чата); успех — commit (у сессии, которая только
   читала, это не обмен с базой), ошибка — rollback;
3. обработчик: права по записи ``User`` в базе → объект из пути → тело → «один запрос за раз»
   (``UserGate``) и «не чаще N за окно» (``RateLimiter``: подсказка AI, поручения) → тот же сервис, что
   и в чате → commit → те же уведомления ``bot.notify.*``.

Обмены с базой. Каждый обмен с облачной базой — 130–190 мс, поэтому списки, KPI и история читаются
«лёгкими чтениями» (раздел ниже): фиксированное число запросов, независимо от числа строк (столбцы,
без загрузки ORM-связей). Карточки и действия — через сервисы (жадные связи моделей, без N+1). Перед
долгой сетевой работой (AI, загрузка файлов в Telegram) — ``session.commit()``: соединение возвращается
в пул. Фоновые задачи (оценка сдачи, Excel, пересылка файлов) открывают свои сессии.

В журнал не пишутся initData, hash, токен, тела запросов и имена файлов сотрудников.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import os
import re
import tempfile
import time
import unicodedata
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, FSInputFile, Message
from aiohttp import BodyPartReader, hdrs, web
from sqlalchemy import ColumnElement, case, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notify
from bot.ai import dictate, formulate
from bot.ai import provider as ai_provider
from bot.config import get_settings
from bot.db.models import (
    EXCLUDED_FROM_KPI,
    OPEN_STATUSES,
    AttachmentKind,
    Priority,
    Role,
    Submission,
    Task,
    TaskStatus,
    User,
    UserStatus,
)
from bot.middlewares import load_user
from bot.services import export as export_service
from bot.services import kpi, periods, proposal_flow
from bot.services import tasks as tasks_svc
from bot.services import users as users_svc
from bot.services.dbsafe import clip_file_name, is_db_id, non_negative, sql_limit
from bot.services.errors import DomainError
from bot.services.kpi import KpiResult, TaskSnapshot
from bot.utils import dateparse
from bot.utils.dates import to_local, to_utc, utcnow
from bot.utils.text import esc, parse_number
from bot.webapp import CTX, ApiError, WebappContext, auth, pending_tasks, register_webapp
from bot.webapp import serializers as ser
from bot.webapp.serializers import HistoryRowData, TaskRowData

__all__ = [
    "ROUTES",
    "ApiError",
    "active_viewer",
    "build_api_app",
    "count_task_rows",
    "employee_viewer",
    "history_rows",
    "kpi_in",
    "list_task_rows",
    "manager_viewer",
    "me_counts",
    "parse_deadline_input",
    "parse_number_input",
    "pending_tasks",
    "register_webapp",
    "search_task_rows",
    "snapshots_between",
    "status_filter",
    "tab_counts",
]

log = logging.getLogger(__name__)

T = TypeVar("T")
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# --- Константы (§6.4; тесты их подменяют) -----------------------------------------------------------------

MAX_FILES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024  # = лимит скачивания getFile у Bot API: AI потом сможет прочитать файл
MAX_TOTAL_BYTES = 50 * 1024 * 1024  # бережём 512 МБ памяти и 5 ГБ исходящего трафика Render Free
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # лимит sendPhoto; больше — отправляем документом
MAX_TEXT_PART_BYTES = 64 * 1024  # текстовое поле multipart
PAGE_LIMIT_DEFAULT = 20
PAGE_LIMIT_MAX = 50
SEARCH_SCAN_LIMIT = 2000
MAX_BACK_OFFSET = 500  # как dashboard.MAX_BACK_OFFSET
HISTORY_PAGE_SIZE = 10  # как dashboard.HISTORY_PAGE_SIZE
TREND_WEEKS = 8
WEIGHT_OPTIONS = (5, 10, 15, 20, 25, 30, 40, 50)  # как keyboards._WEIGHT_OPTIONS
SCORE_OPTIONS = (50, 70, 80, 90, 100, 110, 120)  # как keyboards._SCORE_OPTIONS
UPLOAD_CONCURRENCY = 3  # файлов сдачи, которые одновременно грузятся в Telegram
UPLOAD_CHUNK_BYTES = 256 * 1024
RETRY_AFTER_MAX_SEC = 60  # флуд-лимит Telegram: дольше не ждём (как notify._call)
FORMULATE_EXTRA_SEC = 10  # запас сверх времени, за которое цепочка AI обязана ответить
# Чтение тела запроса. aiohttp не ограничивает время чтения тела: клиент, приславший заголовки и не
# приславший тело, держал бы обработчик (а без этих сроков — и соединение пула БД) сколько угодно.
BODY_READ_TIMEOUT_SEC = 15.0  # JSON-тело (≤ 1 МиБ) читается целиком ДО открытия сессии БД
UPLOAD_IDLE_TIMEOUT_SEC = 60.0  # сдача: столько можно не присылать ни байта
UPLOAD_MAX_SEC = 15 * 60.0  # сдача: всё тело (до 50 МБ с медленного мобильного интернета) — не дольше
EXTRA_PARTS = 2  # частей формы сдачи сверх MAX_FILES файлов и текстовых полей — запас
# «Не чаще N за окно» (RateLimiter): (сколько, за сколько секунд).
DAY_SEC = 24 * 60 * 60.0
FORMULATE_LIMITS_MANAGER = ((6, 60.0), (60, DAY_SEC))
FORMULATE_LIMITS_EMPLOYEE = ((6, 60.0), (20, DAY_SEC))
# Подсказки AI всей команды за сутки: дальше — правила, чтобы квоты бесплатных моделей оставались оценке сдач.
FORMULATE_TEAM_PER_DAY = 200
PROPOSAL_LIMITS = ((5, 10 * 60.0), (20, DAY_SEC))
# Голосовой ввод (§8.10): запись уходит телом запроса. Размер — с запасом на 2 минуты AAC с iPhone.
VOICE_MAX_BYTES = 6 * 1024 * 1024
VOICE_READ_TIMEOUT_SEC = 60.0  # запись целиком (с медленного мобильного интернета) — не дольше
VOICE_LIMITS = ((12, 60.0), (200, DAY_SEC))  # на человека
VOICE_TEAM_PER_DAY = 800  # всей команды за сутки: дальше — «напишите текстом» (квоты AI нужны оценке сдач)
VOICE_EXTRA_SEC = 10  # запас сверх времени, за которое AI обязан распознать запись

# Лимиты текстов (§6.1) — как в чате.
TITLE_MAX = 255  # tasks._MAX_TITLE_LEN
RESULT_MAX = 2000  # task_propose.RESULT_MAX
UNIT_MAX = 64
PLAN_MAX = 1e15  # task_create.PLAN_MAX
FACT_MIN = 3  # task_submit.MIN_FACT
TEXT_MAX = 3000  # task_submit.MAX_TEXT
NOTES_MAX = 1500  # task_submit.MAX_NOTES
COMMENT_MAX = 2000  # task_review.MAX_COMMENT_LEN
REASON_MAX = 1000  # task_propose.REASON_MAX
QUERY_MAX = 100
RAW_MIN = 3
PREVIOUS_MAX = 1000

# --- Тексты (совпадающие с чатом — константами, без импорта handlers; тест сверяет их с чатом) ------------

NO_RIGHTS = "⛔ Недостаточно прав для этого действия."  # = handlers.common.NO_RIGHTS
NOT_DELIVERED = (  # = handlers.common.NOT_DELIVERED
    "⚠️ Уведомление не доставлено: у сотрудника нет доступа к боту или он заблокировал бота — "
    "сообщите ему лично."
)
TXT_PENDING = (  # = handlers.start.TXT_PENDING
    "⏳ Заявка на рассмотрении у начальника.\nКак только вас подтвердят, придёт уведомление."
)
TXT_BLOCKED = "⛔ Доступ закрыт. Обратитесь к начальнику."  # = handlers.start.TXT_BLOCKED
GENERIC_ERROR = "⚠️ Произошла ошибка, попробуйте ещё раз"  # = main.GENERIC_ERROR
EXPORT_FAILED = "⚠️ Не удалось подготовить отчёт. Попробуйте ещё раз чуть позже."  # = dashboard.EXPORT_FAILED
EXPORT_SEND_FAILED = "⚠️ Не удалось отправить файл. Попробуйте ещё раз."  # = dashboard.EXPORT_SEND_FAILED
NOT_OPEN_TEXTS: dict[TaskStatus, str] = {  # = task_submit._NOT_OPEN_TEXTS
    TaskStatus.SUBMITTED: "📝 Результат уже отправлен и ждёт проверки начальника.",
    TaskStatus.DONE: "✅ Задача уже выполнена и оценена — сдавать результат не нужно.",
    TaskStatus.CANCELLED: "🚫 Задача отменена начальником — сдавать результат не нужно.",
    TaskStatus.PROPOSED: "📥 Поручение ещё не подтверждено начальником — сдать результат можно после подтверждения.",
    TaskStatus.REJECTED: "❌ Поручение отклонено начальником — сдавать результат не нужно.",
}
NOT_OPEN_DEFAULT = "Задача не в работе — сдать результат нельзя."

NOT_REGISTERED = "Вы ещё не зарегистрированы. Откройте чат с ботом и нажмите /start."
MANAGER_ONLY = "Действие доступно только начальнику"
EMPLOYEE_ONLY = "Действие доступно только сотруднику"
TASK_NOT_FOUND = "Задача не найдена."
SUB_NOT_FOUND = "Результат не найден."
USER_NOT_FOUND = "Сотрудник не найден."
NOT_FOUND = "Не найдено."
METHOD_NOT_ALLOWED = "Метод не поддерживается."
TOO_LARGE = "Слишком большой запрос."
BAD_REQUEST = "Неверный запрос."
BAD_JSON = "Неверный запрос: ожидается JSON-объект"
NOT_MULTIPART = "Ожидается multipart/form-data"
BAD_FORM = "Неверный запрос: не удалось прочитать данные формы"
NO_CHANGES = "Нет изменений"
PROPOSAL_FIELDS = "Вес и приоритет назначаются при подтверждении поручения"
DEADLINE_FORMAT = "Срок: укажите дату в формате ГГГГ-ММ-ДД"
DEADLINE_PASSED = "Текущий срок уже прошёл — укажите новый."
NO_FILES = "У этого результата нет файлов."
FACT_TOO_SHORT = "Опишите чуть подробнее, пожалуйста."
TG_BLOCKED = (
    "Не удалось передать файлы в чат с ботом: бот заблокирован или чат удалён. "
    "Откройте чат с ботом, нажмите «Перезапустить» и повторите."
)
TG_FILE_FAILED = "Telegram не принял файл «{name}». Попробуйте ещё раз или отправьте файл через чат."
NO_MANAGER_NOTICE = (
    "📥 Поручение #{task_id} сохранено, но уведомить начальника сейчас не удалось — в боте нет "
    "активного начальника. Сообщите начальнику о нём лично."
)
AI_RETRY_NOTICE = "⚠️ AI сейчас недоступен — другой вариант предложить не получилось. Отредактируйте формулировку сами."
AI_TEAM_LIMIT_NOTICE = (
    "⚠️ AI-подсказки на сегодня закончились — показан вариант по правилам. Отредактируйте его сами."
)
FORMULATE_TOO_OFTEN = "⏳ Слишком много запросов к AI подряд — повторите через {wait} или сформулируйте результат сами."
PROPOSE_TOO_OFTEN = "⏳ Слишком много поручений подряд — следующее можно внести через {wait}."
VOICE_TOO_OFTEN = "🎤 Слишком много записей подряд — повторите через {wait} или введите текст с клавиатуры."
VOICE_TOO_BIG = "🎤 Запись слишком длинная — скажите короче или введите текст с клавиатуры."
VOICE_NO_AUDIO = "🎤 Запись не дошла до сервера. Проверьте интернет и повторите."
REQUEST_TIMEOUT = "Запрос не дошёл до сервера целиком. Проверьте интернет и повторите."
UPLOAD_TIMEOUT = "Файлы не дошли до сервера: связь прервалась. Проверьте интернет и отправьте ещё раз."
TOO_MANY_PARTS = "Слишком много полей в форме сдачи."
EXPORT_STARTED = "📤 Отчёт «{label}» придёт в чат с ботом через несколько секунд."
SUBMIT_CAPTION = "📎 К задаче #{task_id}"
BUSY: dict[str, str] = {
    "formulate": "⏳ Подождите, формулирую вариант…",
    "create": "⏳ Задача уже создаётся…",
    "propose": "⏳ Поручение уже отправляется…",
    "submit": "⏳ Результат уже отправляется…",
    "export": "⏳ Отчёт уже готовится — он придёт в чат с ботом.",
    "files": "⏳ Файлы уже отправляются в чат.",
    "voice": "⏳ Подождите, распознаю предыдущую запись…",
}

_LIST_SCOPES = ("my", "all", "emp")
_LIST_STATUSES = ("open", "overdue", "review", "done", "proposed", "all")
# «Все» для сотрудника — без отклонённых и отменённых; начальник видит и отменённые (task_view).
_ALL_FOR_EMPLOYEE = (TaskStatus.PROPOSED, TaskStatus.ACTIVE, TaskStatus.REWORK, TaskStatus.SUBMITTED, TaskStatus.DONE)
_ALL_FOR_MANAGER = (*_ALL_FOR_EMPLOYEE, TaskStatus.CANCELLED)
_PHOTO_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_ID_RE = re.compile(r"\d{1,19}")
_INT_RE = re.compile(r"-?\d{1,9}")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LOCAL_DT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?")
_TASK_ID_QUERY_RE = re.compile(r"#?(\d{1,10})")
_dumps = functools.partial(json.dumps, ensure_ascii=False, separators=(",", ":"))


# Данные запроса, которые кладёт auth_session_middleware.
INIT: web.RequestKey[auth.InitData] = web.RequestKey("kpi_webapp_init", auth.InitData)
SESSION: web.RequestKey[AsyncSession] = web.RequestKey("kpi_webapp_session", AsyncSession)
VIEWER: web.RequestKey[Any] = web.RequestKey("kpi_webapp_viewer", object)  # User | None


def _ok(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_dumps)


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status, dumps=_dumps)


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def _ctx(request: web.Request) -> WebappContext:
    return request.config_dict[CTX]


def _session(request: web.Request) -> AsyncSession:
    return request[SESSION]


def _notice(delivered: object) -> str | None:
    return None if delivered else NOT_DELIVERED


def _wait_text(seconds: float) -> str:
    """Через сколько можно снова (с округлением вверх): «40 с», «7 мин», «3 ч»."""
    secs = max(1, math.ceil(seconds))
    if secs < 60:
        return f"{secs} с"
    mins = math.ceil(secs / 60)
    if mins < 60:
        return f"{mins} мин"
    return f"{math.ceil(mins / 60)} ч"


def _rate_limit(ctx: WebappContext, kind: str, tg_id: int, limits: Sequence[tuple[int, float]], message: str) -> None:
    """Засчитать действие пользователя; лимит превышен — 429 ``rate_limited`` с текстом «повторите через …»."""
    wait = ctx.limits.hit(kind, tg_id, limits)
    if wait is not None:
        log.info("API: лимит частоты «%s» превышен", kind)
        raise ApiError(429, "rate_limited", message.format(wait=_wait_text(wait)))


def _not_open_text(status: TaskStatus) -> str:
    return NOT_OPEN_TEXTS.get(status, NOT_OPEN_DEFAULT)


def _route_name(request: web.Request) -> str:
    """Шаблон маршрута для журнала («/api/tasks/{task_id}»), не адрес запроса."""
    try:
        resource = request.match_info.route.resource
    except AttributeError:
        return "-"
    return resource.canonical if resource is not None else "-"


# --- Доступ (§4.4, §5.3) ----------------------------------------------------------------------------------


def _access(user: User | None) -> tuple[str, str | None]:
    """Вид доступа и текст для неактивных. Анкету не заполнил (PENDING без ФИО) — как незарегистрированный."""
    if user is None or (user.status == UserStatus.PENDING and not user.full_name):
        return "unregistered", NOT_REGISTERED
    if user.status == UserStatus.PENDING:
        return "pending", TXT_PENDING
    if user.status == UserStatus.BLOCKED:
        return "blocked", TXT_BLOCKED
    return "active", None


def active_viewer(request: web.Request) -> User:
    """Активный пользователь; иначе 403 not_registered / pending / blocked."""
    user: User | None = request[VIEWER]
    access, message = _access(user)
    if access != "active" or user is None:
        raise ApiError(403, "not_registered" if access == "unregistered" else access, message or NOT_REGISTERED)
    return user


def manager_viewer(request: web.Request) -> User:
    user = active_viewer(request)
    if not user.is_manager:
        raise ApiError(403, "forbidden", MANAGER_ONLY)
    return user


def employee_viewer(request: web.Request) -> User:
    user = active_viewer(request)
    if user.role != Role.EMPLOYEE:
        raise ApiError(403, "forbidden", EMPLOYEE_ONLY)
    return user


def _can_view(task: Task, viewer: User) -> bool:
    return viewer.is_manager or task.assignee_id == viewer.id


# --- Входные данные (§6.1) ----------------------------------------------------------------------------------


def _path_id(request: web.Request, name: str, not_found: str) -> int:
    """id из пути: десятичное целое в пределах INTEGER, иначе 404."""
    raw = request.match_info.get(name, "")
    if not _ID_RE.fullmatch(raw):
        raise ApiError(404, "not_found", not_found)
    value = int(raw)
    if value <= 0 or not is_db_id(value):
        raise ApiError(404, "not_found", not_found)
    return value


def _query_int(request: web.Request, name: str, default: int, *, low: int, high: int | None = None) -> int:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    if not _INT_RE.fullmatch(raw.strip()):
        raise _bad(f"Параметр «{name}»: ожидается целое число")
    value = int(raw.strip())
    if value < low or (high is not None and value > high):
        bounds = f"от {low} до {high}" if high is not None else f"не меньше {low}"
        raise _bad(f"Параметр «{name}»: число {bounds}")
    return value


def _query_id(request: web.Request, name: str, *, required: bool) -> int | None:
    raw = (request.query.get(name) or "").strip()
    if not raw:
        if required:
            raise _bad(f"Не указан параметр «{name}»")
        return None
    if not _ID_RE.fullmatch(raw) or not is_db_id(int(raw)):
        raise _bad(f"Параметр «{name}»: ожидается id")
    return int(raw)


async def _body(request: web.Request, allowed: Iterable[str]) -> dict[str, Any]:
    """Тело запроса — JSON-объект (пустое тело — {}); неизвестные поля — 400. Больше 1 МиБ — 413."""
    raw = await request.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise _bad(BAD_JSON) from None
    if not isinstance(data, dict):
        raise _bad(BAD_JSON)
    allowed_set = set(allowed)
    for key in data:
        if key not in allowed_set:
            raise _bad(f"Неизвестное поле «{key}»")
    return data


def _text(
    data: dict[str, Any], key: str, label: str, *, max_len: int, required: bool = False, min_len: int = 0
) -> str | None:
    """Текстовое поле: str, пробелы по краям обрезаются; пустое необязательное — None."""
    value = data.get(key)
    if value is None:
        if required:
            raise _bad(f"{label}: обязательное поле")
        return None
    if not isinstance(value, str):
        raise _bad(f"{label}: ожидается текст")
    value = value.strip()
    if not value:
        if required:
            raise _bad(f"{label}: обязательное поле")
        return None
    if len(value) > max_len:
        raise _bad(f"{label}: до {max_len} символов")
    if len(value) < min_len:
        raise _bad(f"{label}: не короче {min_len} символов")
    return value


def parse_number_input(value: Any, label: str) -> float | None:
    """Число: null/"" -> None; bool — ошибка; конечное int/float; строка — как в чате («1 200», «10,5»)."""
    if value is None or value == "":
        return None
    error = _bad(f"«{label}»: ожидается число")
    if isinstance(value, bool):
        raise error
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            raise error from None
    elif isinstance(value, str):
        parsed = parse_number(value)
        if parsed is None:
            raise error
        number = parsed
    else:
        raise error
    if not math.isfinite(number):
        raise error
    return number


def _plan_value(value: Any) -> float | None:
    number = parse_number_input(value, "План")
    if number is not None and not 0 < number <= PLAN_MAX:
        raise _bad("План: положительное число не больше 10^15")
    return number


def _weight(value: Any) -> int:
    error = _bad("Вес: целое число от 1 до 100")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error
    if isinstance(value, float):
        if not value.is_integer():
            raise error
        value = int(value)
    if not 1 <= value <= 100:
        raise error
    return int(value)


def _priority(value: Any, default: Priority | None = Priority.MEDIUM) -> Priority:
    if value is None and default is not None:
        return default
    try:
        return Priority(value)
    except ValueError:
        raise _bad("Приоритет: high, medium или low") from None


def _object_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or not is_db_id(value):
        raise _bad(f"{label}: ожидается id")
    return value


def parse_deadline_input(value: Any) -> datetime:
    """Срок (DeadlineInput) -> naive UTC.

    «YYYY-MM-DD» — местная дата + время по умолчанию; «YYYY-MM-DDTHH:MM[:SS]» — местное время;
    ISO-8601 со смещением или «Z» — абсолютный момент. Прошлое и «дальше 5 лет» проверяют сервисы.
    """
    if not isinstance(value, str):
        raise _bad(DEADLINE_FORMAT)
    text = value.strip()
    try:
        if _DATE_RE.fullmatch(text):
            return dateparse.iso_to_deadline(text)
        if _LOCAL_DT_RE.fullmatch(text):
            return to_utc(datetime.fromisoformat(text))
        parsed = datetime.fromisoformat(text) if "T" in text else None
        if parsed is None or parsed.tzinfo is None:
            raise ValueError(text)
        return parsed.astimezone(UTC).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise _bad(DEADLINE_FORMAT) from None


# --- Лёгкие чтения (§10): один SQL-запрос на функцию, столбцы без ORM-связей ------------------------------


def status_filter(status: str, viewer_is_manager: bool) -> tuple[tuple[TaskStatus, ...], bool]:
    """Вкладка списка -> (статусы, только просроченные) — как task_view._status_filter + «proposed»."""
    match status:
        case "overdue":
            return OPEN_STATUSES, True
        case "review":
            return (TaskStatus.SUBMITTED,), False
        case "done":
            return (TaskStatus.DONE,), False
        case "proposed":
            return (TaskStatus.PROPOSED,), False
        case "all":
            return (_ALL_FOR_MANAGER if viewer_is_manager else _ALL_FOR_EMPLOYEE), False
    return OPEN_STATUSES, False


def _row_filters(
    assignee_id: int | None, statuses: Sequence[TaskStatus] | None, overdue_only: bool, now: datetime
) -> list[ColumnElement[bool]]:
    """Копия tasks._task_filters с явным «сейчас»."""
    conditions: list[ColumnElement[bool]] = []
    if assignee_id is not None:
        conditions.append(Task.assignee_id == assignee_id if is_db_id(assignee_id) else false())
    if statuses is not None:
        conditions.append(Task.status.in_(list(statuses)))
    if overdue_only:
        conditions.append(Task.status.in_(OPEN_STATUSES))
        conditions.append(Task.deadline < now)
    return conditions


def _list_order() -> list[Any]:
    """Порядок tasks.list_tasks: незавершённые по сроку (ближайшие сверху), DONE по completed_at desc, id."""
    is_done = Task.status == TaskStatus.DONE
    return [
        case((is_done, 1), else_=0),
        case((~is_done, Task.deadline)).nulls_first(),
        Task.completed_at.desc().nulls_last(),
        Task.id,
    ]


def _last_sub(column: Any) -> Any:
    """Значение столбца последней сдачи задачи (как Task.last_submission) — коррелированный подзапрос."""
    return (
        select(column)
        .where(Submission.task_id == Task.id)
        .order_by(Submission.id.desc())
        .limit(1)
        .scalar_subquery()
    )


def _row_columns(*, with_text: bool = False) -> list[Any]:
    columns: list[Any] = [
        Task.id,
        Task.title,
        Task.status,
        Task.priority,
        Task.weight,
        Task.source,
        Task.deadline,
        Task.accepted_at,
        Task.submitted_at,
        Task.completed_at,
        Task.final_score,
        Task.rework_count,
        _last_sub(Submission.is_late),
        Task.assignee_id,
        User.full_name,
        User.position,
        User.role,
        User.status,
    ]
    if with_text:
        columns.append(Task.expected_result)
    return columns


def _task_row(row: Sequence[Any], *, with_text: bool = False) -> TaskRowData:
    return TaskRowData(
        id=row[0],
        title=row[1],
        status=TaskStatus(row[2]),
        priority=Priority(row[3]),
        weight=row[4],
        source=row[5],
        deadline=row[6],
        accepted_at=row[7],
        submitted_at=row[8],
        completed_at=row[9],
        final_score=row[10],
        rework_count=row[11] or 0,
        last_late=None if row[12] is None else bool(row[12]),
        assignee_id=row[13],
        assignee_full_name=row[14],
        assignee_position=row[15],
        assignee_role=Role(row[16]),
        assignee_status=UserStatus(row[17]),
        expected_result=row[18] if with_text else None,
    )


async def list_task_rows(
    session: AsyncSession,
    *,
    assignee_id: int | None,
    statuses: Sequence[TaskStatus] | None,
    overdue_only: bool,
    now: datetime,
    limit: int,
    offset: int,
) -> list[TaskRowData]:
    """Страница списка — фильтры и порядок как tasks.list_tasks."""
    rows, _ = await _task_page(
        session, assignee_id=assignee_id, statuses=statuses, overdue_only=overdue_only, now=now,
        limit=limit, offset=offset, with_total=False,
    )
    return rows


async def _task_page(
    session: AsyncSession,
    *,
    assignee_id: int | None,
    statuses: Sequence[TaskStatus] | None,
    overdue_only: bool,
    now: datetime,
    limit: int,
    offset: int,
    with_total: bool = True,
) -> tuple[list[TaskRowData], int | None]:
    """Страница и (with_total) общее число строк — одним запросом: ``count(*) OVER ()`` считается до
    LIMIT/OFFSET. Страница за пределами — строк нет, итог неизвестен (None)."""
    columns = _row_columns()
    if with_total:
        columns.append(func.count().over())
    stmt = (
        select(*columns)
        .join(User, User.id == Task.assignee_id)
        .where(*_row_filters(assignee_id, statuses, overdue_only, now))
        .order_by(*_list_order())
        .offset(non_negative(offset))
        .limit(sql_limit(limit))
    )
    rows = list(await session.execute(stmt))
    items = [_task_row(row) for row in rows]
    total = int(rows[0][-1]) if with_total and rows else None
    return items, total


async def count_task_rows(
    session: AsyncSession,
    *,
    assignee_id: int | None,
    statuses: Sequence[TaskStatus] | None,
    overdue_only: bool,
    now: datetime,
) -> int:
    stmt = select(func.count()).select_from(Task).where(*_row_filters(assignee_id, statuses, overdue_only, now))
    return int(await session.scalar(stmt) or 0)


def _normalize(text: str | None) -> str:
    """Для поиска: casefold, «ё» = «е», пробелы схлопнуты. (lower() в SQLite не понимает кириллицу,
    а в PostgreSQL зависит от локали базы — поэтому сравнение в Python.)"""
    return " ".join((text or "").casefold().replace("ё", "е").split())


async def search_task_rows(
    session: AsyncSession,
    q: str,
    *,
    assignee_id: int | None,
    statuses: Sequence[TaskStatus] | None,
    overdue_only: bool,
    now: datetime,
    scan_limit: int = SEARCH_SCAN_LIMIT,
) -> tuple[list[TaskRowData], bool]:
    """Поиск: до scan_limit строк выборки одним запросом (те же фильтры и порядок), совпадения — в Python:
    название, ожидаемый результат, ФИО исполнителя; «#12» / «12» — ещё и номер задачи.
    -> (совпавшие в порядке списка, выборка упёрлась в scan_limit)."""
    stmt = (
        select(*_row_columns(with_text=True))
        .join(User, User.id == Task.assignee_id)
        .where(*_row_filters(assignee_id, statuses, overdue_only, now))
        .order_by(*_list_order())
        .limit(max(scan_limit, 0) + 1)
    )
    rows = [_task_row(row, with_text=True) for row in await session.execute(stmt)]
    truncated = len(rows) > scan_limit
    rows = rows[:scan_limit]
    needle = _normalize(q)
    id_match = _TASK_ID_QUERY_RE.fullmatch(q.strip())
    wanted_id = int(id_match.group(1)) if id_match else None
    found = [
        row
        for row in rows
        if row.id == wanted_id
        or needle in _normalize(row.title)
        or needle in _normalize(row.expected_result)
        or needle in _normalize(row.assignee_full_name)
    ]
    return found, truncated


def _count_if(condition: ColumnElement[bool]) -> Any:
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


async def tab_counts(
    session: AsyncSession, *, assignee_id: int | None, viewer_is_manager: bool, now: datetime
) -> dict[str, int]:
    """Счётчики вкладок списка одним запросом (условные суммы)."""
    names = list(_LIST_STATUSES)
    columns = []
    for name in names:
        statuses, overdue_only = status_filter(name, viewer_is_manager)
        condition = Task.status.in_(list(statuses))
        if overdue_only:
            condition = condition & (Task.deadline < now)
        columns.append(_count_if(condition))
    stmt = select(*columns).select_from(Task).where(*_row_filters(assignee_id, None, False, now))
    row = (await session.execute(stmt)).one()
    return {name: int(value or 0) for name, value in zip(names, row, strict=True)}


async def me_counts(session: AsyncSession, viewer: User, now: datetime) -> dict[str, int]:
    """Бейджи вкладок /api/me одним запросом. Начальник: review, proposals, open, overdue, pending_users
    (заявки: PENDING с ФИО — скалярный подзапрос); сотрудник: open, overdue, unaccepted, rework, review, proposed."""
    is_open = Task.status.in_(OPEN_STATUSES)
    overdue = is_open & (Task.deadline < now)
    if viewer.is_manager:
        pending_users = (
            select(func.count())
            .select_from(User)
            .where(User.status == UserStatus.PENDING, User.full_name != "")
            .scalar_subquery()
        )
        names = ["review", "proposals", "open", "overdue", "pending_users"]
        stmt = select(
            _count_if(Task.status == TaskStatus.SUBMITTED),
            _count_if(Task.status == TaskStatus.PROPOSED),
            _count_if(is_open),
            _count_if(overdue),
            pending_users,
        ).select_from(Task)
    else:
        names = ["open", "overdue", "unaccepted", "rework", "review", "proposed"]
        stmt = (
            select(
                _count_if(is_open),
                _count_if(overdue),
                _count_if((Task.status == TaskStatus.ACTIVE) & Task.accepted_at.is_(None)),
                _count_if(Task.status == TaskStatus.REWORK),
                _count_if(Task.status == TaskStatus.SUBMITTED),
                _count_if(Task.status == TaskStatus.PROPOSED),
            )
            .select_from(Task)
            .where(Task.assignee_id == viewer.id)
        )
    row = (await session.execute(stmt)).one()
    return {name: int(value or 0) for name, value in zip(names, row, strict=True)}


async def history_rows(
    session: AsyncSession, assignee_id: int, *, limit: int, offset: int
) -> list[HistoryRowData]:
    """История оценок (DONE исполнителя) — порядок как tasks.evaluated_history."""
    rows, _ = await _history_page(session, assignee_id, limit=limit, offset=offset, with_total=False)
    return rows


async def _history_page(
    session: AsyncSession, assignee_id: int, *, limit: int, offset: int, with_total: bool = True
) -> tuple[list[HistoryRowData], int | None]:
    columns: list[Any] = [
        Task.id,
        Task.title,
        Task.weight,
        Task.completed_at,
        func.coalesce(Task.ai_score, _last_sub(Submission.ai_score)),
        Task.final_score,
        Task.rework_count,
        _last_sub(Submission.decision),
        _last_sub(Submission.is_late),
    ]
    if with_total:
        columns.append(func.count().over())
    stmt = (
        select(*columns)
        .where(Task.assignee_id == assignee_id, Task.status == TaskStatus.DONE)
        .order_by(Task.completed_at.desc().nulls_last(), Task.id.desc())
        .limit(sql_limit(limit))
        .offset(non_negative(offset))
    )
    rows = list(await session.execute(stmt))
    items = [
        HistoryRowData(
            task_id=row[0],
            title=row[1],
            weight=row[2],
            completed_at=row[3],
            ai_score=row[4],
            final_score=row[5],
            rework_count=row[6] or 0,
            decision=row[7],
            is_late=None if row[8] is None else bool(row[8]),
        )
        for row in rows
    ]
    total = int(rows[0][-1]) if with_total and rows else None
    return items, total


async def snapshots_between(
    session: AsyncSession, start: datetime, end: datetime, assignee_ids: Sequence[int]
) -> list[tuple[int, TaskSnapshot]]:
    """Копия kpi._period_snapshots для произвольного [start, end): (assignee_id, снимок) одним запросом."""
    if not assignee_ids:
        return []
    stmt = (
        select(
            Task.assignee_id,
            Task.id,
            Task.title,
            Task.weight,
            Task.status,
            Task.deadline,
            Task.final_score,
            Task.source,
            _last_sub(Submission.is_late),
        )
        .where(
            Task.deadline >= start,
            Task.deadline < end,
            Task.status.not_in(EXCLUDED_FROM_KPI),
            Task.assignee_id.in_(list(assignee_ids)),
        )
        .order_by(Task.deadline, Task.id)
    )
    return [
        (
            row[0],
            TaskSnapshot(
                task_id=row[1],
                title=row[2],
                weight=row[3],
                status=row[4],
                deadline=row[5],
                final_score=row[6],
                source=row[7],
                last_late=None if row[8] is None else bool(row[8]),
            ),
        )
        for row in await session.execute(stmt)
    ]


def kpi_in(
    snaps: Sequence[tuple[int, TaskSnapshot]],
    start: datetime,
    end: datetime,
    now: datetime,
    assignee_id: int | None = None,
) -> KpiResult:
    """KPI по снимкам со сроком в [start, end) (и исполнителю) — kpi.compute_kpi, как в чате."""
    chosen = [
        snap
        for owner, snap in snaps
        if start <= snap.deadline < end and (assignee_id is None or owner == assignee_id)
    ]
    return kpi.compute_kpi(chosen, now, get_settings().overdue_counts_as_zero)


def _merge_ranges(ranges: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Пересекающиеся и соприкасающиеся диапазоны — в один (меньше запросов)."""
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


async def _snapshots_for(
    session: AsyncSession, ranges: Iterable[tuple[datetime, datetime]], assignee_ids: Sequence[int]
) -> list[tuple[int, TaskSnapshot]]:
    """Снимки для всех диапазонов: обычно один запрос (период рядом с окном тренда), иначе — два.
    Диапазоны после слияния не пересекаются и идут по возрастанию — порядок (срок, id) сохраняется."""
    result: list[tuple[int, TaskSnapshot]] = []
    for start, end in _merge_ranges(ranges):
        result += await snapshots_between(session, start, end, assignee_ids)
    return result


async def _weight_load_row(
    session: AsyncSession, user_id: int, start: datetime, end: datetime, exclude_task_id: int | None
) -> int | None:
    """Пользователь есть -> сумма весов его задач со сроком в [start, end) (фильтры tasks.weight_load);
    нет -> None. Один запрос."""
    conditions = [
        Task.assignee_id == user_id,
        Task.status.not_in(EXCLUDED_FROM_KPI),
        Task.deadline >= start,
        Task.deadline < end,
    ]
    if exclude_task_id is not None:
        conditions.append(Task.id != exclude_task_id)
    load = select(func.coalesce(func.sum(Task.weight), 0)).where(*conditions).scalar_subquery()
    row = (await session.execute(select(User.id, load).where(User.id == user_id))).first()
    return None if row is None else int(row[1] or 0)


# --- Middleware (§4.4) --------------------------------------------------------------------------------------


def _make_middlewares(ctx: WebappContext) -> list[Any]:
    @web.middleware
    async def error_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        started = time.perf_counter()
        code: str | None = None
        try:
            response = await handler(request)
        except ApiError as exc:
            code, response = exc.code, _error(exc.status, exc.code, exc.message)
        except auth.AuthError as exc:
            code, response = exc.code, _error(401, exc.code, exc.message)
        except DomainError as exc:
            code, response = "domain", _error(400, "domain", exc.message)
        except web.HTTPNotFound:
            code, response = "not_found", _error(404, "not_found", NOT_FOUND)
        except web.HTTPMethodNotAllowed:
            code, response = "method_not_allowed", _error(405, "method_not_allowed", METHOD_NOT_ALLOWED)
        except web.HTTPRequestEntityTooLarge:
            code, response = "too_large", _error(413, "too_large", TOO_LARGE)
        except web.HTTPException as exc:
            if 400 <= exc.status < 500:
                code, response = "bad_request", _error(400, "bad_request", BAD_REQUEST)
            else:
                log.error("API %s %s: HTTP %s", request.method, _route_name(request), exc.status)
                code, response = "internal", _error(500, "internal", GENERIC_ERROR)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("API %s %s: ошибка обработки запроса", request.method, _route_name(request))
            code, response = "internal", _error(500, "internal", GENERIC_ERROR)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-App-Version"] = ctx.static.version
        if response.status in (401, 403):
            log.info("API %s %s", response.status, code)
        log.debug(
            "API %s %s -> %s (%d мс)",
            request.method,
            _route_name(request),
            response.status,
            (time.perf_counter() - started) * 1000,
        )
        return response

    @web.middleware
    async def auth_session_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        init = auth.validate_init_data(
            request.headers.get(auth.INIT_DATA_HEADER, ""), ctx.settings.bot_token.strip()
        )
        request[INIT] = init
        if request.can_read_body and request.match_info.handler not in _STREAMING_HANDLERS:
            # Тело — целиком и с ограничением по времени ДО соединения с базой: клиент, приславший
            # заголовки без тела, не держит соединение пула (§6.3). aiohttp запоминает прочитанные байты —
            # _body() их не перечитывает. Больше client_max_size (1 МиБ) — 413 из request.read().
            try:
                await asyncio.wait_for(request.read(), BODY_READ_TIMEOUT_SEC)
            except TimeoutError:
                log.info("API %s %s: тело запроса не пришло за %.0f с", request.method, _route_name(request), BODY_READ_TIMEOUT_SEC)
                raise ApiError(408, "request_timeout", REQUEST_TIMEOUT) from None
        async with ctx.sessionmaker() as session:
            request[SESSION] = session
            request[VIEWER] = await load_user(session, init.tg_id)
            try:
                response = await handler(request)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return response

    return [error_middleware, auth_session_middleware]


# --- /api/me (§8.3) --------------------------------------------------------------------------------------


async def get_me(request: web.Request) -> web.Response:
    session = _session(request)
    viewer: User | None = request[VIEWER]
    settings = get_settings()
    now = utcnow()
    access, message = _access(viewer)
    counts: dict[str, int] | None = None
    role: str | None = None
    if access == "active" and viewer is not None:
        role = "manager" if viewer.is_manager else "employee"
        counts = await me_counts(session, viewer, now)
    return _ok(
        {
            "access": access,
            "message": message,
            "user": ser.me_user(viewer) if viewer is not None and access != "unregistered" else None,
            "role": role,
            "now": ser.iso(now),
            "today": to_local(now).date().isoformat(),
            "config": {
                "max_score": settings.max_score,
                "timezone": settings.timezone,
                "default_deadline_time": settings.default_deadline_time,
                "ai_enabled": settings.ai_enabled,
                "period_kinds": list(periods.PERIOD_KINDS),
                "max_files": MAX_FILES,
                "max_file_mb": MAX_FILE_BYTES // (1024 * 1024),
                "max_total_mb": MAX_TOTAL_BYTES // (1024 * 1024),
                "weight_options": list(WEIGHT_OPTIONS),
                "score_options": list(SCORE_OPTIONS),
                "history_page_size": HISTORY_PAGE_SIZE,
                "trend_weeks": TREND_WEEKS,
                # Голосовой ввод в формах: включён и есть чем распознавать; самая длинная запись, секунд.
                "voice_enabled": dictate.voice_hint_enabled(),
                "voice_max_sec": settings.voice_max_sec,
            },
            "deadline_options": [
                {"label": label, "date": day} for label, day in dateparse.quick_deadline_options()
            ],
            "counts": counts,
        }
    )


# --- Задачи (§8.4) -------------------------------------------------------------------------------------------


async def _load_task(session: AsyncSession, task_id: int) -> Task:
    task = await tasks_svc.get_task(session, task_id)
    if task is None:
        raise ApiError(404, "not_found", TASK_NOT_FOUND)
    return task


def _search_text(raw: str | None) -> str:
    """q после обрезки; короче 2 символов и не «#число» — как пустой."""
    text = (raw or "").strip()
    if len(text) > QUERY_MAX:
        raise _bad(f"Поиск: до {QUERY_MAX} символов")
    return text if len(text) >= 2 else ""  # «#число» — не короче 2 символов


async def list_tasks(request: web.Request) -> web.Response:
    viewer = active_viewer(request)
    session = _session(request)
    query = request.query
    scope = query.get("scope") or ("all" if viewer.is_manager else "my")
    if scope not in _LIST_SCOPES:
        raise _bad("Параметр «scope»: my, all или emp")
    if scope in ("all", "emp") and not viewer.is_manager:
        raise ApiError(403, "forbidden", MANAGER_ONLY)
    status = query.get("status") or "open"
    if status not in _LIST_STATUSES:
        raise _bad("Параметр «status»: open, overdue, review, done, proposed или all")
    if scope == "my":
        assignee_id: int | None = viewer.id
    elif scope == "emp":
        assignee_id = _query_id(request, "user_id", required=True)
    else:
        assignee_id = None
    page = _query_int(request, "page", 0, low=0, high=10**6)
    limit = _query_int(request, "limit", PAGE_LIMIT_DEFAULT, low=1, high=PAGE_LIMIT_MAX)
    want_counts = (query.get("counts") or "0").strip()
    if want_counts not in ("0", "1"):
        raise _bad("Параметр «counts»: 0 или 1")
    text = _search_text(query.get("q"))
    statuses, overdue_only = status_filter(status, viewer.is_manager)
    now = utcnow()
    filters = {"assignee_id": assignee_id, "statuses": statuses, "overdue_only": overdue_only, "now": now}

    truncated = False
    if text:
        found, truncated = await search_task_rows(session, text, scan_limit=SEARCH_SCAN_LIMIT, **filters)
        total = len(found)
        items = found[page * limit : (page + 1) * limit]
    else:
        items, counted = await _task_page(session, limit=limit, offset=page * limit, **filters)
        total = counted if counted is not None else await count_task_rows(session, **filters)
    data: dict[str, Any] = {
        "items": [ser.task_row(row, now) for row in items],
        "total": total,
        "page": page,
        "pages": max(1, math.ceil(total / limit)),
        "limit": limit,
        "truncated": truncated,
    }
    if want_counts == "1":
        data["counts"] = await tab_counts(
            session, assignee_id=assignee_id, viewer_is_manager=viewer.is_manager, now=now
        )
    return _ok(data)


async def get_task(request: web.Request) -> web.Response:
    viewer = active_viewer(request)
    session = _session(request)
    task = await _load_task(session, _path_id(request, "task_id", TASK_NOT_FOUND))
    if not _can_view(task, viewer):
        raise ApiError(403, "forbidden", NO_RIGHTS)
    events = await tasks_svc.task_events(session, task.id)
    return _ok(ser.task_card(task, events, viewer, utcnow()))


_TASK_FIELDS = ("title", "expected_result", "description", "plan_value", "plan_unit", "deadline")


def _task_texts(data: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    """Поля задачи/поручения из тела. partial — правка: только присланные поля."""
    fields: dict[str, Any] = {}
    if not partial or "title" in data:
        fields["title"] = _text(data, "title", "Название", max_len=TITLE_MAX, required=True)
    if not partial or "expected_result" in data:
        fields["expected_result"] = _text(
            data, "expected_result", "Ожидаемый результат", max_len=RESULT_MAX, required=True
        )
    if not partial or "description" in data:
        fields["description"] = _text(data, "description", "Описание", max_len=RESULT_MAX)
    plan_cleared = False
    if not partial or "plan_value" in data:
        fields["plan_value"] = _plan_value(data.get("plan_value"))
        plan_cleared = fields["plan_value"] is None
    if plan_cleared:
        fields["plan_unit"] = None  # без числа единица не нужна (task_create._clean_plan)
    elif not partial or "plan_unit" in data:
        fields["plan_unit"] = _text(data, "plan_unit", "Единица плана", max_len=UNIT_MAX)
    if not partial or "deadline" in data:
        fields["deadline"] = parse_deadline_input(data.get("deadline"))
    return fields


async def create_task(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = manager_viewer(request)
    session = _session(request)
    data = await _body(request, ("assignee_id", *_TASK_FIELDS, "priority", "weight"))
    assignee_id = _object_id(data.get("assignee_id"), "Сотрудник")
    fields = _task_texts(data, partial=False)
    priority = _priority(data.get("priority"))
    weight = _weight(data.get("weight"))
    async with ctx.gate.hold("create", viewer.tg_id, BUSY["create"]):
        task = await tasks_svc.create_task(
            session, creator=viewer, assignee_id=assignee_id, priority=priority, weight=weight, **fields
        )
        await session.commit()
        delivered = await notify.notify_new_task(ctx.bot, task)
    return _ok(
        {"task": ser.task_detail(task, viewer, utcnow()), "delivered": bool(delivered), "notice": _notice(delivered)},
        status=201,
    )


async def update_task(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = manager_viewer(request)
    session = _session(request)
    task = await _load_task(session, _path_id(request, "task_id", TASK_NOT_FOUND))
    data = await _body(request, (*_TASK_FIELDS, "priority", "weight"))
    if not data:
        raise _bad(NO_CHANGES)
    if task.status == TaskStatus.PROPOSED and ({"priority", "weight"} & data.keys()):
        raise DomainError(PROPOSAL_FIELDS)
    fields = _task_texts(data, partial=True)
    if "priority" in data:
        fields["priority"] = _priority(data["priority"], default=None)
    if "weight" in data:
        fields["weight"] = _weight(data["weight"])
    task, changes = await tasks_svc.update_task(session, task.id, viewer, **fields)
    await session.commit()
    delivered = await notify.notify_task_changed(ctx.bot, task, changes) if changes else None
    return _ok(
        {
            "task": ser.task_detail(task, viewer, utcnow()),
            "changed": list(changes),
            "delivered": bool(delivered) if changes else None,
            "notice": _notice(delivered) if changes else None,
        }
    )


async def accept_task(request: web.Request) -> web.Response:
    viewer = active_viewer(request)
    session = _session(request)
    task = await _load_task(session, _path_id(request, "task_id", TASK_NOT_FOUND))
    if task.assignee_id != viewer.id:
        raise ApiError(403, "forbidden", NO_RIGHTS)
    task = await tasks_svc.accept_task(session, task.id, viewer)
    await session.commit()
    return _ok({"task": ser.task_detail(task, viewer, utcnow())})


async def cancel_task(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = manager_viewer(request)
    session = _session(request)
    task = await _load_task(session, _path_id(request, "task_id", TASK_NOT_FOUND))
    data = await _body(request, ("reason",))
    reason = _text(data, "reason", "Причина", max_len=REASON_MAX)
    task = await tasks_svc.cancel_task(session, task.id, viewer, reason)
    await session.commit()
    delivered = await notify.notify_task_cancelled(ctx.bot, task, reason)
    return _ok({"task": ser.task_detail(task, viewer, utcnow()), "delivered": bool(delivered), "notice": _notice(delivered)})


async def ai_formulate(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = active_viewer(request)
    session = _session(request)
    data = await _body(request, ("title", "raw_result", "previous"))
    title = _text(data, "title", "Название", max_len=TITLE_MAX, required=True) or ""
    raw = _text(data, "raw_result", "Ожидаемый результат", max_len=RESULT_MAX, required=True, min_len=RAW_MIN) or ""
    previous = _text(data, "previous", "Предыдущий вариант", max_len=PREVIOUS_MAX)
    notice = None
    async with ctx.gate.hold("formulate", viewer.tg_id, BUSY["formulate"]):
        # Каждый вызов — вся цепочка AI, а её квоты общие с оценкой сдач: не чаще лимита на человека,
        # а после суточного лимита команды — сразу правила (квоты остаются оценке результатов).
        limits = FORMULATE_LIMITS_MANAGER if viewer.is_manager else FORMULATE_LIMITS_EMPLOYEE
        _rate_limit(ctx, "formulate", viewer.tg_id, limits, FORMULATE_TOO_OFTEN)
        await session.commit()  # отпустить соединение на время ответа AI
        team_limit = ((FORMULATE_TEAM_PER_DAY, DAY_SEC),)
        if ai_provider.ai_available() and ctx.limits.hit("formulate_team", 0, team_limit) is not None:
            log.info("Подсказки AI команды за сутки исчерпаны — правила")
            suggestion = formulate.rules_suggestion(title, raw)
            notice = AI_TEAM_LIMIT_NOTICE
        else:
            suggestion = await _ai_suggestion(title, raw, previous)
    if previous and suggestion.source != "ai":
        # Правила взяли бы текст вместе с «Предыдущий вариант: …» — берём исходные слова.
        suggestion = formulate.rules_suggestion(title, raw)
        notice = notice or AI_RETRY_NOTICE
    return _ok(
        {
            "expected_result": suggestion.expected_result,
            "plan_value": suggestion.plan_value,
            "plan_unit": suggestion.plan_unit,
            "note": suggestion.note,
            "source": suggestion.source,
            "notice": notice,
        }
    )


async def _ai_suggestion(title: str, raw: str, previous: str | None) -> formulate.ResultSuggestion:
    """Подсказка цепочки AI с общим сроком; не уложилась или упала — правила."""
    raw_for_ai = raw if not previous else f"{raw}\n\nПредыдущий вариант: {previous}. Предложи другую формулировку."
    timeout = ai_provider.chain_budget_sec(get_settings(), "formulate") + FORMULATE_EXTRA_SEC
    try:
        return await asyncio.wait_for(
            formulate.suggest_expected_result(title, raw_for_ai, deadline_text=None), timeout=timeout
        )
    except TimeoutError:
        log.warning("Подсказка формулировки не уложилась в %.0f с — правила", timeout)
    except Exception:  # noqa: BLE001 - подсказка не должна ломать форму
        log.exception("Подсказка формулировки упала — правила")
    return formulate.rules_suggestion(title, raw)


# --- Голосовой ввод (§8.10) --------------------------------------------------------------------------------


async def _read_audio(request: web.Request) -> tuple[bytes, str]:
    """Тело запроса — запись целиком: (байты, Content-Type). Больше VOICE_MAX_BYTES — 413, не пришла — 400/408."""
    declared = request.content_length
    if declared is not None and declared > VOICE_MAX_BYTES:
        raise ApiError(413, "too_large", VOICE_TOO_BIG)

    async def read() -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > VOICE_MAX_BYTES:
                raise ApiError(413, "too_large", VOICE_TOO_BIG)
            chunks.append(chunk)
        return b"".join(chunks)

    try:
        data = await asyncio.wait_for(read(), VOICE_READ_TIMEOUT_SEC)
    except TimeoutError:
        raise ApiError(408, "request_timeout", REQUEST_TIMEOUT) from None
    if not data:
        raise _bad(VOICE_NO_AUDIO)
    return data, request.headers.get("Content-Type", "")


async def voice_input(request: web.Request) -> web.Response:
    """Диктовка в форме приложения: тело — запись (WebM/Opus, MP4/AAC, OGG), ответ — распознанный текст.

    ``?mode=task`` — запись описывает задачу целиком: в ответе ещё и поля формы (начальнику — исполнитель из
    активных сотрудников). Речь не распознана — 422 ``voice_failed`` с текстом для пользователя.
    """
    ctx = _ctx(request)
    viewer = active_viewer(request)
    session = _session(request)
    mode = request.query.get("mode", "text")
    if mode not in ("text", "task"):
        raise _bad("mode: ожидается text или task")
    employees: list[tuple[int, str]] = []
    if mode == "task" and viewer.is_manager:
        employees = [(user.id, user.full_name) for user in await users_svc.list_employees(session)]
    await session.commit()  # соединение свободно на время чтения записи и ответа AI
    async with ctx.gate.hold("voice", viewer.tg_id, BUSY["voice"]):
        _rate_limit(ctx, "voice", viewer.tg_id, VOICE_LIMITS, VOICE_TOO_OFTEN)
        audio, mime_type = await _read_audio(request)
        if ctx.limits.hit("voice_team", 0, ((VOICE_TEAM_PER_DAY, DAY_SEC),)) is not None:
            log.info("Голосовой ввод команды за сутки исчерпан")
            raise ApiError(422, "voice_failed", dictate.VoiceError("off").message)
        timeout = ai_provider.chain_budget_sec(get_settings(), "transcribe") + VOICE_EXTRA_SEC
        try:
            if mode == "text":
                text = await asyncio.wait_for(dictate.transcribe(audio, mime_type), timeout=timeout)
                return _ok({"text": text})
            author = "manager" if viewer.is_manager else "employee"
            result = await asyncio.wait_for(
                dictate.dictate_task(audio=audio, mime_type=mime_type, employees=employees, author=author, now=utcnow()),
                timeout=timeout,
            )
        except dictate.VoiceError as exc:
            raise ApiError(422, "voice_failed", exc.message) from exc
        except TimeoutError:
            log.warning("Распознавание записи не уложилось в %.0f с", timeout)
            raise ApiError(422, "voice_failed", dictate.VoiceError("unavailable").message) from None
    local = to_local(result.deadline) if result.deadline is not None else None
    return _ok(
        {
            "text": result.transcript,
            "task": {
                "assignee_id": result.assignee_id,
                "title": result.title,
                "expected_result": result.expected_result,
                "plan_value": result.plan_value,
                "plan_unit": result.plan_unit,
                # Срок — местные дата и время, как их вводит форма (§11.8).
                "deadline_date": local.strftime("%Y-%m-%d") if local is not None else None,
                "deadline_time": local.strftime("%H:%M") if local is not None else None,
                "source": result.source,
            },
        }
    )


# --- Поручения сотрудников (§8.5) -------------------------------------------------------------------------


async def list_proposals(request: web.Request) -> web.Response:
    viewer = manager_viewer(request)
    session = _session(request)
    now = utcnow()
    items = await tasks_svc.list_proposals(session)
    return _ok({"items": [{"task": ser.task_detail(task, viewer, now)} for task in items]})


async def create_proposal(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = employee_viewer(request)
    session = _session(request)
    data = await _body(request, _TASK_FIELDS)
    fields = _task_texts(data, partial=False)
    async with ctx.gate.hold("propose", viewer.tg_id, BUSY["propose"]):
        # Каждое поручение — карточка с кнопками всем начальникам: не чаще лимита на сотрудника.
        _rate_limit(ctx, "propose", viewer.tg_id, PROPOSAL_LIMITS, PROPOSE_TOO_OFTEN)
        try:
            task = await tasks_svc.propose_task(session, employee=viewer, **fields)
        except DomainError:
            ctx.limits.undo("propose", viewer.tg_id)  # поручение не создано — попытка не в счёт
            raise
        await session.commit()
        # Вес поручения подбирает AI (не дольше proposal_flow.WEIGHT_BUDGET_SEC), затем уведомление начальникам.
        count = await proposal_flow.run_after_propose(ctx.bot, session, task)
    return _ok(
        {
            "task": ser.task_detail(task, viewer, utcnow()),
            "notified": count,
            "notice": None if count else NO_MANAGER_NOTICE.format(task_id=task.id),
        },
        status=201,
    )


async def approve_proposal(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = manager_viewer(request)
    session = _session(request)
    task = await _load_task(session, _path_id(request, "task_id", TASK_NOT_FOUND))
    data = await _body(request, ("weight", "priority"))
    weight = _weight(data.get("weight"))
    priority = _priority(data.get("priority"))
    task = await tasks_svc.approve_proposal(session, task.id, viewer, weight=weight, priority=priority)
    await session.commit()
    delivered = await notify.notify_proposal_decision(ctx.bot, task, True)
    return _ok({"task": ser.task_detail(task, viewer, utcnow()), "delivered": bool(delivered), "notice": _notice(delivered)})


async def reject_proposal(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = manager_viewer(request)
    session = _session(request)
    task = await _load_task(session, _path_id(request, "task_id", TASK_NOT_FOUND))
    data = await _body(request, ("reason",))
    reason = _text(data, "reason", "Причина", max_len=REASON_MAX)
    task = await tasks_svc.reject_proposal(session, task.id, viewer, reason)
    await session.commit()
    delivered = await notify.notify_proposal_decision(ctx.bot, task, False, reason)
    return _ok({"task": ser.task_detail(task, viewer, utcnow()), "delivered": bool(delivered), "notice": _notice(delivered)})


# --- Сдача результата (§8.6) ---------------------------------------------------------------------------------

_TEXT_PARTS = ("fact_text", "result_text", "fact_value", "materials_text")
_FILE_PARTS = ("files", "files[]")


@dataclass
class _UploadedFile:
    path: str
    name: str
    content_type: str | None
    size: int = 0


@dataclass
class _Upload:
    """Прочитанное тело сдачи: тексты в памяти, файлы — во временных файлах (удаляются cleanup)."""

    texts: dict[str, str] = field(default_factory=dict)
    files: list[_UploadedFile] = field(default_factory=list)
    total: int = 0

    def cleanup(self) -> None:
        for item in self.files:
            try:
                os.unlink(item.path)
            except FileNotFoundError:
                pass
            except OSError as exc:  # файл занят (Windows) — не критично, но в лог
                log.warning("Не удалось удалить временный файл сдачи (%s)", type(exc).__name__)
        self.files.clear()


def _safe_file_name(raw: str | None) -> str:
    """Имя файла из формы: только базовое имя, без управляющих символов, ≤ 255 с расширением; пусто — «file»."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    # Пробельные (таб, перевод строки) — в пробел; прочие управляющие, невидимые и обрывки суррогатов — прочь.
    name = "".join(
        " " if ch.isspace() else ch for ch in name if ch.isspace() or unicodedata.category(ch) not in ("Cc", "Cf", "Cs")
    )
    name = " ".join(name.split())
    name = (clip_file_name(name, 255) or "").strip()
    return name if name not in ("", ".", "..") else "file"


def _mb(size: int) -> int:
    return max(1, size // (1024 * 1024))


class _ReadDeadline:
    """Срок чтения тела сдачи: UPLOAD_IDLE_TIMEOUT_SEC без единого байта или UPLOAD_MAX_SEC на всё тело
    (что наступит раньше). ``touch()`` — пришли данные: срок «тишины» отсчитывается заново."""

    def __init__(self, timeout: asyncio.Timeout) -> None:
        self._timeout = timeout
        self._loop = asyncio.get_running_loop()
        self._end = self._loop.time() + UPLOAD_MAX_SEC
        self.touch()

    def touch(self) -> None:
        self._timeout.reschedule(min(self._loop.time() + UPLOAD_IDLE_TIMEOUT_SEC, self._end))


async def _read_text_part(part: BodyPartReader, name: str, deadline: _ReadDeadline) -> str:
    data = bytearray()
    while chunk := await part.read_chunk(8192):
        deadline.touch()
        data += chunk
        if len(data) > MAX_TEXT_PART_BYTES:
            raise _bad(f"Поле «{name}» слишком большое")
    try:
        return bytes(data).decode(part.get_charset(default="utf-8"))
    except (UnicodeDecodeError, LookupError):
        raise _bad(f"Поле «{name}»: неверная кодировка текста") from None


async def _read_file_part(part: BodyPartReader, upload: _Upload, deadline: _ReadDeadline) -> None:
    raw_name = part.filename
    if raw_name is None:
        raise _bad(f"Поле «{part.name}»: ожидается файл")
    name = _safe_file_name(raw_name)
    chunk = await part.read_chunk(UPLOAD_CHUNK_BYTES)
    deadline.touch()
    if not chunk:
        if not raw_name:  # пустое поле выбора файла из обычной формы браузера — не файл (диск не трогаем)
            return
        raise _bad(f"Пустой файл «{name}»")
    if len(upload.files) >= MAX_FILES:
        raise ApiError(413, "too_large", f"Можно приложить не более {MAX_FILES} файлов.")
    content_type = (part.headers.get(hdrs.CONTENT_TYPE) or "").split(";", 1)[0].strip().lower() or None
    handle = tempfile.NamedTemporaryFile(delete=False, prefix="kpi_upload_")  # noqa: SIM115 - закрывается ниже
    item = _UploadedFile(path=handle.name, name=name, content_type=content_type)
    upload.files.append(item)  # cleanup удалит и недописанный файл
    size = 0
    try:
        while chunk:
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise ApiError(413, "too_large", f"Файл «{name}» больше {_mb(MAX_FILE_BYTES)} МБ.")
            if upload.total + size > MAX_TOTAL_BYTES:
                raise ApiError(413, "too_large", f"Все файлы вместе — не больше {_mb(MAX_TOTAL_BYTES)} МБ.")
            handle.write(chunk)
            chunk = await part.read_chunk(UPLOAD_CHUNK_BYTES)
            deadline.touch()
    finally:
        handle.close()
    item.size = size
    upload.total += size


async def _read_submission(request: web.Request) -> _Upload:
    """Тело multipart целиком: тексты — в память (с лимитом), файлы — потоком во временные файлы с подсчётом
    байт. Превышение лимитов (в т.ч. числа частей формы) — 413 сразу; связь замолчала дольше
    UPLOAD_IDLE_TIMEOUT_SEC или всё тело дольше UPLOAD_MAX_SEC — 408. Ошибка — временные файлы удаляются."""
    if request.content_type != "multipart/form-data":
        raise _bad(NOT_MULTIPART)
    upload = _Upload()
    max_parts = MAX_FILES + len(_TEXT_PARTS) + EXTRA_PARTS
    try:
        async with asyncio.timeout(None) as timeout:
            deadline = _ReadDeadline(timeout)
            reader = await request.multipart()
            parts = 0
            while (part := await reader.next()) is not None:
                deadline.touch()
                parts += 1
                if parts > max_parts:
                    raise ApiError(413, "too_large", TOO_MANY_PARTS)
                if not isinstance(part, BodyPartReader):
                    raise _bad(BAD_FORM)
                name = part.name or ""
                if name in _TEXT_PARTS:
                    if name in upload.texts:
                        raise _bad(f"Поле «{name}» указано дважды")
                    upload.texts[name] = await _read_text_part(part, name, deadline)
                elif name in _FILE_PARTS:
                    await _read_file_part(part, upload, deadline)
                else:
                    raise _bad(f"Неизвестное поле «{name}»")
    except TimeoutError:
        upload.cleanup()
        log.info("API: тело сдачи не дочитано — связь с клиентом замолчала или чтение слишком долгое")
        raise ApiError(408, "request_timeout", UPLOAD_TIMEOUT) from None
    except ApiError:
        upload.cleanup()
        raise
    except Exception as exc:  # битый multipart, обрыв соединения клиента
        upload.cleanup()
        log.info("API: не удалось прочитать форму сдачи (%s)", type(exc).__name__)
        raise _bad(BAD_FORM) from None
    except BaseException:
        upload.cleanup()
        raise
    return upload


@dataclass(frozen=True)
class _SubmitTexts:
    fact_text: str
    result_text: str | None
    fact_value: float | None
    materials_text: str | None


def _submit_texts(texts: dict[str, str]) -> _SubmitTexts:
    fact = (texts.get("fact_text") or "").strip()
    if len(fact) > TEXT_MAX:
        raise _bad(f"Слишком длинно: {len(fact)} символов. Сократите до {TEXT_MAX} и отправьте ещё раз.")
    if len(fact) < FACT_MIN:
        raise _bad(FACT_TOO_SHORT)
    result = (texts.get("result_text") or "").strip() or None
    if result is not None and len(result) > TEXT_MAX:
        raise _bad(f"Слишком длинно: {len(result)} символов. Сократите до {TEXT_MAX} и отправьте ещё раз.")
    materials = (texts.get("materials_text") or "").strip() or None
    if materials is not None and len(materials) > NOTES_MAX:
        raise _bad(f"Где лежат материалы: до {NOTES_MAX} символов")
    value = parse_number_input((texts.get("fact_value") or "").strip(), "Фактическое значение")
    if value is not None and value < 0:
        raise _bad("Фактическое значение не может быть отрицательным")
    return _SubmitTexts(fact_text=fact, result_text=result, fact_value=value, materials_text=materials)


def _is_photo(item: _UploadedFile) -> bool:
    return item.content_type in _PHOTO_TYPES or Path(item.name).suffix.lower() in _PHOTO_EXTENSIONS


async def _tg_call(action: Callable[[], Awaitable[T]]) -> T:
    """Запрос к Telegram; флуд-лимит до RETRY_AFTER_MAX_SEC — подождать и повторить один раз."""
    try:
        return await action()
    except TelegramRetryAfter as exc:
        if exc.retry_after > RETRY_AFTER_MAX_SEC:
            raise
        await asyncio.sleep(exc.retry_after)
    return await action()


def _attachment_from(message: Message, item: _UploadedFile) -> tasks_svc.AttachmentIn:
    """Файл из ответа Telegram на sendDocument: document, а если Telegram вернул видео — VIDEO, прочее — OTHER."""
    if message.document is not None:
        kind, media = AttachmentKind.DOCUMENT, message.document
    elif message.video is not None:
        kind, media = AttachmentKind.VIDEO, message.video
    else:
        media = message.animation or message.audio or message.voice
        if media is None:
            log.warning("API: в ответе Telegram на sendDocument нет файла")
            raise ApiError(502, "telegram_error", TG_FILE_FAILED.format(name=item.name))
        kind = AttachmentKind.OTHER
    return tasks_svc.AttachmentIn(
        kind=kind,
        file_id=media.file_id,
        file_unique_id=media.file_unique_id,
        file_name=item.name,
        mime_type=getattr(media, "mime_type", None) or item.content_type,
        file_size=media.file_size if media.file_size is not None else item.size,
    )


async def _upload_one(bot: Bot, chat_id: int, item: _UploadedFile, caption: str) -> tasks_svc.AttachmentIn:
    """Один файл — в личный чат сотрудника с ботом: фото (≤ PHOTO_MAX_BYTES) или документ."""
    try:
        if _is_photo(item) and item.size <= PHOTO_MAX_BYTES:
            try:
                message = await _tg_call(
                    lambda: bot.send_photo(
                        chat_id, FSInputFile(item.path, filename=item.name), caption=caption, disable_notification=True
                    )
                )
            except TelegramBadRequest:
                log.info("API: Telegram не принял файл как фото — отправляю документом")
            else:
                if message.photo:
                    photo = message.photo[-1]
                    return tasks_svc.AttachmentIn(
                        kind=AttachmentKind.PHOTO,
                        file_id=photo.file_id,
                        file_unique_id=photo.file_unique_id,
                        file_name=item.name,
                        mime_type="image/jpeg",
                        file_size=photo.file_size,
                    )
        message = await _tg_call(
            lambda: bot.send_document(
                chat_id,
                FSInputFile(item.path, filename=item.name),
                caption=caption,
                disable_notification=True,
                disable_content_type_detection=True,
            )
        )
        return _attachment_from(message, item)
    except TelegramForbiddenError as exc:
        raise ApiError(502, "telegram_error", TG_BLOCKED) from exc
    except (TelegramAPIError, OSError) as exc:
        log.warning("API: Telegram не принял файл сдачи (%s)", type(exc).__name__)
        raise ApiError(502, "telegram_error", TG_FILE_FAILED.format(name=item.name)) from exc


async def _upload_files(bot: Bot, chat_id: int, task_id: int, files: Sequence[_UploadedFile]) -> list[tasks_svc.AttachmentIn]:
    """Файлы — в Telegram параллельно, но не больше UPLOAD_CONCURRENCY сразу; порядок результата — как у
    файлов. Первая ошибка останавливает остальные (уже загруженные остаются в чате)."""
    if not files:
        return []
    caption = SUBMIT_CAPTION.format(task_id=task_id)
    semaphore = asyncio.Semaphore(max(1, UPLOAD_CONCURRENCY))

    async def one(item: _UploadedFile) -> tasks_svc.AttachmentIn:
        async with semaphore:
            return await _upload_one(bot, chat_id, item, caption)

    jobs = [asyncio.ensure_future(one(item)) for item in files]
    try:
        await asyncio.wait(jobs, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
    for job in jobs:
        if not job.cancelled() and job.exception() is not None:
            raise job.exception()  # type: ignore[misc]
    return [job.result() for job in jobs]


async def submit_result(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = active_viewer(request)
    session = _session(request)
    # 1. Права и состояние — до чтения тела.
    task = await _load_task(session, _path_id(request, "task_id", TASK_NOT_FOUND))
    if task.assignee_id != viewer.id:
        raise ApiError(403, "forbidden", NO_RIGHTS)
    if not task.is_open:
        raise DomainError(_not_open_text(task.status))
    task_id = task.id
    async with ctx.gate.hold("submit", viewer.tg_id, BUSY["submit"]):
        await session.commit()  # соединение свободно на время загрузки файлов
        # 2. Тело целиком, с проверкой: в Telegram ничего не уходит, пока всё не прочитано.
        upload = await _read_submission(request)
        try:
            texts = _submit_texts(upload.texts)
            # 3. Файлы — в личный чат сотрудника с ботом.
            attachments = await _upload_files(ctx.bot, viewer.tg_id, task_id, upload.files)
        finally:
            upload.cleanup()
        # 4. Сдача — тем же сервисом, что и в чате. Пока грузились файлы, задачу могли сдать из чата и
        # вернуть на доработку: перечитать её и её сдачи поверх объектов сессии (иначе переход статуса
        # проверялся бы по старому статусу, а номер попытки считался бы по старому списку сдач).
        await _refresh_task(session, task_id)
        notes = [texts.materials_text] if texts.materials_text else []
        try:
            sub = await tasks_svc.submit_result(
                session,
                task_id,
                viewer,
                fact_text=texts.fact_text,
                result_text=_result_with_notes(texts.result_text, notes),
                fact_value=texts.fact_value,
                attachments=attachments,
            )
        except DomainError as exc:
            # Задачу отменили (или сдали из чата), пока грузились файлы: объяснить по свежему статусу.
            raise DomainError(_not_open_text(task.status) if not task.is_open else exc.message) from exc
        await session.commit()
        sub_id, attempt, count = sub.id, sub.attempt, len(sub.attachments)
    # 5. Оценка и уведомление начальнику — в фоне, общим конвейером чата.
    ctx.tasks.spawn(_after_submit(ctx, task_id, sub_id), name=f"webapp-submit-{sub_id}")
    return _ok(
        {
            "task_id": task_id,
            "submission_id": sub_id,
            "attempt": attempt,
            "files": count,
            "status": TaskStatus.SUBMITTED.value,
            "evaluation": "pending",
        },
        status=202,
    )


async def _refresh_task(session: AsyncSession, task_id: int) -> None:
    """Задача и её сдачи — заново из базы поверх объектов сессии (populate_existing доходит и до жадно
    загружаемых связей: списка сдач и файлов)."""
    stmt = select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
    await session.execute(stmt)


def _result_with_notes(result: str | None, notes: Sequence[str]) -> str | None:
    from bot.services import submission_flow

    return submission_flow.result_with_notes(result, notes)


async def _after_submit(ctx: WebappContext, task_id: int, sub_id: int) -> None:
    """Фон: оценка (AI или правила) и уведомление начальнику — submission_flow, как в чате. Остановка
    сервера посреди оценки — сдачу доведёт jobs.recover_stalled_evaluations."""
    from bot.services import submission_flow

    async with ctx.sessionmaker() as session:
        sub = await tasks_svc.get_submission(session, sub_id)
        # Оценка (скачивание файлов + цепочка AI) идёт до пары минут: соединение — обратно в пул до неё,
        # как в чате (там commit сразу после submit_result). Объекты остаются в памяти (expire_on_commit=False).
        await session.commit()
        if sub is not None:
            await submission_flow.run_after_submit(ctx.bot, session, sub.task, sub)
        else:
            log.warning("Сдача #%s задачи #%s не найдена для оценки", sub_id, task_id)


# --- Проверка результатов (§8.7) -----------------------------------------------------------------------------


async def list_review(request: web.Request) -> web.Response:
    viewer = manager_viewer(request)
    session = _session(request)
    now = utcnow()
    items = []
    for task in await tasks_svc.list_for_review(session):
        last = task.last_submission
        items.append(
            {
                "task": ser.task_detail(task, viewer, now),
                "submission": ser.submission(task, last, manager_view=True) if last is not None else None,
            }
        )
    return _ok({"items": items})


async def _load_submission(request: web.Request) -> Submission:
    sub = await tasks_svc.get_submission(_session(request), _path_id(request, "sub_id", SUB_NOT_FOUND))
    if sub is None:
        raise ApiError(404, "not_found", SUB_NOT_FOUND)
    return sub


async def review_confirm(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = manager_viewer(request)
    session = _session(request)
    sub = await _load_submission(request)
    task = await tasks_svc.review_confirm(session, sub.id, viewer)
    await session.commit()
    delivered = await notify.notify_review_result(ctx.bot, task, sub)
    return _ok({"task": ser.task_detail(task, viewer, utcnow()), "delivered": bool(delivered), "notice": _notice(delivered)})


def _score(value: Any) -> float:
    max_score = get_settings().max_score
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad("Оценка: ожидается число")
    if not math.isfinite(value) or not 0 <= value <= max_score:
        raise _bad(f"Оценка должна быть от 0 до {max_score} %")
    # Как в чате (task_review._parse_score): оценки везде показываются целыми — и хранятся так же.
    return float(math.floor(value + 0.5))


async def review_score(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = manager_viewer(request)
    session = _session(request)
    sub = await _load_submission(request)
    data = await _body(request, ("score", "comment"))
    score = _score(data.get("score"))
    comment = _text(data, "comment", "Комментарий", max_len=COMMENT_MAX)
    task = await tasks_svc.review_set_score(session, sub.id, viewer, score, comment)
    await session.commit()
    delivered = await notify.notify_review_result(ctx.bot, task, sub)
    return _ok({"task": ser.task_detail(task, viewer, utcnow()), "delivered": bool(delivered), "notice": _notice(delivered)})


async def review_revise(request: web.Request) -> web.Response:
    """Изменить оценку, подтверждённую автоматически (в течение AUTO_REVISE_DAYS) — как SubCB("revise") в чате."""
    ctx = _ctx(request)
    viewer = manager_viewer(request)
    session = _session(request)
    sub = await _load_submission(request)
    data = await _body(request, ("score", "comment"))
    score = _score(data.get("score"))
    comment = _text(data, "comment", "Комментарий", max_len=COMMENT_MAX)
    task = await tasks_svc.review_revise_auto(session, sub.id, viewer, score, comment)
    await session.commit()
    delivered = await notify.notify_review_result(ctx.bot, task, sub)
    return _ok({"task": ser.task_detail(task, viewer, utcnow()), "delivered": bool(delivered), "notice": _notice(delivered)})


async def review_rework(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = manager_viewer(request)
    session = _session(request)
    sub = await _load_submission(request)
    data = await _body(request, ("comment", "deadline"))
    comment = _text(data, "comment", "Комментарий", max_len=COMMENT_MAX, required=True) or ""
    raw_deadline = data.get("deadline")
    new_deadline = parse_deadline_input(raw_deadline) if raw_deadline not in (None, "") else None
    task = sub.task
    reviewable = task.status == TaskStatus.SUBMITTED and task.last_submission is sub and sub.decision is None
    if new_deadline is None and reviewable and task.deadline <= utcnow():
        raise DomainError(DEADLINE_PASSED)  # правило чата task_review._finish_rework
    task = await tasks_svc.review_rework(session, sub.id, viewer, comment, new_deadline)
    await session.commit()
    delivered = await notify.notify_rework(ctx.bot, task, sub)
    return _ok({"task": ser.task_detail(task, viewer, utcnow()), "delivered": bool(delivered), "notice": _notice(delivered)})


async def review_files(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = manager_viewer(request)
    session = _session(request)
    sub = await _load_submission(request)
    if not sub.attachments:
        raise DomainError(NO_FILES)
    if not ctx.gate.try_enter("files", viewer.tg_id):
        raise ApiError(429, "busy", BUSY["files"])
    try:
        await session.commit()
        ctx.tasks.spawn(_send_files_job(ctx, viewer.tg_id, sub), name=f"webapp-files-{sub.id}")
    except BaseException:
        ctx.gate.leave("files", viewer.tg_id)
        raise
    return _ok({"count": len(sub.attachments)}, status=202)


async def _send_files_job(ctx: WebappContext, chat_id: int, sub: Submission) -> None:
    """Фон: файлы сдачи — в чат начальника (как «📎 Файлы» в чате). Сдача и файлы уже загружены — к базе
    фон не обращается. «Занято» снимается в конце."""
    try:
        await notify.send_attachments(ctx.bot, chat_id, sub)
    finally:
        ctx.gate.leave("files", chat_id)


# --- KPI и отчёты (§8.8) --------------------------------------------------------------------------------------


def _period_params(request: web.Request) -> tuple[str, int]:
    """kind (по умолчанию неделя; неизвестный — 400) и offset (> 0 -> 0, < -500 -> -500)."""
    kind = request.query.get("kind") or "week"
    if kind not in periods.PERIOD_KINDS:
        raise _bad("Параметр «kind»: week, month, quarter или year")
    raw = (request.query.get("offset") or "0").strip()
    if not _INT_RE.fullmatch(raw):
        raise _bad("Параметр «offset»: ожидается целое число")
    return kind, max(-MAX_BACK_OFFSET, min(int(raw), 0))


def _trend_weeks(now: datetime) -> list[periods.Period]:
    return [periods.get_period("week", offset, now) for offset in range(-(TREND_WEEKS - 1), 1)]


async def _team_dashboard(session: AsyncSession, kind: str, offset: int, now: datetime) -> dict[str, Any]:
    """Дашборд начальника: те же числа и порядок строк, что kpi.kpi_for_team / kpi.team_kpi."""
    period = periods.get_period(kind, offset, now)
    weeks = _trend_weeks(now)
    employees = list(
        await session.scalars(
            select(User)
            .where(User.status == UserStatus.ACTIVE, User.role == Role.EMPLOYEE)
            .order_by(User.full_name, User.id)
        )
    )
    ids = [user.id for user in employees]
    snaps = await _snapshots_for(session, [(period.start, period.end), (weeks[0].start, weeks[-1].end)], ids)

    by_user: dict[int, list[tuple[int, TaskSnapshot]]] = defaultdict(list)
    for owner, snap in snaps:
        by_user[owner].append((owner, snap))

    def team_rows(start: datetime, end: datetime) -> list[tuple[User, KpiResult]]:
        rows = [(user, kpi_in(by_user[user.id], start, end, now)) for user in employees]
        rows.sort(key=lambda row: (row[1].kpi is None, -(row[1].kpi or 0.0), row[0].full_name))
        return rows

    rows = team_rows(period.start, period.end)
    team = kpi.team_kpi(rows)
    # Точка тренда — KPI команды за неделю (kpi.team_kpi по строкам, отсортированным как kpi_for_team).
    points = [(week, kpi.team_kpi(team_rows(week.start, week.end))) for week in weeks]
    return {
        "scope": "team",
        "period": ser.period(period, MAX_BACK_OFFSET),
        "team": {"kpi": team, "kpi_text": ser.kpi_text(team)},
        "totals": {
            "employees": len(rows),
            "total": sum(res.total for _, res in rows),
            "done": sum(res.done for _, res in rows),
            "overdue_total": sum(res.overdue_total for _, res in rows),
            "in_progress": sum(res.in_progress for _, res in rows),
            "on_review": sum(res.on_review for _, res in rows),
        },
        "rows": [
            {"user": ser.user_ref(user), "kpi": res.kpi, "kpi_text": ser.kpi_text(res.kpi), "stats": ser.kpi_stats(res)}
            for user, res in rows
        ],
        "trend": ser.trend(points),
    }


async def _person_kpi(session: AsyncSession, user: User, kind: str, offset: int, now: datetime) -> dict[str, Any]:
    """KPI сотрудника: выбранный период, текущие неделя и месяц (как employee_card) и тренд — обычно одним
    запросом снимков (два, если период далеко от последних недель)."""
    period = periods.get_period(kind, offset, now)
    week = periods.get_period("week", 0, now)
    month = periods.get_period("month", 0, now)
    weeks = _trend_weeks(now)
    ranges = [(period.start, period.end), (weeks[0].start, weeks[-1].end), (week.start, week.end), (month.start, month.end)]
    snaps = await _snapshots_for(session, ranges, [user.id])
    current = kpi_in(snaps, period.start, period.end, now, user.id)
    return {
        "period": ser.period(period, MAX_BACK_OFFSET),
        "user": ser.user_ref(user),
        "current": ser.kpi_block(current),
        "week": ser.kpi_short(kpi_in(snaps, week.start, week.end, now, user.id)),
        "month": ser.kpi_short(kpi_in(snaps, month.start, month.end, now, user.id)),
        "trend": ser.trend([(w, kpi_in(snaps, w.start, w.end, now, user.id).kpi) for w in weeks]),
    }


async def dashboard(request: web.Request) -> web.Response:
    viewer = active_viewer(request)
    session = _session(request)
    kind, offset = _period_params(request)
    now = utcnow()
    if viewer.is_manager:
        return _ok(await _team_dashboard(session, kind, offset, now))
    return _ok({"scope": "self", **await _person_kpi(session, viewer, kind, offset, now)})


async def user_kpi(request: web.Request) -> web.Response:
    viewer = active_viewer(request)
    session = _session(request)
    user_id = _path_id(request, "user_id", USER_NOT_FOUND)
    if not viewer.is_manager and user_id != viewer.id:
        raise ApiError(403, "forbidden", NO_RIGHTS)
    target = viewer if user_id == viewer.id else await users_svc.get_user(session, user_id)
    if target is None:
        raise ApiError(404, "not_found", USER_NOT_FOUND)
    kind, offset = _period_params(request)
    page = _query_int(request, "page", 0, low=0, high=10**6)
    now = utcnow()
    data = await _person_kpi(session, target, kind, offset, now)
    items, total = await _history_page(
        session, target.id, limit=HISTORY_PAGE_SIZE, offset=page * HISTORY_PAGE_SIZE
    )
    if total is None:  # страница за пределами — итог отдельным запросом
        total = await tasks_svc.count_tasks(session, assignee_id=target.id, statuses=[TaskStatus.DONE])
    data["history"] = {
        "items": [ser.history_item(row) for row in items],
        "page": page,
        "pages": max(1, math.ceil(total / HISTORY_PAGE_SIZE)),
        "total": total,
        "page_size": HISTORY_PAGE_SIZE,
    }
    return _ok({"scope": "user", **data})


async def export_report(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    viewer = manager_viewer(request)
    session = _session(request)
    kind, offset = _period_params(request)
    now = utcnow()
    period = periods.get_period(kind, offset, now)
    if not ctx.gate.try_enter("export", viewer.tg_id):
        raise ApiError(429, "busy", BUSY["export"])
    try:
        await session.commit()
        ctx.tasks.spawn(_export_job(ctx, viewer.tg_id, period, now), name=f"webapp-export-{viewer.tg_id}")
    except BaseException:
        ctx.gate.leave("export", viewer.tg_id)
        raise
    return _ok(
        {
            "status": "started",
            "period": ser.period(period, MAX_BACK_OFFSET),
            "message": EXPORT_STARTED.format(label=period.label),
        },
        status=202,
    )


async def _export_job(ctx: WebappContext, chat_id: int, period: periods.Period, now: datetime) -> None:
    """Фон: Excel-отчёт за период — документом в чат начальника (как dashboard.export_report)."""
    bot = ctx.bot
    try:
        try:
            async with ctx.sessionmaker() as session:
                data = await export_service.build_report_xlsx(session, period, now)
        except DomainError as exc:
            await notify.safe_send(bot, chat_id, f"⚠️ {esc(exc.message)}")
            return
        except Exception:  # noqa: BLE001 - начальнику — понятный текст, подробности — в лог
            log.exception("Не удалось собрать Excel-отчёт (%s, offset=%s)", period.kind, period.offset)
            await notify.safe_send(bot, chat_id, EXPORT_FAILED)
            return
        # Дата начала периода — по местному времени (в UTC неделя начинается ещё «вчера»).
        filename = f"kpi_{period.kind}_{to_local(period.start):%Y%m%d}.xlsx"
        try:
            await bot.send_document(
                chat_id, BufferedInputFile(data, filename=filename), caption=f"📊 Отчёт: {esc(period.label)}"
            )
        except (TelegramAPIError, OSError) as exc:
            log.warning("Не удалось отправить Excel-отчёт (%s)", type(exc).__name__)
            await notify.safe_send(bot, chat_id, EXPORT_SEND_FAILED)
    finally:
        ctx.gate.leave("export", chat_id)


# --- Сотрудники и загрузка недели (§8.9) ---------------------------------------------------------------------


async def list_employees(request: web.Request) -> web.Response:
    manager_viewer(request)
    users = await users_svc.list_employees(_session(request))
    return _ok({"items": [ser.user_ref(user) for user in users]})


async def weight_load(request: web.Request) -> web.Response:
    manager_viewer(request)
    session = _session(request)
    user_id = _path_id(request, "user_id", USER_NOT_FOUND)
    try:
        deadline = parse_deadline_input(request.query.get("deadline"))
        exclude = _query_id(request, "exclude_task_id", required=False)
    except ApiError:
        # Порядок проверок §6.2: сначала «нет сотрудника» (запрос — только на этой ветке ошибки).
        if await users_svc.get_user(session, user_id) is None:
            raise ApiError(404, "not_found", USER_NOT_FOUND) from None
        raise
    week = periods.get_period("week", 0, deadline)  # местные пн–вс недели срока, как tasks.weight_load
    load = await _weight_load_row(session, user_id, week.start, week.end, exclude)
    if load is None:
        raise ApiError(404, "not_found", USER_NOT_FOUND)
    return _ok(
        {
            "load": load,
            "week_label": week.label,
            "options": [{"weight": weight, "over": load + weight > 100} for weight in WEIGHT_OPTIONS],
        }
    )


# --- Маршруты (§8.2) -----------------------------------------------------------------------------------------

_TABLE: list[tuple[str, str, Handler]] = [
    ("GET", "/me", get_me),
    ("GET", "/tasks", list_tasks),
    ("POST", "/tasks", create_task),
    ("GET", "/tasks/{task_id}", get_task),
    ("PATCH", "/tasks/{task_id}", update_task),
    ("POST", "/tasks/{task_id}/accept", accept_task),
    ("POST", "/tasks/{task_id}/cancel", cancel_task),
    ("POST", "/tasks/{task_id}/submit", submit_result),
    ("POST", "/tasks/{task_id}/approve", approve_proposal),
    ("POST", "/tasks/{task_id}/reject", reject_proposal),
    ("POST", "/ai/formulate", ai_formulate),
    ("POST", "/voice", voice_input),
    ("GET", "/review", list_review),
    ("POST", "/submissions/{sub_id}/confirm", review_confirm),
    ("POST", "/submissions/{sub_id}/score", review_score),
    ("POST", "/submissions/{sub_id}/revise", review_revise),
    ("POST", "/submissions/{sub_id}/rework", review_rework),
    ("POST", "/submissions/{sub_id}/files", review_files),
    ("GET", "/proposals", list_proposals),
    ("POST", "/proposals", create_proposal),
    ("GET", "/dashboard", dashboard),
    ("GET", "/users/{user_id}/kpi", user_kpi),
    ("GET", "/employees", list_employees),
    ("GET", "/employees/{user_id}/weight-load", weight_load),
    ("POST", "/export", export_report),
]

# Все маршруты API: (метод, шаблон полного пути). Тест SPA сверяет с ними пути в app.js.
ROUTES: list[tuple[str, str]] = [(method, "/api" + path) for method, path, _ in _TABLE]
# Обработчики, которые читают тело сами, потоком и после commit (сдача с файлами): middleware его не читает.
_STREAMING_HANDLERS: frozenset[Handler] = frozenset({submit_result, voice_input})


def build_api_app(ctx: WebappContext) -> web.Application:
    """Sub-app /api: middleware ошибок и входа, маршруты ROUTES. Неизвестный путь и метод проходят через
    middleware sub-app — ответ JSON 404 / 405 (без initData — 401: вход проверяется раньше)."""
    api = web.Application(middlewares=_make_middlewares(ctx))
    api[CTX] = ctx
    for method, path, handler in _TABLE:
        if method == "GET":
            api.router.add_get(path, handler)
        else:
            api.router.add_route(method, path, handler)
    return api
