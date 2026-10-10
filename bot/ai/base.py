"""Общее для AI-провайдеров (bot.ai.gemini, bot.ai.openai_compat): классы сбоев и их распознавание.

Провайдер при неудаче бросает ``ProviderError`` с классом сбоя (``Failure``) и областью действия
(``scope``): ``"model"`` — не смогла эта модель, ``"provider"`` — не сможет ни одна модель этого
провайдера (неверный ключ, дневной лимит на весь аккаунт, API закрыт для всего проекта). По классу сбоя
цепочка в bot.ai.provider решает, что делать дальше и на сколько «поставить на паузу» модель или провайдера.

``DataText`` — часть запроса с данными сотрудника, которую запасной провайдер может сократить под свой лимит.
"""

from __future__ import annotations

import enum
import json
import re
from typing import Literal

__all__ = [
    "DataText",
    "TRIM_FILES",
    "TRIM_FACT",
    "Failure",
    "ProviderError",
    "classify_http",
    "parse_retry_after",
    "json_instruction",
    "unsupported_file_note",
    "MIN_OUTPUT_TOKENS",
    "TEMPERATURE",
]

# Нижняя граница лимита ответа для Gemini: «думающие» модели (gemini-3.x-flash) тратят часть
# max_output_tokens на рассуждения, и при маленьком лимите ответ приходит пустым (finish_reason=MAX_TOKENS).
MIN_OUTPUT_TOKENS = 8192
TEMPERATURE = 0.2

Scope = Literal["model", "provider"]

# Очередь сокращения частей DataText: сначала тексты файлов, описание факта — только если этого мало.
TRIM_FILES = 1
TRIM_FACT = 2


class DataText(str):
    """Часть запроса с данными сотрудника между строками «<<<» и «>>>» (bot.ai.evaluate, bot.ai.evidence).

    Запасной провайдер с маленьким лимитом (bot.ai.openai_compat.fit_texts) сокращает только такие части:
    вырезает середину данных, а всё до «<<<» включительно и всё от последней «>>>» до конца оставляет —
    блок данных остаётся закрытым. Обычные строки (задача, указания бота, пометки о файлах) не сокращаются.
    ``trim_order`` — очередь сокращения (TRIM_FILES раньше TRIM_FACT). Для Gemini это обычная строка.
    """

    trim_order: int

    def __new__(cls, text: str, trim_order: int = TRIM_FILES) -> DataText:
        obj = super().__new__(cls, text)
        obj.trim_order = trim_order
        return obj


class Failure(enum.StrEnum):
    """Класс сбоя запроса к AI."""

    QUOTA = "лимит"                 # 429: исчерпан бесплатный лимит (минутный или дневной)
    BILLING = "нужна оплата"        # бесплатный тариф недоступен, нужен биллинг, доступ запрещён
    AUTH = "ключ не принят"         # неверный, отозванный или не тот ключ
    NOT_FOUND = "модели нет"        # 404 / модель больше не поддерживается
    OVERLOADED = "сервис перегружен"  # 5xx, 408
    TIMEOUT = "таймаут"
    NETWORK = "сеть"
    BAD_REQUEST = "запрос отклонён"  # прочие 4xx: запрос не подходит этому провайдеру
    BAD_ANSWER = "плохой ответ"      # пустой ответ, не JSON, не по схеме
    UNKNOWN = "ошибка"               # непредвиденная ошибка SDK


class ProviderError(Exception):
    """Запрос к модели не удался. detail — короткое пояснение для лога (ключи в него не попадают)."""

    def __init__(
        self,
        kind: Failure,
        detail: str = "",
        *,
        scope: Scope = "model",
        retry_after: float | None = None,
        daily: bool = False,
    ) -> None:
        super().__init__(detail or kind.value)
        self.kind = kind
        self.detail = detail or kind.value
        self.scope: Scope = scope
        self.retry_after = retry_after  # через сколько секунд провайдер советует повторить (если сказал)
        self.daily = daily              # исчерпан дневной лимит


# --- Распознавание ответов HTTP-API (Gemini, OpenAI-совместимые) ------------------------------------

