"""OpenAI-совместимые бесплатные AI-провайдеры (Groq, Cloudflare Workers AI, Mistral, OpenRouter).

Один запрос ``POST {base_url}/chat/completions`` к одной модели через httpx (он уже стоит вместе с
google-genai); перебор моделей и провайдеров, паузы после лимитов — в bot.ai.provider.

* JSON: ``response_format`` = json_schema (схема из запроса), где провайдер это умеет, иначе json_object;
  схема всегда повторяется в системной инструкции. Если провайдер отверг необязательный параметр
  (response_format, reasoning_effort) — один повтор с json_object и без дополнительных параметров.
* Файлы-подтверждения: изображения уходят data-URL только моделям с поддержкой изображений
  (settings.ai_vision_models); PDF и изображения для остальных моделей заменяются пометкой «файл приложен»
  (текст Word/Excel/txt и так идёт текстом).
* Текст запроса (вместе с системной инструкцией) ужимается до лимита провайдера (``max_input_chars``; каждое
  переданное изображение «стоит» ``image_chars`` символов). Сокращаются только части с данными сотрудника
  (``base.DataText``): сначала тексты файлов (самые длинные — до общего «потолка»), потом, если мало, описание
  факта. Вырезается середина данных, а всё до «<<<» и от последней «>>>» до конца части сохраняется — блок
  данных всегда закрыт. Задача и указания бота не сокращаются никогда.
* Время: вся попытка (с повтором без необязательных параметров) укладывается в переданный ``timeout``.
* Ключи передаются только в заголовке Authorization и никогда не пишутся в лог.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from bot.ai.base import (
    TEMPERATURE,
    DataText,
    Failure,
    ProviderError,
    classify_http,
    json_instruction,
    unsupported_file_note,
)

__all__ = ["ProviderSpec", "SPECS", "OpenAICompatProvider", "build_user_content", "fit_texts", "close_http"]


@dataclass(frozen=True)
class ProviderSpec:
    """Постоянные свойства провайдера (ключ, модели и адрес аккаунта — из настроек)."""

    name: str
    title: str
    base_url: str                       # может содержать {account_id} (Cloudflare)
    json_schema: bool = True            # принимает response_format json_schema (иначе json_object)
    max_output_tokens: int = 4096       # потолок ответа (у «думающих» моделей сюда входят рассуждения)
    max_input_chars: int = 60_000       # символов текста в запросе, вместе с системной инструкцией
    max_images: int = 5
    image_chars: int = 0                # на сколько символов меньше текста за каждое изображение (лимит токенов)
    max_image_bytes: int = 4 * 1024 * 1024
    extra: Mapping[str, Any] = field(default_factory=dict)  # необязательные параметры запроса


SPECS: dict[str, ProviderSpec] = {
    # Free: 30 запросов/мин, 1000 в сутки и 8000 токенов в минуту на КАЖДУЮ модель (запрос + max_tokens) —
    # поэтому короткий запрос: ~12 000 символов вместе с инструкцией (≈ 4000 токенов) + 2048 на ответ;
    # изображение — 2048 токенов входа, поэтому одно, и за него на ~6000 символов текста меньше.
    "groq": ProviderSpec(
        name="groq",
        title="Groq",
        base_url="https://api.groq.com/openai/v1",
        max_output_tokens=2048,
        max_input_chars=12_000,
        max_images=1,
        image_chars=6_000,
        extra={"reasoning_effort": "low"},
    ),
    # Workers AI Free: 10 000 «нейронов» в сутки на аккаунт (сброс в 00:00 UTC) — длина запроса = расход.
    "cloudflare": ProviderSpec(
        name="cloudflare",
        title="Cloudflare Workers AI",
        base_url="https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        max_output_tokens=2048,
        max_input_chars=24_000,
    ),
    # Free mode (Mistral AI Studio): около 1 запроса в секунду, большой месячный лимит токенов.
    "mistral": ProviderSpec(
        name="mistral",
        title="Mistral",
        base_url="https://api.mistral.ai/v1",
        max_input_chars=60_000,
    ),
    # Бесплатные модели «:free»: 50 запросов в сутки на аккаунт; json_schema — не у всех моделей.
    "openrouter": ProviderSpec(
        name="openrouter",
        title="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        json_schema=False,
        max_input_chars=40_000,
    ),
}

# 400 из-за необязательного параметра запроса — повторить проще (json_object, без extra).
_OPTIONAL_PARAM_HINTS = ("response_format", "json_schema", "json schema", "reasoning_effort", "reasoning")
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_TRIM_MARK = "\n…[часть текста не передана: лимит бесплатного AI]…"
_DATA_OPEN = "<<<\n"   # начало данных сотрудника в части DataText
_DATA_CLOSE = "\n>>>"  # конец данных (последний в части; такие же цепочки внутри данных обезврежены)

_http: httpx.AsyncClient | None = None
# Транспорт httpx для тестов (httpx.MockTransport); None — настоящая сеть.
_transport: httpx.AsyncBaseTransport | None = None


class OpenAICompatProvider:
    """Провайдер с OpenAI-совместимым API: адрес, ключ, модели и какие из них видят изображения."""

    def __init__(
        self, spec: ProviderSpec, *, api_key: str, models: list[str], vision_models: set[str], account_id: str = ""
    ) -> None:
        self.spec = spec
        self.name = spec.name
        self.title = spec.title
        self.models = models
        self._api_key = api_key
        self._vision_models = vision_models
        self.url = spec.base_url.format(account_id=account_id.strip()).rstrip("/") + "/chat/completions"

    def supports_images(self, model: str) -> bool:
        return model in self._vision_models

    def label(self, model: str) -> str:
        """Имя модели для журнала и БД: «groq:openai/gpt-oss-120b»."""
        return f"{self.name}:{model}"

    async def generate(
        self, *, model: str, system: str, parts: list, schema: dict, max_output_tokens: int, timeout: float
    ) -> str:
        """Текст ответа модели (JSON). Бросает ProviderError. Вся попытка — не дольше timeout секунд."""
        spec = self.spec
        loop = asyncio.get_running_loop()
        ends_at = loop.time() + timeout
        instruction = system + json_instruction(schema)
        user_content = build_user_content(
            parts,
            vision=self.supports_images(model),
            max_chars=spec.max_input_chars - len(instruction),
            max_images=spec.max_images,
            max_image_bytes=spec.max_image_bytes,
            image_chars=spec.image_chars,
        )
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": user_content},
            ],
            "temperature": TEMPERATURE,
            "max_tokens": min(int(max_output_tokens), spec.max_output_tokens),
            "response_format": _response_format(schema) if spec.json_schema else {"type": "json_object"},
            **spec.extra,
        }
        status, headers, text = await self._post(payload, timeout)
        if status == 400 and _optional_param_rejected(text) and (spec.json_schema or spec.extra):
            simple = {key: value for key, value in payload.items() if key not in spec.extra}
            simple["response_format"] = {"type": "json_object"}
            status, headers, text = await self._post(simple, max(ends_at - loop.time(), 0.0))
        return _answer_text(status, headers, text)

    async def _post(self, payload: dict[str, Any], timeout: float) -> tuple[int, Mapping[str, str], str]:
        client = _get_http()
        headers = {"Authorization": f"Bearer {self._api_key.strip()}"}
        try:
            response = await asyncio.wait_for(
                client.post(self.url, json=payload, headers=headers, timeout=timeout), timeout=timeout
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ProviderError(Failure.TIMEOUT, f"нет ответа за {timeout:.0f} с") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise ProviderError(Failure.NETWORK, f"сеть: {type(exc).__name__}") from exc
        return response.status_code, response.headers, response.text


async def close_http() -> None:
    """Закрыть HTTP-клиент (при остановке бота)."""
    global _http
    client, _http = _http, None
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - при остановке ошибки закрытия не важны
            pass


def build_user_content(
    parts: list,
    *,
    vision: bool,
    max_chars: int,
    max_images: int = 5,
    max_image_bytes: int = 4 * 1024 * 1024,
    image_chars: int = 0,
) -> str | list[dict[str, Any]]:
    """Части запроса (str, base.DataText и google.genai.types.Part) -> content сообщения пользователя.

    Без изображений — одна строка (её понимают все провайдеры); с изображениями — список частей
    text / image_url (data-URL). Текст ужимается до max_chars минус image_chars за каждое изображение.
    """
    segments: list[tuple[str, str]] = []  # ("text", текст) | ("image", data-URL)
    images = 0
    for part in parts:
        if isinstance(part, str):
            segments.append(("text", part))
            continue
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            segments.append(("text", text))
            continue
        blob = getattr(part, "inline_data", None)
        data = getattr(blob, "data", None) if blob is not None else None
        if not data:
            continue
        mime = str(getattr(blob, "mime_type", "") or "application/octet-stream").lower()
        if not mime.startswith("image/"):
            segments.append(("text", unsupported_file_note(mime, "эта модель не читает PDF")))
        elif not vision:
            segments.append(("text", unsupported_file_note(mime, "эта модель не видит изображения")))
        elif len(data) > max_image_bytes:
            segments.append(("text", unsupported_file_note(mime, "изображение слишком большое")))
        elif images >= max_images:
            segments.append(("text", unsupported_file_note(mime, "слишком много изображений")))
        else:
            images += 1
            segments.append(("image", f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"))

    texts = fit_texts([value for kind, value in segments if kind == "text"], max_chars - images * image_chars)
    fitted = iter(texts)
    segments = [(kind, next(fitted) if kind == "text" else value) for kind, value in segments]
    if not images:
        return "\n\n".join(value for _, value in segments)

    content: list[dict[str, Any]] = []
    for kind, value in segments:
        if kind == "image":
            content.append({"type": "image_url", "image_url": {"url": value}})
        elif content and content[-1]["type"] == "text":
            content[-1]["text"] += "\n\n" + value
        else:
            content.append({"type": "text", "text": value})
    return content


def fit_texts(texts: list[str], budget: int) -> list[str]:
    """Уложить тексты в budget символов, сокращая только части с данными сотрудника (base.DataText).

    Очередь — DataText.trim_order: сначала тексты файлов, потом (если мало) описание факта; внутри очереди
    самые длинные части обрезаются до общего «потолка», короткие не трогаются. Остальные строки (задача,
    указания бота, пометки) не сокращаются: если без них не уложиться, запрос уходит длиннее — провайдер
    ответит «запрос слишком большой», и цепочка спросит следующую модель.
    """
    result = list(texts)
    for order in sorted({text.trim_order for text in result if isinstance(text, DataText)}):
        overflow = sum(len(text) for text in result) - budget
        if overflow <= 0:
            break
        indexes = [i for i, text in enumerate(result) if isinstance(text, DataText) and text.trim_order == order]
        lengths = [len(result[i]) for i in indexes]
        cap = _common_cap(lengths, sum(lengths) - overflow)
        for i in indexes:
            if len(result[i]) > cap:
                result[i] = _cut_data(result[i], cap)
    return result


def _common_cap(lengths: list[int], budget: int) -> int:
    """Общий «потолок»: части не длиннее него не трогаются, остальные обрезаются до него, всего ≤ budget."""
    remaining = budget
    ordered = sorted(lengths)
    for index, length in enumerate(ordered):
        share = remaining // (len(ordered) - index)
        if length > share:
            return max(share, 0)
        remaining -= length
    return ordered[-1] if ordered else 0


def _cut_data(text: str, limit: int) -> str:
    """Сократить часть DataText примерно до limit символов: середина данных -> пометка.

    Всё до «<<<» включительно (заголовок) и от последней «>>>» до конца (закрывающий маркер и строки бота
    после него) сохраняется всегда — даже если ради этого часть выйдет длиннее limit.
    """
    opened = text.find(_DATA_OPEN)
    start = opened + len(_DATA_OPEN) if opened >= 0 else 0
    end = text.rfind(_DATA_CLOSE)
    if end < start:
        end = len(text)
    head, data, tail = text[:start], text[start:end], text[end:]
    keep = max(limit - len(head) - len(tail) - len(_TRIM_MARK), 0)
    if keep >= len(data):
        return text
    return head + data[:keep] + _TRIM_MARK + tail


def _response_format(schema: dict) -> dict[str, Any]:
    return {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": False}}


def _optional_param_rejected(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in _OPTIONAL_PARAM_HINTS)


def _answer_text(status: int, headers: Mapping[str, str], text: str) -> str:
    """Текст ответа модели из ответа /chat/completions или ProviderError."""
    if status >= 400:
        raise classify_http(status, _error_text(text), retry_after=_retry_after(headers))
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise ProviderError(Failure.BAD_ANSWER, f"HTTP {status}: ответ сервиса не JSON") from exc
    if not isinstance(body, dict):
        raise ProviderError(Failure.BAD_ANSWER, f"HTTP {status}: неожиданный ответ сервиса")
    if body.get("error") and not body.get("choices"):
        # OpenRouter иногда присылает ошибку модели с кодом 200.
        error = body["error"] if isinstance(body["error"], dict) else {}
        code = error.get("code")
        effective = code if isinstance(code, int) and 400 <= code < 600 else 502
        raise classify_http(effective, _error_text(text), label=f"HTTP {status}/{effective}")
    try:
        choice = body["choices"][0]
        content = choice["message"].get("content")
        finish = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ProviderError(Failure.BAD_ANSWER, "в ответе нет choices[0].message") from exc
    if isinstance(content, list):  # часть провайдеров отдаёт content частями
        content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    if not isinstance(content, str):
        content = ""
    content = _THINK_RE.sub("", content).strip()
    if not content:
        raise ProviderError(Failure.BAD_ANSWER, f"пустой ответ (finish_reason={finish or '?'})")
    return content


def _error_text(text: str) -> str:
    """Сообщение и коды ошибки («code=…») из тела ответа (форматы OpenAI, Cloudflare, OpenRouter) — для распознавания."""
    try:
        body = json.loads(text)
    except ValueError:
        return (text or "")[:1000]
    pieces: list[str] = []

    def collect(item: object, key: str = "") -> None:
        if isinstance(item, dict):
            for name in ("message", "code", "type", "status", "param", "metadata", "raw"):
                if name in item:
                    collect(item[name], name)
        elif isinstance(item, list):
            for element in item:
                collect(element, key)
        elif item is not None:
            # Код ошибки — с подписью: «code=4006» (Cloudflare, дневной лимит) не спутать с цифрами в тексте.
            pieces.append(f"code={item}" if key == "code" else str(item))

    if isinstance(body, dict):
        collect(body.get("error"))
        collect(body.get("errors"))
        collect(body.get("message"))
    return " ".join(pieces)[:2000] or (text or "")[:1000]


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = (headers.get("retry-after") or "").strip()
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(transport=_transport, follow_redirects=False)
    return _http
