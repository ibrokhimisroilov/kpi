"""bot.ai.provider: настройки запроса к Gemini и «пустые» ответы думающих моделей — без сети.

* AFC (автоматический вызов функций SDK) выключен: иначе google-genai на КАЖДЫЙ запрос пишет в лог
  «AFC is enabled with max remote calls: 10» и предупреждение про AFC.
* Лимит ответа — не меньше 8192 токенов: gemini-3.x-flash тратит лимит на рассуждения, и при
  маленьком max_output_tokens ``response.text`` приходит пустым (finish_reason=MAX_TOKENS).
* Пустой/обрезанный ответ — повод попробовать следующую модель, а не «ошибка AI».

Сеть не используется: либо фейковый ``client.aio.models``, либо настоящий ``genai.Client`` с подменённым
транспортом (``_api_client.async_request``) — так проверяется реальный путь SDK, включая его логи.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from google import genai
from google.genai import types

from bot.ai import provider
from bot.ai.evaluate import evaluate_submission
from bot.ai.formulate import suggest_expected_result
from bot.ai.provider import MIN_OUTPUT_TOKENS, AIUnavailable, generate_json
from bot.db.models import Submission, Task

API_KEY = "offline-test-key-42"
SCHEMA = {"type": "object", "properties": {"score": {"type": "number"}}}
DEADLINE = datetime(2026, 10, 5, 13, 0)


@pytest.fixture
def ai_on(set_env) -> None:
    set_env(AI_PROVIDER="gemini", GEMINI_API_KEY=API_KEY, GEMINI_MODELS="gemini-3.8-flash,gemini-3.6-flash")


def gemini_response(text: str | None, finish: str = "STOP") -> types.GenerateContentResponse:
    """Ответ в том виде, в каком его возвращает SDK (у «пустого» ответа нет частей — .text = None)."""
    parts = [types.Part(text=text)] if text is not None else []
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=types.FinishReason(finish))
        ]
    )


class FakeModels:
    """client.aio.models: ответы по моделям; запоминает вызовы и переданные настройки."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.calls: list[str] = []
        self.configs: list[types.GenerateContentConfig] = []

    async def generate_content(self, *, model: str, contents: list, config: Any) -> Any:
        self.calls.append(model)
        self.configs.append(config)
        outcome = self.script[model]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def fake_models(ai_on, monkeypatch) -> Callable[[dict[str, Any]], FakeModels]:
    def _install(script: dict[str, Any]) -> FakeModels:
        models = FakeModels(script)
        monkeypatch.setattr(provider, "_client", SimpleNamespace(aio=SimpleNamespace(models=models)))
        return models

    return _install


def make_pair() -> tuple[Task, Submission]:
    task = Task(
        id=1, title="Анализ договоров", expected_result="Проверить 100 договоров", plan_value=100,
        plan_unit="договоров", deadline=DEADLINE, weight=20,
    )
    sub = Submission(
        id=1, attempt=1, fact_text="Проверено 110 договоров", fact_value=110, created_at=DEADLINE,
        deadline_at_submit=DEADLINE, is_late=False, late_days=0.0,
    )
    return task, sub


# --- Настройки запроса -----------------------------------------------------------------------------


@pytest.mark.parametrize("requested", [256, 2048, 4096, None])
async def test_request_config_disables_afc_and_keeps_big_budget(fake_models, requested) -> None:
    """Каждый запрос — без AFC и с лимитом ответа не меньше 8192 токенов (даже если попросили меньше)."""
    models = fake_models({"gemini-3.8-flash": gemini_response('{"score": 100}')})
    kwargs = {} if requested is None else {"max_output_tokens": requested}
    data, model = await generate_json(system="s", parts=["p"], schema=SCHEMA, **kwargs)
    assert (data, model) == ({"score": 100}, "gemini-3.8-flash")

    config = models.configs[0]
    assert config.automatic_function_calling is not None
    assert config.automatic_function_calling.disable is True
    assert config.max_output_tokens == MIN_OUTPUT_TOKENS == 8192
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == SCHEMA


async def test_bigger_budget_is_not_cut(fake_models) -> None:
    models = fake_models({"gemini-3.8-flash": gemini_response('{"score": 1}')})
    await generate_json(system="s", parts=["p"], schema=SCHEMA, max_output_tokens=16384)
    assert models.configs[0].max_output_tokens == 16384


async def test_evaluation_and_formulation_requests_use_big_budget(fake_models) -> None:
    """И оценка сдачи, и подсказка формулировки идут с лимитом ≥ 8192 и без AFC."""
    models = fake_models({
        "gemini-3.8-flash": gemini_response(
            '{"score": 110, "rationale": "План 100, факт 110.", "completeness": "exceeded",'
            ' "expected_result": "Проверить 100 договоров", "plan_value": 100, "plan_unit": "договоров",'
            ' "note": null}'
        )
    })
    task, sub = make_pair()
    assert (await evaluate_submission(task, sub)).source == "ai"
    assert (await suggest_expected_result("Анализ", "посмотреть 100 договоров")).source == "ai"
    assert len(models.configs) == 2
    for config in models.configs:
        assert config.max_output_tokens >= 8192
        assert config.automatic_function_calling.disable is True


# --- Пустые и обрезанные ответы --------------------------------------------------------------------