# Ключ не принят — от модели не зависит, другие модели того же провайдера пробовать бессмысленно.
_AUTH_HINTS = (
    "api key not valid",
    "api_key_invalid",
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "api key expired",
    "api key not found",
    "invalid authentication",
    "authentication error",
    "authentication failed",
    "unauthorized",
    "unauthenticated",
    "unregistered callers",
    "invalid token",
    "missing authorization",
    "no auth credentials",
)
# Бесплатный тариф недоступен / нужен биллинг (для ответов 400/403, НЕ 429: в обычном сообщении Gemini о
# лимите тоже есть «check your plan and billing details»).
_BILLING_HINTS = (
    "billing",
    "failed_precondition",
    "free tier",
    "free_tier",
    "location is not supported",
    "not available in your country",
    "denied access",
    "permission_denied",
    "payment",
    "prepay",
    "credit",
    "upgrade",
    "paid plan",
    "paid tier",
)
# Доступ закрыт всему проекту/аккаунту, а не одной модели.
_ACCOUNT_BLOCK_HINTS = ("denied access", "has been suspended", "account is suspended")
# 400/403, которые относятся ко всему проекту Google / ключу, а не к одной модели: регион не поддерживается,
# API не включён в проекте, ключу запрещён этот API.
_PROJECT_BLOCK_HINTS = (
    "location is not supported",
    "not available in your country",
    "has not been used in project",
    "service_disabled",
    "api_key_service_blocked",
)
# То же, если в сообщении не названа модель (иначе — отказ только этой модели, например «Model requires a paid tier»).
_PROJECT_BLOCK_UNLESS_MODEL_HINTS = ("failed_precondition", "permission_denied", "billing account")
# Запрос больше, чем модель (или её бесплатный лимит токенов в минуту) может принять: ждать бесполезно,
# а другая модель (с бо́льшим лимитом или без изображений) может справиться.
_TOO_LARGE_HINTS = (
    "request too large",
    "reduce your message size",
    "request entity too large",
    "payload too large",
    "context_length_exceeded",
    "context length",
    "exceeds the maximum number of tokens",
)
# 429, который означает «бесплатно больше нельзя», а не «подождите»: лимит 0, нет кредитов.
_PAID_ONLY_429_RE = re.compile(r"limit:\s*0(?![\d.])|insufficient_quota|credit")
# Дневной лимит: ждать минуту бессмысленно.
_DAILY_HINTS = (
    "per day",
    "perday",
    "per_day",
    "daily",
    "(rpd)",
    "(tpd)",
    "allocation",
)
# Лимит на весь аккаунт (а не на модель): Cloudflare — нейроны в сутки, OpenRouter — бесплатные запросы в сутки.
_ACCOUNT_QUOTA_HINTS = ("daily free allocation", "neurons", "free-models-per-day", "free models per day")
# Cloudflare: код ошибки 4006 (дневной бесплатный лимит) — только как поле code ответа («code=4006» из
# openai_compat._error_text или «"code": 4006» в сыром JSON), а не любые цифры 4006 в тексте
# («retry in 21.400612s», «Used 4006» — это минутные лимиты).
_ACCOUNT_QUOTA_CODE_RE = re.compile(r'\bcode"?\s*[=:]\s*"?4006(?![\d.])')
# 4xx с такими словами — проблема в самой модели: её нет или она не умеет то, что нужно.
_MODEL_HINTS = (
    "not supported",
    "unsupported",
    "does not support",
    "not found",
    "not available",
    "no longer available",
    "is not enabled",
    "deprecated",
    "decommissioned",
    "does not exist",
    "no endpoints found",
)
# Модель ответила, но не JSON по схеме (провайдер проверил ответ сам и вернул ошибку).
_BAD_ANSWER_HINTS = ("json_validate_failed", "failed to generate json", "json mode couldn't be met", "failed_generation")

