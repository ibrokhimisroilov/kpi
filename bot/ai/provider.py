"""Вызов бесплатного Google Gemini (google-genai) с перебором моделей.

Модели из settings.gemini_models пробуются по порядку: если у модели исчерпан
бесплатный лимит (429), она недоступна (404), сервер ответил 5xx, истёк таймаут,
ответ пустой или его не удалось разобрать как JSON — берётся следующая. Если не смогла ни
одна — AIUnavailable, и вызывающий код переходит на расчёт по правилам.

* Лимит ответа не меньше ``MIN_OUTPUT_TOKENS``: «думающие» модели (gemini-3.x-flash) тратят
  часть max_output_tokens на рассуждения, и при маленьком лимите ``response.text`` приходит
  пустым (finish_reason=MAX_TOKENS). Лимит — потолок, а не расход: короткий ответ его не тратит.
* Автоматический вызов функций (AFC) SDK выключен: инструментов мы не передаём, а включённый
  по умолчанию AFC пишет в лог «AFC is enabled…» и предупреждение на каждый запрос.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from bot.config import get_settings

__all__ = ["AIUnavailable", "ai_available", "generate_json", "close_client"]

logger = logging.getLogger(__name__)

_TEMPERATURE = 0.2
# Нижняя граница лимита ответа (см. docstring модуля): меньше — «думающая» модель может не успеть ответить.
MIN_OUTPUT_TOKENS = 8192
# Не больше двух одновременных запросов — чтобы не упираться в лимиты бесплатного тарифа.
_semaphore = asyncio.Semaphore(2)
_client: genai.Client | None = None

# Коды, при которых имеет смысл попробовать другую модель.
_RETRY_NEXT_CODES = frozenset({404, 408, 429})
# Сетевые ошибки и таймауты (google-genai ходит в сеть через aiohttp или httpx).
_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    OSError,
    aiohttp.ClientError,
    httpx.HTTPError,
)
# Признаки сообщения «эта модель не поддерживается / не найдена» в ответе 4xx.
_UNSUPPORTED_HINTS = (
    "not supported",
    "unsupported",
    "does not support",
    "not found",
    "not available",
    "no longer available",
    "is not enabled",
    "deprecated",
)
_CODE_FENCE_RE = re.compile(r"^```[\w-]*\s*|\s*```$")


class AIUnavailable(Exception):
    """AI недоступен: выключен, нет ключа, исчерпан бесплатный лимит, ошибка сети или плохой ответ."""


def ai_available() -> bool:
    """Включён ли AI в настройках (провайдер gemini и задан ключ)."""
    return get_settings().ai_enabled


async def generate_json(
    *, system: str, parts: list, schema: dict, max_output_tokens: int = MIN_OUTPUT_TOKENS
) -> tuple[dict, str]:
    """Запросить у Gemini JSON-ответ по схеме. Возвращает (данные, имя_модели).

    parts — список строк и/или google.genai.types.Part. max_output_tokens меньше
    MIN_OUTPUT_TOKENS поднимается до него. Бросает AIUnavailable.
    """
    settings = get_settings()
    if not settings.ai_enabled:
        raise AIUnavailable("AI отключён в настройках")
    models = [model.strip() for model in settings.gemini_models if model.strip()]
    if not models:
        raise AIUnavailable("Не задан список моделей Gemini (GEMINI_MODELS)")

    client = _get_client()
    config = _build_config(system=system, schema=schema, max_output_tokens=max_output_tokens)
    hard_timeout = settings.ai_timeout_sec + 5
    reasons: list[str] = []

    for model in models:
        try:
            async with _semaphore:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(model=model, contents=parts, config=config),
                    timeout=hard_timeout,
                )
        except genai_errors.APIError as exc:
            reason = f"{model}: {exc.code} {exc.status}"
            if not _should_try_next_model(exc):
                logger.warning("Gemini отклонил запрос (%s): %s", reason, _safe(exc.message))
                raise AIUnavailable(f"Gemini отклонил запрос: {exc.code} {exc.status}") from exc
            logger.info("Модель %s недоступна (%s), пробую следующую", model, reason)
            reasons.append(reason)
            continue
        except _NETWORK_ERRORS as exc:
            logger.info("Модель %s: сеть или таймаут (%s), пробую следующую", model, _describe(exc))
            reasons.append(f"{model}: {type(exc).__name__}")
            continue
        except Exception as exc:  # noqa: BLE001 - непредвиденная ошибка SDK не должна ломать бота
            logger.warning("Модель %s: ошибка %s, пробую следующую", model, _describe(exc))
            reasons.append(f"{model}: {type(exc).__name__}")
            continue

        text = _response_text(response)
        if not text or not text.strip():
            finish = _finish_reason(response)
            logger.info("Модель %s вернула пустой ответ (finish_reason=%s), пробую следующую", model, finish)
            reasons.append(f"{model}: пустой ответ ({finish})")
            continue
        data = _parse_json(text)
        if data is None:
            logger.info(
                "Модель %s вернула не JSON (finish_reason=%s), пробую следующую", model, _finish_reason(response)
            )
            reasons.append(f"{model}: не JSON")
            continue
        return data, model

    logger.warning("Ни одна модель Gemini не ответила: %s", "; ".join(reasons))
    raise AIUnavailable("Ни одна модель Gemini не ответила: " + "; ".join(reasons))


async def close_client() -> None:
    """Закрыть HTTP-сессии клиента Gemini (вызывать при остановке бота)."""
    global _client
    client, _client = _client, None
    if client is None:
        return
    try:
        await client.aio.aclose()
    except Exception as exc:  # noqa: BLE001 - при остановке ошибки закрытия не важны
        logger.debug("Не удалось закрыть клиент Gemini: %s", _describe(exc))


def _build_config(*, system: str, schema: dict, max_output_tokens: int) -> types.GenerateContentConfig:
    """Настройки запроса: JSON по схеме, низкая температура, лимит ответа не меньше MIN_OUTPUT_TOKENS, без AFC."""
    return types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema,
        temperature=_TEMPERATURE,
        max_output_tokens=max(int(max_output_tokens), MIN_OUTPUT_TOKENS),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def _get_client() -> genai.Client:
    """Один клиент на процесс; создаётся при первом обращении."""
    global _client
    if _client is None:
        settings = get_settings()
        try:
            _client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(timeout=settings.ai_timeout_sec * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - ошибка конфигурации SDK -> работаем на правилах
            raise AIUnavailable(f"Не удалось создать клиент Gemini: {type(exc).__name__}") from exc
    return _client


def _should_try_next_model(exc: genai_errors.APIError) -> bool:
    """Есть ли смысл пробовать следующую модель после ошибки API."""
    code = exc.code if isinstance(exc.code, int) else 0
    if code in _RETRY_NEXT_CODES or code >= 500 or code == 0:
        return True
    # Прочие 4xx (неверный ключ, некорректный запрос) не зависят от модели —
    # кроме случая, когда сам ответ говорит, что модель не поддерживается.
    message = str(exc.message or "").lower()
    return "model" in message and any(hint in message for hint in _UNSUPPORTED_HINTS)


def _response_text(response: Any) -> str | None:
    """Текст ответа модели; None, если ответ пустой (блокировка, нет кандидатов)."""
    try:
        return response.text
    except Exception:  # noqa: BLE001 - у «пустого» ответа SDK может бросить при чтении .text
        return None


def _finish_reason(response: Any) -> str:
    """Почему модель закончила ответ (STOP, MAX_TOKENS, SAFETY…) — для лога; «?», если неизвестно."""
    try:
        reason = response.candidates[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return "?"
    return str(getattr(reason, "value", reason) or "?")


def _parse_json(text: str | None) -> dict | None:
    """Разобрать JSON-объект из ответа; снимает обёртку ```json … ```. None — если не вышло."""
    if not text or not text.strip():
        return None
    cleaned = _CODE_FENCE_RE.sub("", text.strip()).strip()
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if 0 < start < end:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _describe(exc: BaseException) -> str:
    """Короткое описание исключения для лога (без ключа API)."""
    text = str(exc)
    return type(exc).__name__ + (f": {_safe(text)}" if text else "")


def _safe(text: object) -> str:
    """Вырезать ключ API из текста для лога и обрезать слишком длинные сообщения."""
    result = str(text or "")
    key = get_settings().gemini_api_key
    if key:
        result = result.replace(key, "***")
    return result[:300]
