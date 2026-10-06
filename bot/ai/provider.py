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

Паузы. Модель или провайдер, которые только что отказали из-за лимита / оплаты / ключа, какое-то время
не спрашиваются (``cooldown_sec``): иначе каждый запрос сначала стучался бы в заведомо закрытую дверь.
Лимит — 10 минут (или сколько сказал сервис; дневной — не меньше часа), оплата / ключ / нет модели —
6 часов, перегрузка и таймаут — 2 минуты (таймаут — только если попытке досталось полное время: урезанная
остатком времени попытка паузы не ставит). Запрос слишком большой для модели — без паузы, следующая модель.
Паузы живут в памяти процесса: после перезапуска бота (например, когда в настройках поменяли ключ) всё
пробуется заново.

Время. Весь перебор укладывается в ``chain_budget_sec()`` (2 × AI_TIMEOUT_SEC) или в ``time_budget``
вызывающего кода, если он меньше; ожидание свободного места у провайдера (не больше 2 запросов сразу) входит
в это время. Каждая попытка — не дольше AI_TIMEOUT_SEC + 5 с и не дольше остатка времени. Провайдер, который
не ответил вовремя или недоступен по сети, уходит в конец очереди — оставшееся время сначала получают другие.

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
from collections.abc import Callable
from typing import Any, Protocol

from bot.ai import gemini, openai_compat
from bot.ai.base import MIN_OUTPUT_TOKENS, Failure, ProviderError
from bot.config import Settings, get_settings

__all__ = [
    "AIUnavailable",
    "MIN_OUTPUT_TOKENS",
    "ai_available",
    "active_chain",
    "chain_budget_sec",
    "close_client",
    "generate_json",
    "reset_state",
]

logger = logging.getLogger(__name__)

# Не больше двух одновременных запросов к одному провайдеру — чтобы не упираться в бесплатные лимиты.
_CONCURRENCY = 2
_MIN_ATTEMPT_SEC = 3.0  # меньше этого времени осталось — новую попытку не начинаем

# Паузы после отказа, секунды.
QUOTA_COOLDOWN_SEC = 10 * 60
DAILY_QUOTA_COOLDOWN_SEC = 60 * 60
LONG_COOLDOWN_SEC = 6 * 60 * 60
SHORT_COOLDOWN_SEC = 2 * 60
_MIN_QUOTA_COOLDOWN_SEC = 30

_PROVIDER_WIDE = "*"
_CODE_FENCE_RE = re.compile(r"^```[\w-]*\s*|\s*```$")
_MAX_JSON_STARTS = 20  # сколько «{» в ответе пробовать как начало JSON-объекта
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Часы для пауз (тесты подменяют).
_now: Callable[[], float] = time.monotonic
# (провайдер, модель или "*") -> (момент окончания паузы, причина).
_cooldowns: dict[tuple[str, str], tuple[float, str]] = {}
_semaphores: dict[str, asyncio.Semaphore] = {}


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


def chain_budget_sec(settings: Settings | None = None) -> float:
    """Сколько секунд generate_json может перебирать модели и провайдеров (потом — AIUnavailable)."""
    settings = settings or get_settings()
    return float(max(settings.ai_timeout_sec, 1) * 2)