_RETRY_RE = re.compile(r"(?:retry in|try again in|retrydelay[\"':\s]+)\s*([0-9hms.\s]+)", re.IGNORECASE)
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def classify_http(
    status: int, message: str, *, retry_after: float | None = None, label: str | None = None
) -> ProviderError:
    """Ошибка HTTP-API -> ProviderError с классом сбоя.

    message — текст ошибки для распознавания (статус, сообщение, детали; коды ошибки — как «code=…»);
    в лог он не идёт. label — короткая подпись для лога («429 RESOURCE_EXHAUSTED»), по умолчанию «HTTP 429».
    """
    text = (message or "").lower()
    detail = label or (f"HTTP {status}" if status else "ошибка API")
    if retry_after is None:
        retry_after = parse_retry_after(text)

    if status == 402:
        return ProviderError(Failure.BILLING, detail, scope="provider")
    if status == 401 or (status in (400, 403) and any(hint in text for hint in _AUTH_HINTS)):
        return ProviderError(Failure.AUTH, detail, scope="provider")
    if status == 413 or (status in (400, 429) and any(hint in text for hint in _TOO_LARGE_HINTS)):
        # Без паузы и только для этой модели: следующая (без изображений, с бо́льшим лимитом) может принять запрос.
        return ProviderError(Failure.BAD_REQUEST, f"{detail}: запрос слишком большой для модели")
    if 400 <= status < 500 and (
        any(hint in text for hint in _ACCOUNT_QUOTA_HINTS) or _ACCOUNT_QUOTA_CODE_RE.search(text)
    ):
        # Дневной лимит на весь аккаунт (Cloudflare 4006, OpenRouter free-models-per-day) — при любом коде 4xx.
        return ProviderError(
            Failure.QUOTA, f"{detail} (дневной лимит аккаунта)", scope="provider", retry_after=retry_after, daily=True
        )
    if status == 429:
        if _PAID_ONLY_429_RE.search(text):
            return ProviderError(Failure.BILLING, f"{detail}: бесплатный лимит 0")
        daily = any(hint in text for hint in _DAILY_HINTS)
        return ProviderError(
            Failure.QUOTA, detail + (" (дневной лимит)" if daily else ""), retry_after=retry_after, daily=daily
        )
    if status in (400, 403) and any(hint in text for hint in _ACCOUNT_BLOCK_HINTS):
        return ProviderError(Failure.BILLING, f"{detail}: доступ закрыт", scope="provider")
    if status in (400, 403) and _project_wide(text):
        return ProviderError(Failure.BILLING, f"{detail}: недоступно всему проекту", scope="provider")
    if any(hint in text for hint in _BAD_ANSWER_HINTS):
        return ProviderError(Failure.BAD_ANSWER, f"{detail}: ответ не по схеме JSON")
    if status == 404 or (400 <= status < 500 and "model" in text and any(hint in text for hint in _MODEL_HINTS)):
        return ProviderError(Failure.NOT_FOUND, detail)
    if status in (400, 403) and any(hint in text for hint in _BILLING_HINTS):
        return ProviderError(Failure.BILLING, detail)
    if status == 403:
        return ProviderError(Failure.BILLING, f"{detail}: доступ запрещён")
    if status == 408 or status >= 500 or status == 0:
        return ProviderError(Failure.OVERLOADED, detail, retry_after=retry_after)
    return ProviderError(Failure.BAD_REQUEST, detail, scope="provider")


def _project_wide(text: str) -> bool:
    """400/403 относится ко всему проекту / ключу (регион, API выключен, нет биллинга), а не к одной модели."""
    if any(hint in text for hint in _PROJECT_BLOCK_HINTS):
        return True
    return "model" not in text and any(hint in text for hint in _PROJECT_BLOCK_UNLESS_MODEL_HINTS)


def parse_retry_after(text: str) -> float | None:
    """«Please retry in 37.5s», «try again in 7m12.5s», «retryDelay": "37s"» -> секунды; None — не сказано."""
    match = _RETRY_RE.search(text or "")
    if match is None:
        return None
    total = sum(float(value) * _DURATION_UNITS[unit] for value, unit in _DURATION_RE.findall(match.group(1)))
    return total or None


# --- Подсказки модели ------------------------------------------------------------------------------


def json_instruction(schema: dict) -> str:
    """Указание отвечать JSON по схеме — для моделей без режима «JSON по схеме» (Gemma, OpenAI-совместимые)."""
    return (
        "\n\nФОРМАТ ОТВЕТА: только один JSON-объект — без пояснений до и после и без обёртки ```. "
        "Объект строго по этой JSON-схеме (поля, типы, допустимые значения):\n"
        + json.dumps(schema, ensure_ascii=False)
    )


def unsupported_file_note(mime_type: str, reason: str) -> str:
    """Пометка вместо файла, который модели передать нельзя (AI должен знать, что файл был)."""
    if mime_type.startswith("image/"):
        kind = "Изображение приложено"
    elif mime_type == "application/pdf":
        kind = "PDF-файл приложен"
    else:
        kind = "Файл приложен"
    return (
        f"[{kind}, но его содержимое этой модели AI не передано ({reason}). "
        "Оценивай по описанию сотрудника и отметь в обосновании, что файл нужно посмотреть начальнику.]"
    )
