"""Провайдер Google Gemini (google-genai SDK) — бесплатный тариф Google AI Studio.

Один запрос к одной модели; перебор моделей и провайдеров, паузы после лимитов — в bot.ai.provider.

* Ответ — JSON по схеме (response_mime_type + response_json_schema). Модели Gemma (``gemma-…``) режим
  JSON по схеме не поддерживают — им инструкция и схема передаются первой частью запроса (не через
  system_instruction), а PDF (его Gemma не читает) заменяется пометкой «файл приложен»; изображения Gemma 4 видит.
* Лимит ответа не меньше ``MIN_OUTPUT_TOKENS``: «думающие» модели (gemini-3.x-flash) тратят часть
  max_output_tokens на рассуждения, и при маленьком лимите ``response.text`` приходит пустым
  (finish_reason=MAX_TOKENS). Лимит — потолок, а не расход: короткий ответ его не тратит.
* Автоматический вызов функций (AFC) SDK выключен: инструментов мы не передаём, а включённый
  по умолчанию AFC пишет в лог «AFC is enabled…» и предупреждение на каждый запрос.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from bot.ai.base import (
    MIN_OUTPUT_TOKENS,
    TEMPERATURE,
    Failure,
    ProviderError,
    classify_http,
    json_instruction,
    unsupported_file_note,
)
from bot.config import get_settings

__all__ = ["GeminiProvider", "close_client"]

logger = logging.getLogger(__name__)

_client: genai.Client | None = None

# Сетевые ошибки (google-genai ходит в сеть через aiohttp или httpx).
_NETWORK_ERRORS: tuple[type[BaseException], ...] = (OSError, aiohttp.ClientError, httpx.HTTPError)


class GeminiProvider:
    """Google Gemini: все модели принимают изображения; PDF — все, кроме Gemma."""

    name = "gemini"
    title = "Google Gemini"

    def __init__(self, api_key: str, models: list[str]) -> None:
        self.api_key = api_key
        self.models = models

    def supports_images(self, model: str) -> bool:
        return True

    def label(self, model: str) -> str:
        """Имя модели для журнала и БД (без префикса — как в прежних версиях бота)."""
        return model

    async def generate(
        self, *, model: str, system: str, parts: list, schema: dict, max_output_tokens: int, timeout: float
    ) -> str:
        """Текст ответа модели (JSON). Бросает ProviderError."""
        if _is_gemma(model):
            # Инструкция и схема — первой частью запроса (system_instruction у Gemma в API бывает выключен).
            config = _build_config(system=None, schema=None, max_output_tokens=max_output_tokens)
            contents = [system + json_instruction(schema), *_without_pdf(parts)]
        else:
            config = _build_config(system=system, schema=schema, max_output_tokens=max_output_tokens)
            contents = parts
        client = _get_client(self.api_key)
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(model=model, contents=contents, config=config), timeout=timeout
            )
        except genai_errors.APIError as exc:
            code = exc.code if isinstance(exc.code, int) else 0
            message = f"{exc.status or ''} {exc.message or ''} {_details(exc)}"
            raise classify_http(code, message, label=f"{exc.code} {exc.status}") from exc
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ProviderError(Failure.TIMEOUT, f"нет ответа за {timeout:.0f} с") from exc
        except _NETWORK_ERRORS as exc:
            raise ProviderError(Failure.NETWORK, f"сеть: {type(exc).__name__}") from exc
        except Exception as exc:  # noqa: BLE001 - непредвиденная ошибка SDK не должна ломать бота
            raise ProviderError(Failure.UNKNOWN, f"ошибка SDK: {type(exc).__name__}") from exc

        text = _response_text(response)
        if not text or not text.strip():
            raise ProviderError(Failure.BAD_ANSWER, f"пустой ответ (finish_reason={_finish_reason(response)})")
        return text


async def close_client() -> None:
    """Закрыть HTTP-сессии клиента Gemini (при остановке бота)."""
    global _client
    client, _client = _client, None
    if client is None:
        return
    try:
        await client.aio.aclose()
    except Exception as exc:  # noqa: BLE001 - при остановке ошибки закрытия не важны
        logger.debug("Не удалось закрыть клиент Gemini: %s", type(exc).__name__)


def _is_gemma(model: str) -> bool:
    return model.strip().lower().startswith("gemma-")


def _without_pdf(parts: list) -> list:
    """Части запроса для Gemma: PDF -> пометка (Gemma читает текст и изображения, но не PDF)."""
    result: list = []
    for part in parts:
        blob = getattr(part, "inline_data", None)
        mime = str(getattr(blob, "mime_type", "") or "").lower() if blob is not None else ""
        if blob is not None and not mime.startswith("image/"):
            result.append(unsupported_file_note(mime, "эта модель не читает PDF"))
        else:
            result.append(part)
    return result


def _build_config(*, system: str | None, schema: dict | None, max_output_tokens: int) -> types.GenerateContentConfig:
    """Настройки запроса: JSON по схеме, низкая температура, лимит ответа не меньше MIN_OUTPUT_TOKENS, без AFC."""
    json_mode: dict[str, Any] = (
        {"response_mime_type": "application/json", "response_json_schema": schema} if schema is not None else {}
    )
    return types.GenerateContentConfig(
        system_instruction=system,
        temperature=TEMPERATURE,
        max_output_tokens=max(int(max_output_tokens), MIN_OUTPUT_TOKENS),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        **json_mode,
    )


def _get_client(api_key: str) -> genai.Client:
    """Один клиент на процесс; создаётся при первом обращении."""
    global _client
    if _client is None:
        try:
            _client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=get_settings().ai_timeout_sec * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - ошибка конфигурации SDK -> следующий провайдер
            raise ProviderError(
                Failure.AUTH, f"не удалось создать клиент Gemini: {type(exc).__name__}", scope="provider"
            ) from exc
    return _client


def _details(exc: genai_errors.APIError) -> str:
    """Подробности ошибки (квоты, RetryInfo) — для распознавания дневного лимита и «повторите через N с»."""
    try:
        return json.dumps(exc.details, ensure_ascii=False, default=str)[:4000]
    except (TypeError, ValueError):
        return ""


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