def active_chain(settings: Settings | None = None) -> list[Provider]:
    """Провайдеры с ключами в порядке AI_PROVIDERS (пусто, если AI выключен)."""
    settings = settings or get_settings()
    if not settings.ai_enabled:
        return []
    vision = {model.strip() for model in settings.ai_vision_models if model.strip()}
    chain: list[Provider] = []
    for name in settings.active_ai_providers:
        models = settings.ai_models_for(name)
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
) -> tuple[dict, str]:
    """Запросить у AI JSON-ответ по схеме. Возвращает (данные, имя_модели).

    parts — список строк и/или google.genai.types.Part (файлы-подтверждения). Для Gemini
    max_output_tokens меньше MIN_OUTPUT_TOKENS поднимается до него. time_budget — сколько секунд есть
    у вызывающего кода (None — chain_budget_sec()); перебор укладывается в меньшее. Бросает AIUnavailable.
    """
    settings = get_settings()
    if not settings.ai_enabled:
        raise AIUnavailable("AI отключён в настройках или не задан ни один ключ")
    chain = active_chain(settings)
    if not chain:
        raise AIUnavailable("Не задан список моделей AI")

    has_images = _has_images(parts)
    queue: deque[tuple[Provider, str]] = deque(
        (provider, model) for provider in chain for model in _ordered_models(provider, has_images)
    )
    budget = chain_budget_sec(settings)
    if time_budget is not None:
        budget = min(budget, time_budget)
    deadline = _now() + budget
    attempt_limit = float(settings.ai_timeout_sec + 5)
    reasons: list[str] = []
    paused: list[str] = []
    skipped: set[str] = set()   # провайдеры, которых в этом запросе больше не спрашиваем
    deferred: set[str] = set()  # провайдеры, уже отправленные в конец очереди

    while queue:
        provider, model = queue.popleft()
        if provider.name in skipped:
            continue
        pause = _cooldown_reason(provider.name, model)
        if pause:
            paused.append(f"{provider.label(model)}: {pause}")
            continue
        remaining = deadline - _now()
        if remaining < _MIN_ATTEMPT_SEC:
            reasons.append("время на ответ AI истекло")
            break
        semaphore = _semaphore(provider.name)
        # Ожидание свободного места у провайдера — тоже из общего времени.
        if not await _acquire(semaphore, remaining - _MIN_ATTEMPT_SEC):
            reasons.append("время на ответ AI истекло (провайдер занят другими запросами)")
            break
        # Время попытки считается после ожидания; урезанной попытке (меньше attempt_limit) таймаут
        # паузы не ставит: модель не обязательно медленная, ей просто не хватило остатка времени.
        timeout = max(min(attempt_limit, deadline - _now()), 0.0)
        shortened = timeout < attempt_limit
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
                _set_cooldown(provider.name, model if err.scope == "model" else _PROVIDER_WIDE, pause_sec, err)
            if err.scope == "provider":
                skipped.add(provider.name)
            elif err.kind in (Failure.TIMEOUT, Failure.NETWORK) and provider.name not in deferred:
                deferred.add(provider.name)
                _defer(queue, provider.name)
            logger.info(
                "AI %s: %s%s — %s",
                provider.label(model),
                _safe(err.detail),
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
        if reasons:
            logger.info("AI ответил: %s (до этого: %s)", provider.label(model), "; ".join(reasons))
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
        return float(SHORT_COOLDOWN_SEC)
    return 0.0


# --- Паузы и очередь -----------------------------------------------------------------------------------


def _set_cooldown(provider: str, model: str, seconds: float, err: ProviderError) -> None:
    until = _now() + seconds
    key = (provider, model)
    current = _cooldowns.get(key)
    if current is None or current[0] < until:
        _cooldowns[key] = (until, err.kind.value)


def _cooldown_reason(provider: str, model: str) -> str | None:
    """Почему модель сейчас на паузе («лимит, ещё 9 мин»); None — можно спрашивать."""
    now = _now()
    for key in ((provider, _PROVIDER_WIDE), (provider, model)):
        entry = _cooldowns.get(key)
        if entry is None:
            continue
        until, reason = entry
        if until <= now:
            del _cooldowns[key]
            continue
        return f"{reason}, ещё {_fmt_duration(until - now)}"
    return None


def _defer(queue: deque[tuple[Provider, str]], name: str) -> None:
    """Оставшиеся модели провайдера — в конец очереди (сначала спросить других)."""
    mine = [item for item in queue if item[0].name == name]
    others = [item for item in queue if item[0].name != name]
    queue.clear()
    queue.extend(others + mine)


def _semaphore(name: str) -> asyncio.Semaphore:
    semaphore = _semaphores.get(name)
    if semaphore is None:
        semaphore = _semaphores[name] = asyncio.Semaphore(_CONCURRENCY)
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
