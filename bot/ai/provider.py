"""Цепочка бесплатных AI-провайдеров: Gemini → Groq → Cloudflare → Mistral → OpenRouter → правила.

``generate_json`` пробует провайдеров из settings.ai_providers по порядку — только тех, у кого задан
ключ, — и у каждого его модели по порядку. Следующая модель / провайдер берётся, если модель:

* упёрлась в бесплатный лимит (429), бесплатный тариф ей недоступен (нужна оплата, «limit: 0», 402/403,
  FAILED_PRECONDITION), её нет (404), сервис перегружен (5xx), не ответила вовремя, недоступна сеть;
* ответила пусто, не JSON или не по схеме (нет обязательных полей, не тот тип).

Неверный ключ, дневной лимит на весь аккаунт, API закрыт для всего проекта (регион не поддерживается,
API не включён, нужен биллинг без указания модели) и прочие ошибки запроса (4xx) пропускают сразу всех
оставшихся моделей этого провайдера. Если не ответил никто — AIUnavailable, и вызывающий код
(formulate, evaluate) считает по правилам.

Назначение запроса (``ai_purpose`` / ``purpose=``): «formulate» — подсказка формулировки (короткий ответ,
важна скорость), «evaluate» — оценка сдачи (важно суждение). От него зависят порядок моделей Gemini
(``Settings.ai_models_for``: для формулировки — сначала быстрые flash-lite, для оценки — gemini-3.6-flash),
время одной попытки и всего перебора (``attempt_timeout_sec``, ``chain_budget_sec``).

Паузы. Модель или провайдер, которые только что отказали из-за лимита / оплаты / ключа / перегрузки,
какое-то время не спрашиваются (``cooldown_sec``): иначе каждый запрос сначала стучался бы в заведомо
закрытую дверь и ждал отказа. Лимит — 10 минут (или сколько сказал сервис; дневной — не меньше часа),
оплата / ключ / нет модели — 6 часов, перегрузка (503 «high demand» и т. п.) и таймаут — 4 минуты.
Таймаут ставит паузу, только если попытке досталось полное время (урезанная остатком времени попытка паузы
не ставит), и только для попыток с тем же или меньшим временем: модель, не успевшая за 9 с подсказки
формулировки, для оценки сдачи (30 с) спрашивается. Запрос слишком большой для модели — без паузы,
следующая модель. Паузы живут в памяти процесса: после перезапуска бота (например, когда в настройках
поменяли ключ) всё пробуется заново.

Время. Весь перебор укладывается в ``chain_budget_sec()`` (2 × AI_TIMEOUT_SEC; для формулировки —
AI_FORMULATE_BUDGET_SEC) или в ``time_budget`` вызывающего кода, если он меньше; ожидание свободного места
у провайдера (не больше 2 запросов сразу на каждое назначение — подсказка не ждёт чужих оценок) входит в это
время. Каждая попытка — не дольше ``attempt_timeout_sec()`` (AI_TIMEOUT_SEC + 5 с; для формулировки —
AI_FORMULATE_TIMEOUT_SEC, для оценки без файлов — AI_EVALUATE_TIMEOUT_SEC) и не дольше остатка времени. Провайдер,
который не ответил вовремя или недоступен по сети, уходит в конец очереди — оставшееся время сначала
получают другие.

Ключи API никогда не попадают в журнал: тексты ошибок проходят через ``_safe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal, NamedTuple, Protocol

from bot.ai import gemini, openai_compat
from bot.ai.base import MIN_OUTPUT_TOKENS, Failure, ProviderError
from bot.config import Settings, get_settings

__all__ = [
    "AIUnavailable",
    "MIN_OUTPUT_TOKENS",
    "Purpose",
    "ai_available",
    "ai_purpose",
    "active_chain",
    "attempt_timeout_sec",
    "chain_budget_sec",
    "close_client",
    "generate_json",
    "reset_state",
]

logger = logging.getLogger(__name__)

Purpose = Literal["formulate", "evaluate", "transcribe"]

# Не больше двух одновременных запросов к одному провайдеру на каждое назначение — чтобы не упираться
# в бесплатные лимиты, но и чтобы быстрая подсказка формулировки не ждала, пока закончатся чужие оценки.
_CONCURRENCY = 2
_MIN_ATTEMPT_SEC = 3.0  # меньше этого времени осталось — новую попытку не начинаем

# Паузы после отказа, секунды.
QUOTA_COOLDOWN_SEC = 10 * 60
DAILY_QUOTA_COOLDOWN_SEC = 60 * 60
LONG_COOLDOWN_SEC = 6 * 60 * 60
# Перегрузка (5xx, 503 «high demand») и таймаут: модель, которая только что «не тянула», несколько минут
# не спрашивается — иначе каждый запрос сначала ждал бы от неё отказа (у gemini-3.8-flash — секунды).
OVERLOAD_COOLDOWN_SEC = 4 * 60
_MIN_QUOTA_COOLDOWN_SEC = 30

_PROVIDER_WIDE = "*"
_CODE_FENCE_RE = re.compile(r"^```[\w-]*\s*|\s*```$")
_MAX_JSON_STARTS = 20  # сколько «{» в ответе пробовать как начало JSON-объекта
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class _Pause(NamedTuple):
    until: float   # момент окончания паузы (_now)
    reason: str    # класс сбоя для журнала («сервис перегружен»)
    limit: float   # пауза действует на попытки не дольше limit секунд (таймаут); math.inf — на все


# Часы для пауз (тесты подменяют).
_now: Callable[[], float] = time.monotonic
# (провайдер, модель или "*") -> действующие паузы.
_cooldowns: dict[tuple[str, str], list[_Pause]] = {}
# (провайдер, назначение) -> места для одновременных запросов.
_semaphores: dict[tuple[str, str], asyncio.Semaphore] = {}
# Назначение текущего запроса (ai_purpose), если вызывающий код не передал purpose= явно.
_purpose: ContextVar[Purpose | None] = ContextVar("ai_purpose", default=None)


class AIUnavailable(Exception):
    """AI недоступен: выключен, нет ключа, исчерпаны бесплатные лимиты, ошибка сети или плохой ответ."""


class Provider(Protocol):
    name: str
    title: str
    models: list[str]

    def supports_images(self, model: str) -> bool: ...

    def label(self, model: str) -> str: ...

    async def generate(
        self, *, model: str, system: str, parts: list, schema: dict, max_output_tokens: int, timeout: float
    ) -> str: ...


def ai_available() -> bool:
    """Включён ли AI: AI_PROVIDER не none и задан ключ хотя бы одного провайдера."""
    return get_settings().ai_enabled


@contextmanager
def ai_purpose(purpose: Purpose | None) -> Iterator[None]:
    """Назначение запросов к AI внутри блока: ``with ai_purpose("formulate"): await generate_json(...)``.

    Так назначение доходит до generate_json, не меняя его вызова (тесты подменяют generate_json своими
    функциями с прежней сигнатурой). Явный ``generate_json(purpose=...)`` важнее.
    """
    token = _purpose.set(purpose)
    try:
        yield
    finally:
        _purpose.reset(token)


def chain_budget_sec(settings: Settings | None = None, purpose: Purpose | None = None) -> float:
    """Сколько секунд generate_json может перебирать модели и провайдеров (потом — AIUnavailable).

    2 × AI_TIMEOUT_SEC; подсказке формулировки — не больше AI_FORMULATE_BUDGET_SEC (0 — без отдельного предела).
    """
    settings = settings or get_settings()
    budget = float(max(settings.ai_timeout_sec, 1) * 2)
    if purpose == "formulate" and settings.ai_formulate_budget_sec > 0:
        budget = min(budget, float(settings.ai_formulate_budget_sec))
    if purpose == "transcribe" and settings.ai_transcribe_budget_sec > 0:
        budget = min(budget, float(settings.ai_transcribe_budget_sec))
    return budget


def attempt_timeout_sec(
    settings: Settings | None = None, purpose: Purpose | None = None, *, with_files: bool = False
) -> float:
    """Сколько секунд ждать ответа одной модели: AI_TIMEOUT_SEC + 5 с, а для формулировки и оценки — не
    больше AI_FORMULATE_TIMEOUT_SEC / AI_EVALUATE_TIMEOUT_SEC (0 — без отдельного предела).

    with_files — в запросе есть файлы (PDF, изображения): их чтение моделью может занять заметно дольше,
    поэтому оценке с файлами — полное AI_TIMEOUT_SEC + 5 с, как раньше.
    """
    settings = settings or get_settings()
    limit = float(settings.ai_timeout_sec + 5)
    own = {
        "formulate": settings.ai_formulate_timeout_sec,
        "evaluate": settings.ai_evaluate_timeout_sec,
        "transcribe": settings.ai_transcribe_timeout_sec,
    }.get(purpose or "")
    if own is not None and own > 0 and not (with_files and purpose == "evaluate"):
        limit = min(limit, float(own))
    return limit


def active_chain(settings: Settings | None = None, purpose: Purpose | None = None) -> list[Provider]:
    """Провайдеры с ключами в порядке AI_PROVIDERS (пусто, если AI выключен); порядок моделей — для purpose."""
    settings = settings or get_settings()
    if not settings.ai_enabled:
        return []
    vision = {model.strip() for model in settings.ai_vision_models if model.strip()}
    chain: list[Provider] = []
    for name in settings.active_ai_providers:
        models = settings.ai_models_for(name, purpose)
        if name == "gemini":
            chain.append(gemini.GeminiProvider(settings.gemini_api_key.strip(), models))
        else:
            chain.append(
                openai_compat.OpenAICompatProvider(
                    openai_compat.SPECS[name],
                    api_key=settings.ai_key_for(name),
                    models=models,
                    vision_models=vision,
                    account_id=settings.cloudflare_account_id,
                )
            )
    return chain


async def generate_json(
    *,
    system: str,
    parts: list,
    schema: dict,
    max_output_tokens: int = MIN_OUTPUT_TOKENS,
    time_budget: float | None = None,
    purpose: Purpose | None = None,
) -> tuple[dict, str]:
    """Запросить у AI JSON-ответ по схеме. Возвращает (данные, имя_модели).

    parts — список строк и/или google.genai.types.Part (файлы-подтверждения). Для Gemini
    max_output_tokens меньше MIN_OUTPUT_TOKENS поднимается до него. time_budget — сколько секунд есть
    у вызывающего кода (None — chain_budget_sec()); перебор укладывается в меньшее. purpose — назначение
    запроса (None — из ai_purpose, иначе общий порядок и время). Бросает AIUnavailable.
    """
    settings = get_settings()
    if not settings.ai_enabled:
        raise AIUnavailable("AI отключён в настройках или не задан ни один ключ")
    purpose = purpose or _purpose.get()
    chain = active_chain(settings, purpose)
    if not chain:
        raise AIUnavailable("Не задан список моделей AI")

    has_images = _has_images(parts)
    has_audio = _has_audio(parts)
    queue: deque[tuple[Provider, str]] = deque(
        (provider, model)
        for provider in chain
        for model in _ordered_models(provider, has_images)
        # Звук понимают не все: остальные модели и провайдеров для такого запроса не спрашиваем.
        if not has_audio or _supports_audio(provider, model)
    )
    if not queue:
        raise AIUnavailable("Нет модели AI, которая принимает звук")
    budget = chain_budget_sec(settings, purpose)
    if time_budget is not None:
        budget = min(budget, time_budget)
    started = _now()
    deadline = started + budget
    attempt_limit = attempt_timeout_sec(settings, purpose, with_files=_has_files(parts))
    reasons: list[str] = []
    paused: list[str] = []
    skipped: set[str] = set()   # провайдеры, которых в этом запросе больше не спрашиваем
    deferred: set[str] = set()  # провайдеры, уже отправленные в конец очереди

    while queue:
        provider, model = queue.popleft()
        if provider.name in skipped:
            continue
        pause = _cooldown_reason(provider.name, model, attempt_limit)
        if pause:
            paused.append(f"{provider.label(model)}: {pause}")
            continue
        remaining = deadline - _now()
        if remaining < _MIN_ATTEMPT_SEC:
            reasons.append("время на ответ AI истекло")
            break
        semaphore = _semaphore(provider.name, purpose)
        # Ожидание свободного места у провайдера — тоже из общего времени.
        if not await _acquire(semaphore, remaining - _MIN_ATTEMPT_SEC):
            reasons.append("время на ответ AI истекло (провайдер занят другими запросами)")
            break
        # Время попытки считается после ожидания; урезанной попытке (меньше attempt_limit) таймаут
        # паузы не ставит: модель не обязательно медленная, ей просто не хватило остатка времени.
        timeout = max(min(attempt_limit, deadline - _now()), 0.0)
        shortened = timeout < attempt_limit
        attempt_started = _now()
        try:
            try:
                text = await provider.generate(
                    model=model,
                    system=system,
                    parts=parts,
                    schema=schema,
                    max_output_tokens=max_output_tokens,
                    timeout=timeout,
                )
            finally:
                semaphore.release()
            data = _parse_json(text)
            if data is None:
                raise ProviderError(Failure.BAD_ANSWER, "ответ не JSON-объект")
            problem = _schema_problem(data, schema)
            if problem:
                raise ProviderError(Failure.BAD_ANSWER, f"ответ не по схеме: {problem}")
        except ProviderError as err:
            reasons.append(f"{provider.label(model)}: {_safe(err.detail)}")
            pause_sec = 0.0 if err.kind == Failure.TIMEOUT and shortened else cooldown_sec(err)
            if pause_sec > 0:
                _set_cooldown(
                    provider.name,
                    model if err.scope == "model" else _PROVIDER_WIDE,
                    pause_sec,
                    err,
                    # Не ответила за attempt_limit — не спрашивать там, где ждём столько же или меньше.
                    limit=attempt_limit if err.kind == Failure.TIMEOUT else math.inf,
                )
            if err.scope == "provider":
                skipped.add(provider.name)
            elif err.kind in (Failure.TIMEOUT, Failure.NETWORK) and provider.name not in deferred:
                deferred.add(provider.name)
                _defer(queue, provider.name)
            logger.info(
                "AI %s: %s (%s)%s — %s",
                provider.label(model),
                _safe(err.detail),
                _fmt_seconds(_now() - attempt_started),
                f", пауза {_fmt_duration(pause_sec)}" if pause_sec > 0 else "",
                "пропускаю провайдера" if err.scope == "provider" else "пробую следующую модель",
            )
            continue
        except Exception as exc:  # noqa: BLE001 - ошибка в коде провайдера не должна лишать бота других провайдеров
            reasons.append(f"{provider.label(model)}: {type(exc).__name__}")
            skipped.add(provider.name)
            logger.warning(
                "AI %s: непредвиденная ошибка %s — пропускаю провайдера", provider.label(model), type(exc).__name__
            )
            continue
        logger.info(
            "AI %s%s: ответ за %s%s",
            provider.label(model),
            f" ({purpose})" if purpose else "",
            _fmt_seconds(_now() - attempt_started),
            f", всего {_fmt_seconds(_now() - started)} (до этого: {'; '.join(reasons)})" if reasons else "",
        )
        return data, provider.label(model)

    summary = "; ".join(reasons + ([f"на паузе: {', '.join(paused)}"] if paused else [])) or "нет доступных моделей"
    logger.warning("AI не ответил, считаю по правилам: %s", summary)
    raise AIUnavailable("Ни одна AI-модель не ответила: " + summary)


async def close_client() -> None:
    """Закрыть HTTP-сессии всех провайдеров (вызывать при остановке бота)."""
    await gemini.close_client()
    await openai_compat.close_http()


def reset_state() -> None:
    """Забыть паузы, семафоры и HTTP-клиент (тесты; паузы и так живут только до перезапуска процесса)."""
    _cooldowns.clear()
    _semaphores.clear()
    openai_compat._http = None  # noqa: SLF001 - новый клиент возьмёт текущий транспорт (в тестах — MockTransport)


def cooldown_sec(err: ProviderError) -> float:
    """На сколько секунд не спрашивать модель (или провайдера) после такого отказа; 0 — не делать паузу."""
    if err.kind == Failure.QUOTA:
        pause = float(QUOTA_COOLDOWN_SEC)
        if err.retry_after:
            pause = min(max(err.retry_after, _MIN_QUOTA_COOLDOWN_SEC), LONG_COOLDOWN_SEC)
        return max(pause, DAILY_QUOTA_COOLDOWN_SEC) if err.daily else pause
    if err.kind in (Failure.BILLING, Failure.AUTH, Failure.NOT_FOUND):
        return float(LONG_COOLDOWN_SEC)
    if err.kind in (Failure.OVERLOADED, Failure.TIMEOUT):
        return float(OVERLOAD_COOLDOWN_SEC)
    return 0.0


# --- Паузы и очередь -----------------------------------------------------------------------------------


def _set_cooldown(provider: str, model: str, seconds: float, err: ProviderError, *, limit: float = math.inf) -> None:
    """Пауза модели (или всего провайдера, model="*") на seconds; limit — для пауз по таймауту (см. _Pause)."""
    new = _Pause(_now() + seconds, err.kind.value, limit)
    key = (provider, model)
    pauses = _cooldowns.get(key, [])
    if any(old.until >= new.until and old.limit >= new.limit for old in pauses):
        return  # уже есть пауза не короче и не уже
    _cooldowns[key] = [old for old in pauses if not (old.until <= new.until and old.limit <= new.limit)] + [new]


def _cooldown_reason(provider: str, model: str, attempt_limit: float = 0.0) -> str | None:
    """Почему модель сейчас на паузе для попытки длиной attempt_limit секунд («лимит, ещё 9 мин»);
    None — можно спрашивать. attempt_limit=0 — на паузе ли модель хоть для каких-то попыток."""
    now = _now()
    for key in ((provider, _PROVIDER_WIDE), (provider, model)):
        pauses = [pause for pause in _cooldowns.get(key, ()) if pause.until > now]
        if not pauses:
            _cooldowns.pop(key, None)
            continue
        _cooldowns[key] = pauses
        active = [pause for pause in pauses if attempt_limit <= pause.limit]
        if active:
            longest = max(active, key=lambda pause: pause.until)
            return f"{longest.reason}, ещё {_fmt_duration(longest.until - now)}"
    return None


def _defer(queue: deque[tuple[Provider, str]], name: str) -> None:
    """Оставшиеся модели провайдера — в конец очереди (сначала спросить других)."""
    mine = [item for item in queue if item[0].name == name]
    others = [item for item in queue if item[0].name != name]
    queue.clear()
    queue.extend(others + mine)


def _semaphore(name: str, purpose: Purpose | None = None) -> asyncio.Semaphore:
    """Места для одновременных запросов к провайдеру — отдельно для каждого назначения запроса."""
    key = (name, purpose or "")
    semaphore = _semaphores.get(key)
    if semaphore is None:
        semaphore = _semaphores[key] = asyncio.Semaphore(_CONCURRENCY)
    return semaphore


async def _acquire(semaphore: asyncio.Semaphore, timeout: float) -> bool:
    """Занять место у провайдера, ожидая не дольше timeout секунд; False — не дождались."""
    if not semaphore.locked():
        await semaphore.acquire()  # свободно — сразу, без ожидания
        return True
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=max(timeout, 0.0))
    except TimeoutError:
        return False
    return True


def _ordered_models(provider: Provider, has_images: bool) -> list[str]:
    """Модели провайдера; если в запросе есть изображения — сначала те, что их видят (порядок внутри сохраняется)."""
    if not has_images:
        return list(provider.models)
    return sorted(provider.models, key=lambda model: not provider.supports_images(model))


def _has_files(parts: list) -> bool:
    """В запросе есть файлы байтами (PDF, изображения), а не только текст."""
    return any(getattr(part, "inline_data", None) is not None for part in parts)


def _has_audio(parts: list) -> bool:
    """В запросе есть звук (голосовое сообщение, запись из приложения)."""
    for part in parts:
        blob = getattr(part, "inline_data", None)
        if blob is not None and str(getattr(blob, "mime_type", "") or "").lower().startswith("audio/"):
            return True
    return False


def _supports_audio(provider: Provider, model: str) -> bool:
    """Модель провайдера принимает звук (у провайдера без метода supports_audio — нет)."""
    check = getattr(provider, "supports_audio", None)
    return bool(check(model)) if callable(check) else False


def _has_images(parts: list) -> bool:
    for part in parts:
        blob = getattr(part, "inline_data", None)
        if blob is not None and str(getattr(blob, "mime_type", "") or "").lower().startswith("image/"):
            return True
    return False


# --- Ответ модели ---------------------------------------------------------------------------------------


def _parse_json(text: str | None) -> dict | None:
    """Разобрать JSON-объект из ответа; снимает обёртку ```json … ``` и рассуждения <think>. None — если не вышло.

    Модели без строгого режима JSON (Gemma, json_object) иногда пишут текст до или после объекта
    («Ответ: {…}», «{…}\\nГотово.») — берётся первый целый JSON-объект в ответе.
    """
    if not text or not text.strip():
        return None
    cleaned = _CODE_FENCE_RE.sub("", _THINK_RE.sub("", text).strip()).strip()
    try:
        data = json.loads(cleaned)
    except ValueError:
        data = None
    if isinstance(data, dict):
        return data
    decoder = json.JSONDecoder()
    start = cleaned.find("{")
    for _ in range(_MAX_JSON_STARTS):
        if start < 0:
            break
        try:
            data, _end = decoder.raw_decode(cleaned, start)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data
        start = cleaned.find("{", start + 1)
    return None


def _schema_problem(data: dict, schema: dict) -> str | None:
    """Проверить ответ по схеме: обязательные поля есть, типы совпадают. None — всё в порядке.

    Мягко там, где вызывающий код всё равно разбирает значение сам: число строкой («110 %») — годится,
    отсутствующее поле, которое может быть null, — то же, что null; значения enum не проверяются.
    """
    properties: dict[str, Any] = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if data.get(key) is None and not _allows_null(properties.get(key)):
            return f"нет поля «{key}»"
    for key, spec in properties.items():
        if key in data and not _type_matches(data[key], spec):
            return f"поле «{key}» не того типа"
    return None


def _schema_types(spec: object) -> list[str]:
    if not isinstance(spec, dict):
        return []
    types = spec.get("type")
    if isinstance(types, str):
        return [types]
    return [item for item in types if isinstance(item, str)] if isinstance(types, list) else []


def _allows_null(spec: object) -> bool:
    types = _schema_types(spec)
    return not types or "null" in types


def _type_matches(value: object, spec: object) -> bool:
    types = _schema_types(spec)
    return not types or any(_is_type(value, name) for name in types)


def _is_type(value: object, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "string":
        return isinstance(value, str)
    if name in ("number", "integer"):
        if isinstance(value, bool):
            return False
        if isinstance(value, int | float):
            return math.isfinite(value)
        return isinstance(value, str) and any(char.isdigit() for char in value)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    return True


# --- Журнал ---------------------------------------------------------------------------------------------


def _fmt_seconds(seconds: float) -> str:
    """Время ответа для журнала: «1,2 с»."""
    return f"{max(seconds, 0.0):.1f} с".replace(".", ",")


def _fmt_duration(seconds: float) -> str:
    seconds = max(int(round(seconds)), 0)
    if seconds < 90:
        return f"{seconds} с"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч" + (f" {minutes} мин" if minutes else "")


def _safe(text: object) -> str:
    """Вырезать ключи API из текста для лога и обрезать слишком длинные сообщения."""
    result = str(text or "")
    for secret in get_settings().ai_secrets:
        result = result.replace(secret, "***")
    return result[:300]