@pytest.mark.parametrize(
    "empty",
    [
        gemini_response(None, finish="MAX_TOKENS"),  # рассуждения съели весь лимит
        gemini_response("", finish="MAX_TOKENS"),
        gemini_response("   \n", finish="STOP"),
        gemini_response('{"score": 11', finish="MAX_TOKENS"),  # JSON обрезан на середине
        types.GenerateContentResponse(candidates=[]),           # нет кандидатов (блокировка)
    ],
)
async def test_empty_answer_tries_next_model(fake_models, caplog, empty) -> None:
    """gemini-3.8-flash вернула пустой текст — бот спрашивает gemini-3.6-flash и берёт её ответ."""
    models = fake_models({"gemini-3.8-flash": empty, "gemini-3.6-flash": gemini_response('{"score": 95}')})
    caplog.set_level(logging.INFO, logger="bot.ai.provider")
    data, model = await generate_json(system="s", parts=["p"], schema=SCHEMA)
    assert (data, model) == ({"score": 95}, "gemini-3.6-flash")
    assert models.calls == ["gemini-3.8-flash", "gemini-3.6-flash"]
    assert "gemini-3.8-flash" in caplog.text


async def test_empty_answer_reason_is_logged(fake_models, caplog) -> None:
    """В логе видно, почему ответ пустой (finish_reason=MAX_TOKENS) — чтобы было что чинить."""
    fake_models({
        "gemini-3.8-flash": gemini_response(None, finish="MAX_TOKENS"),
        "gemini-3.6-flash": gemini_response('{"score": 1}'),
    })
    caplog.set_level(logging.INFO, logger="bot.ai.provider")
    await generate_json(system="s", parts=["p"], schema=SCHEMA)
    assert "MAX_TOKENS" in caplog.text


async def test_all_models_empty_falls_back_to_rules(fake_models) -> None:
    """Все модели ответили пусто: generate_json — AIUnavailable, оценка сдачи — по правилам (110 %)."""
    empty = gemini_response(None, finish="MAX_TOKENS")
    fake_models({"gemini-3.8-flash": empty, "gemini-3.6-flash": empty})
    with pytest.raises(AIUnavailable, match="пустой ответ"):
        await generate_json(system="s", parts=["p"], schema=SCHEMA)
    task, sub = make_pair()
    result = await evaluate_submission(task, sub)
    assert (result.source, result.score) == ("rules", 110)
    assert result.rationale.startswith("Расчёт по правилам (AI недоступен)")


# --- Настоящий SDK google-genai с подменённым транспортом -------------------------------------------


class FakeTransport:
    """Вместо HTTP: запоминает тело запроса и отвечает заданным JSON (как REST API Gemini)."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, http_method: str, path: str, request_dict: dict, http_options: Any = None) -> Any:
        self.requests.append((path, request_dict))
        body = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return SimpleNamespace(body=json.dumps(body), headers={})


def rest_answer(text: str | None, finish: str = "STOP") -> dict[str, Any]:
    parts = [{"text": text}] if text is not None else []
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}]}


@pytest.fixture
def real_sdk(ai_on, monkeypatch) -> Callable[..., FakeTransport]:
    """Настоящий genai.Client (как в боте), но запросы уходят в FakeTransport, а не в сеть."""

    def _install(*answers: dict[str, Any]) -> FakeTransport:
        transport = FakeTransport(*answers)
        client = genai.Client(api_key=API_KEY, http_options=types.HttpOptions(timeout=60_000))
        monkeypatch.setattr(client._api_client, "async_request", transport)
        monkeypatch.setattr(provider, "_client", client)
        return transport

    return _install


def afc_records(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "AFC" in r.getMessage() or "function calling" in r.getMessage()]


async def test_sdk_logs_nothing_about_afc(real_sdk, caplog) -> None:
    """Через настоящий SDK: ни «AFC is enabled…», ни предупреждения про AFC; в запросе — лимит 8192,
    служебная настройка AFC в API не уходит, ключ API в логи не попадает."""
    transport = real_sdk(rest_answer('{"score": 103}'))
    caplog.set_level(logging.DEBUG)
    for _ in range(2):  # предупреждение SDK пишет один раз на процесс, info — на каждый запрос
        data, model = await generate_json(system="Ты — помощник руководителя", parts=["План 100"], schema=SCHEMA)
        assert (data, model) == ({"score": 103}, "gemini-3.8-flash")

    assert afc_records(caplog) == []
    path, body = transport.requests[0]
    assert "gemini-3.8-flash" in json.dumps(body) + path
    assert body["generationConfig"]["maxOutputTokens"] == 8192
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "automaticFunctionCalling" not in json.dumps(body)
    assert API_KEY not in caplog.text


async def test_sdk_control_without_disable_logs_afc(real_sdk, caplog) -> None:
    """Контроль: тот же SDK без disable=True пишет «AFC is enabled» — значит, проверка выше не пустая."""
    real_sdk(rest_answer('{"score": 1}'))
    caplog.set_level(logging.DEBUG)
    config = types.GenerateContentConfig(response_mime_type="application/json", max_output_tokens=8192)
    await provider._client.aio.models.generate_content(model="gemini-3.8-flash", contents=["p"], config=config)
    assert any("AFC is enabled" in message for message in afc_records(caplog))


async def test_sdk_empty_text_with_max_tokens_tries_next_model(real_sdk, caplog) -> None:
    """Через настоящий SDK: первая модель «задумалась» и упёрлась в лимит (MAX_TOKENS, без текста) —
    ответ берётся у следующей модели."""
    transport = real_sdk(rest_answer(None, finish="MAX_TOKENS"), rest_answer('{"score": 97}'))
    caplog.set_level(logging.INFO, logger="bot.ai.provider")
    data, model = await generate_json(system="s", parts=["p"], schema=SCHEMA)
    assert (data, model) == ({"score": 97}, "gemini-3.6-flash")
    assert len(transport.requests) == 2
    assert "MAX_TOKENS" in caplog.text
