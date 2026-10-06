"""bot.ai.provider: цепочка бесплатных AI-провайдеров (Gemini → Groq → … → правила) — без сети.

Gemini — фейковый ``client.aio.models`` (bot.ai.gemini._client); OpenAI-совместимые провайдеры (Groq,
Cloudflare, OpenRouter) — настоящий httpx-клиент с ``httpx.MockTransport`` вместо сети. Часы пауз
(bot.ai.provider._now) подменены, чтобы проверять «через 10 минут модель снова спрашивается».
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from bot.ai import gemini as gemini_module
from bot.ai import openai_compat, provider
from bot.ai.base import TRIM_FACT, TRIM_FILES, DataText, Failure, ProviderError, classify_http, parse_retry_after
from bot.ai.evaluate import _build_parts, evaluate_submission
from bot.ai.evidence import EvidenceItem, evidence_to_parts
from bot.ai.formulate import suggest_expected_result
from bot.ai.provider import AIUnavailable, active_chain, ai_available, generate_json
from bot.config import get_settings
from bot.db.models import Submission, Task

GEMINI_KEY = "gemini-secret-key-0001"
GROQ_KEY = "gsk_groq-secret-key-0002"
OPENROUTER_KEY = "sk-or-openrouter-secret-0003"
CF_TOKEN = "cf-token-secret-0004"
CF_ACCOUNT = "cfaccount0005"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "rationale": {"type": "string"},
        "note": {"type": ["string", "null"]},
    },
    "required": ["score", "rationale", "note"],
}
GOOD = {"score": 95, "rationale": "План выполнен.", "note": None}
DEADLINE = datetime(2026, 10, 5, 13, 0)
PNG = b"\x89PNG\r\n\x1a\n fake png"
PDF = b"%PDF-1.4 fake"


# --- Фикстуры --------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(provider, "_now", fake)
    return fake


class FakeGemini:
    """client.aio.models: по каждой модели — очередь ответов (текст JSON или исключение)."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = script
        self.calls: list[str] = []
        self.contents: list[list] = []
        self.configs: list[Any] = []

    async def generate_content(self, *, model: str, contents: list, config: Any) -> Any:
        self.calls.append(model)
        self.contents.append(contents)
        self.configs.append(config)
        queue = self.script[model]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(text=outcome)


@pytest.fixture
def fake_gemini(monkeypatch) -> Callable[[dict[str, list[Any]]], FakeGemini]:
    def _install(script: dict[str, list[Any]]) -> FakeGemini:
        fake = FakeGemini(script)
        monkeypatch.setattr(gemini_module, "_client", SimpleNamespace(aio=SimpleNamespace(models=fake)))
        return fake

    return _install


class FakeHttp:
    """Вместо сети для OpenAI-совместимых провайдеров: ответы по (хост, модель) по очереди."""

    def __init__(self) -> None:
        self.script: dict[str, list[Any]] = {}
        self.requests: list[httpx.Request] = []

    def on(self, model: str, *outcomes: Any) -> None:
        self.script.setdefault(model, []).extend(outcomes)

    @property
    def models(self) -> list[str]:
        return [self.body(index)["model"] for index in range(len(self.requests))]

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        model = json.loads(request.content)["model"]
        queue = self.script.get(model) or [chat({"error": "no script"}, status=500)]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception):
            raise outcome
        # Свежая копия: один и тот же ответ может понадобиться несколько раз.
        return httpx.Response(outcome.status_code, headers=outcome.headers, content=outcome.content)


def chat(answer: Any, *, status: int = 200, headers: dict[str, str] | None = None, finish: str = "stop") -> httpx.Response:
    """Ответ /chat/completions: dict -> JSON в content; str -> content как есть; status >= 400 -> тело ошибки."""
    if status >= 400:
        body = answer if isinstance(answer, dict) else {"error": {"message": str(answer)}}
        return httpx.Response(status, json=body, headers=headers or {})
    content = json.dumps(answer, ensure_ascii=False) if isinstance(answer, dict) else answer
    body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}]}
    return httpx.Response(200, json=body, headers=headers or {})


def error(status: int, message: str, **extra: Any) -> httpx.Response:
    return chat({"error": {"message": message, **extra}}, status=status)


@pytest.fixture
async def http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(openai_compat, "_transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(openai_compat, "_http", None)
    yield fake
    await openai_compat.close_http()


@pytest.fixture
def chain_env(set_env) -> Callable[..., None]:
    """AI включён; по умолчанию — Gemini (gem-a, gem-b) и Groq (gpt-oss-120b, qwen3.8-27b)."""

    def _set(**overrides: str) -> None:
        values = {
            "AI_PROVIDER": "auto",
            "AI_PROVIDERS": "gemini,groq",
            "GEMINI_API_KEY": GEMINI_KEY,
            "GEMINI_MODELS": "gem-a,gem-b",
            "GROQ_API_KEY": GROQ_KEY,
            "GROQ_MODELS": "openai/gpt-oss-120b,qwen/qwen3.8-27b",
            "AI_VISION_MODELS": "qwen/qwen3.8-27b",
            "AI_TIMEOUT_SEC": "60",
        }
        values.update(overrides)
        set_env(**values)

    _set()
    return _set


def api_error(code: int, message: str, status: str, details: list | None = None) -> genai_errors.APIError:
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    body: dict[str, Any] = {"code": code, "message": message, "status": status}
    if details is not None:
        body["details"] = details
    return cls(code, {"error": body})


_QUOTA_TEXT = (
    "You exceeded your current quota, please check your plan and billing details. "
    "* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
    "limit: 20, model: gem-a."
)
# Минутный лимит без подсказки «повторите через…» — пауза 10 минут.
GEMINI_QUOTA = api_error(429, _QUOTA_TEXT, "RESOURCE_EXHAUSTED")
# Дневной лимит (как в настоящем ответе Gemini: QuotaFailure с PerDay и RetryInfo) — пауза не меньше часа.
GEMINI_DAILY_QUOTA = api_error(
    429,
    _QUOTA_TEXT + " Please retry in 37.5s.",
    "RESOURCE_EXHAUSTED",
    details=[
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}],
        },
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"},
    ],
)
GEMINI_MINUTE_QUOTA = api_error(429, _QUOTA_TEXT + " Please retry in 37.5s.", "RESOURCE_EXHAUSTED")
GEMINI_FREE_TIER_ENDED = api_error(
    400, "Gemini API free tier is not available. Please enable billing on your project.", "FAILED_PRECONDITION"
)


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


async def ask(parts: list | None = None) -> tuple[dict, str]:
    return await generate_json(system="Ты — помощник.", parts=parts or ["План 100, факт 95"], schema=SCHEMA)


# --- Настройки: какие провайдеры в цепочке ------------------------------------------------------------


def test_default_chain_order_and_only_providers_with_keys(set_env) -> None:
    settings = get_settings()
    assert settings.ai_providers == ["gemini", "groq", "cloudflare", "mistral", "openrouter"]
    assert settings.active_ai_providers == [] and ai_available() is False

    set_env(AI_PROVIDER="auto", GROQ_API_KEY=GROQ_KEY, OPENROUTER_API_KEY=OPENROUTER_KEY)
    assert get_settings().active_ai_providers == ["groq", "openrouter"]
    assert ai_available() is True  # ключ Gemini не нужен: хватает любого провайдера
    assert [p.name for p in active_chain()] == ["groq", "openrouter"]

    set_env(GEMINI_API_KEY=GEMINI_KEY)
    assert [p.name for p in active_chain()] == ["gemini", "groq", "openrouter"]


def test_chain_order_from_settings_and_unknown_names_ignored(set_env) -> None:
    set_env(
        AI_PROVIDER="auto", AI_PROVIDERS=" Groq, github, gemini, groq ", GEMINI_API_KEY=GEMINI_KEY, GROQ_API_KEY=GROQ_KEY
    )
    assert get_settings().ai_providers == ["groq", "github", "gemini", "groq"]
    assert get_settings().active_ai_providers == ["groq", "gemini"]


def test_cloudflare_needs_token_and_account(set_env) -> None:
    set_env(AI_PROVIDER="auto", CLOUDFLARE_API_TOKEN=CF_TOKEN)
    assert get_settings().active_ai_providers == [] and not ai_available()
    set_env(CLOUDFLARE_ACCOUNT_ID=CF_ACCOUNT)
    [cloudflare] = active_chain()
    assert cloudflare.url == f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/v1/chat/completions"


@pytest.mark.parametrize("value", ["gemini", "GEMINI", "auto", " Auto "])
def test_ai_provider_legacy_gemini_value_means_on(set_env, value) -> None:
    set_env(AI_PROVIDER=value, GROQ_API_KEY=GROQ_KEY)
    assert ai_available() is True


async def test_ai_provider_none_disables_everything_and_rules_work(set_env, http) -> None:
    set_env(AI_PROVIDER="none", GEMINI_API_KEY=GEMINI_KEY, GROQ_API_KEY=GROQ_KEY)
    assert ai_available() is False and active_chain() == []
    with pytest.raises(AIUnavailable):
        await ask()
    task, sub = make_pair()
    result = await evaluate_submission(task, sub)
    assert (result.source, result.score) == ("rules", 110)
    assert (await suggest_expected_result("Анализ", "проверить 100 договоров")).source == "rules"
    assert http.requests == []


def test_keys_are_hidden_in_settings_repr(chain_env) -> None:
    text = repr(get_settings())
    assert GEMINI_KEY not in text and GROQ_KEY not in text


# --- Переход Gemini → Groq при каждом классе сбоя -------------------------------------------------------


@pytest.mark.parametrize(
    "gemini_failure",
    [
        GEMINI_QUOTA,                                                                   # лимит
        GEMINI_FREE_TIER_ENDED,                                                         # бесплатный тариф закончился
        api_error(429, "Quota exceeded for metric: free_tier_requests, limit: 0", "RESOURCE_EXHAUSTED"),
        api_error(403, "Your project has been denied access. Please contact support.", "PERMISSION_DENIED"),
        api_error(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT"),
        api_error(404, "models/gem-a is not found for API version v1beta", "NOT_FOUND"),
        api_error(503, "The model is overloaded. Please try again later.", "UNAVAILABLE"),
        asyncio.TimeoutError(),
        ConnectionResetError("reset"),
        "это не JSON",
        "",
        json.dumps({"score": 90}),                                                      # не по схеме
    ],
)
async def test_gemini_failure_falls_through_to_groq(chain_env, fake_gemini, http, gemini_failure) -> None:
    gem = fake_gemini({"gem-a": [gemini_failure], "gem-b": [gemini_failure]})
    http.on("openai/gpt-oss-120b", chat(GOOD))
    data, model = await ask()
    assert (data, model) == (GOOD, "groq:openai/gpt-oss-120b")
    assert gem.calls and set(gem.calls) <= {"gem-a", "gem-b"}
    request = http.requests[0]
    assert request.url == GROQ_URL
    assert request.headers["authorization"] == f"Bearer {GROQ_KEY}"


@pytest.mark.parametrize(
    ("response", "expected_calls"),
    [
        (error(429, "Rate limit reached for model on requests per minute (RPM)"), 2),
        (error(402, "Insufficient credits"), 1),                                        # весь провайдер
        (error(401, "Invalid API Key"), 1),                                              # весь провайдер
        (error(403, "Your account requires billing to use this model"), 2),
        (error(404, "The model `openai/gpt-oss-120b` does not exist"), 2),
        (error(500, "Internal Server Error"), 2),
        (error(503, "Service Unavailable"), 2),
        (httpx.ReadTimeout("timed out"), 2),
        (httpx.ConnectError("connection refused"), 2),
        (chat("не JSON вовсе"), 2),
        (chat(""), 2),
        (chat({"score": "очень хорошо", "rationale": "x", "note": None}), 2),  # score не число
        (chat({"rationale": "нет оценки", "note": None}), 2),                   # нет обязательного поля
        (chat({"error": {"code": 429, "message": "Rate limit exceeded upstream"}}), 2),  # ошибка с кодом 200
    ],
)
async def test_groq_failure_classes_then_rules(chain_env, http, response, expected_calls) -> None:
    """Gemini не настроен; первая модель Groq отказала — вторая (или, если отказ касается всего
    провайдера, сразу никто) отвечает; если не ответил никто — AIUnavailable, оценка по правилам."""
    chain_env(GEMINI_API_KEY="")
    http.on("openai/gpt-oss-120b", response)
    http.on("qwen/qwen3.8-27b", error(503, "down"))
    with pytest.raises(AIUnavailable):
        await ask()
    assert len(http.requests) == expected_calls
    task, sub = make_pair()
    provider.reset_state()
    result = await evaluate_submission(task, sub)
    assert result.source == "rules" and result.rationale.startswith("Расчёт по правилам (AI недоступен)")


async def test_second_groq_model_answers_after_first_fails(chain_env, http) -> None:
    chain_env(GEMINI_API_KEY="")
    http.on("openai/gpt-oss-120b", error(429, "Rate limit reached on tokens per day (TPD)"))
    http.on("qwen/qwen3.8-27b", chat("```json\n" + json.dumps(GOOD) + "\n```"))
    assert await ask() == (GOOD, "groq:qwen/qwen3.8-27b")


async def test_think_block_and_fences_are_stripped(chain_env, http) -> None:
    chain_env(GEMINI_API_KEY="")
    http.on("openai/gpt-oss-120b", chat("<think>посчитаю {план}</think>\n" + json.dumps(GOOD)))
    assert (await ask())[0] == GOOD


async def test_evaluate_end_to_end_via_groq_when_gemini_paid_only(chain_env, fake_gemini, http) -> None:
    fake_gemini({"gem-a": [GEMINI_FREE_TIER_ENDED], "gem-b": [GEMINI_FREE_TIER_ENDED]})
    http.on(
        "openai/gpt-oss-120b",
        chat({"score": 110, "rationale": "План 100, факт 110 — перевыполнение.", "completeness": "exceeded"}),
    )
    task, sub = make_pair()
    result = await evaluate_submission(task, sub)
    assert (result.source, result.score, result.model) == ("ai", 110, "groq:openai/gpt-oss-120b")
    body = http.body()
    assert body["max_tokens"] == 2048                     # потолок Groq, а не 8192 для Gemini
    assert body["response_format"]["type"] == "json_schema"
    assert "completeness" in body["response_format"]["json_schema"]["schema"]["properties"]
    assert "ФОРМАТ ОТВЕТА" in body["messages"][0]["content"]  # схема продублирована в инструкции
    assert "Проверено 110 договоров" in body["messages"][1]["content"]


# --- Паузы после отказов ------------------------------------------------------------------------------


async def test_quota_pauses_model_then_retries_after_cooldown(chain_env, fake_gemini, http, clock) -> None:
    gem = fake_gemini({"gem-a": [GEMINI_QUOTA, json.dumps(GOOD)], "gem-b": [json.dumps(GOOD)]})
    assert (await ask())[1] == "gem-b"
    assert gem.calls == ["gem-a", "gem-b"]

    clock.advance(5 * 60)
    assert (await ask())[1] == "gem-b"
    assert gem.calls == ["gem-a", "gem-b", "gem-b"]  # gem-a на паузе — в неё не стучимся

    clock.advance(6 * 60)                             # прошло > 10 минут
    assert (await ask())[1] == "gem-a"
    assert http.requests == []


async def test_gemini_daily_quota_pauses_an_hour_minute_quota_follows_retry_delay(
    chain_env, fake_gemini, http, clock
) -> None:
    gem = fake_gemini({"gem-a": [GEMINI_DAILY_QUOTA, json.dumps(GOOD)], "gem-b": [GEMINI_MINUTE_QUOTA, json.dumps(GOOD)]})
    http.on("openai/gpt-oss-120b", chat(GOOD))
    assert (await ask())[1] == "groq:openai/gpt-oss-120b"
    clock.advance(60)  # минутный лимит gem-b (37,5 с) прошёл, дневной у gem-a — нет
    assert (await ask())[1] == "gem-b"
    clock.advance(provider.DAILY_QUOTA_COOLDOWN_SEC)
    assert (await ask())[1] == "gem-a"
    assert gem.calls == ["gem-a", "gem-b", "gem-b", "gem-a"]


async def test_retry_after_header_sets_pause(chain_env, http, clock) -> None:
    chain_env(GEMINI_API_KEY="", GROQ_MODELS="openai/gpt-oss-120b")
    http.on("openai/gpt-oss-120b", chat("x", status=429, headers={"retry-after": "120"}), chat(GOOD))
    with pytest.raises(AIUnavailable):
        await ask()
    clock.advance(60)
    with pytest.raises(AIUnavailable, match="на паузе"):
        await ask()
    assert len(http.requests) == 1
    clock.advance(61)
    assert (await ask())[0] == GOOD


async def test_daily_quota_pauses_at_least_an_hour(chain_env, http, clock) -> None:
    chain_env(GEMINI_API_KEY="", GROQ_MODELS="openai/gpt-oss-120b")
    http.on(
        "openai/gpt-oss-120b",
        error(429, "Rate limit reached on tokens per day (TPD): Limit 200000. Please try again in 7m12s."),
        chat(GOOD),
    )
    with pytest.raises(AIUnavailable):
        await ask()
    clock.advance(30 * 60)
    with pytest.raises(AIUnavailable, match="на паузе"):
        await ask()
    clock.advance(31 * 60)
    assert (await ask())[0] == GOOD


async def test_billing_pause_lasts_hours_and_all_paused_is_instant(chain_env, fake_gemini, http, clock) -> None:
    """Бесплатный тариф Gemini закончился (FAILED_PRECONDITION на весь API, без названия модели): весь Gemini
    на паузе 6 ч после первого же отказа — остальные модели не спрашиваются; запросы сразу идут в Groq."""
    gem = fake_gemini({"gem-a": [GEMINI_FREE_TIER_ENDED], "gem-b": [GEMINI_FREE_TIER_ENDED]})
    http.on("openai/gpt-oss-120b", chat(GOOD))
    await ask()
    assert gem.calls == ["gem-a"]
    clock.advance(5 * 3600)
    for _ in range(3):
        assert (await ask())[1] == "groq:openai/gpt-oss-120b"
    assert gem.calls == ["gem-a"]
    clock.advance(3600 + 1)
    await ask()
    assert gem.calls == ["gem-a", "gem-a"]


@pytest.mark.parametrize(
    "project_error",
    [
        api_error(
            400,
            "User location is not supported for the API use without a billing account linked.",
            "FAILED_PRECONDITION",
        ),
        api_error(
            403,
            "Generative Language API has not been used in project 123 before or it is disabled.",
            "PERMISSION_DENIED",
            details=[{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED"}],
        ),
    ],
)
async def test_project_wide_gemini_errors_skip_all_gemini_models(
    chain_env, fake_gemini, http, clock, project_error
) -> None:
    """Регион не поддерживается / API не включён в проекте — это про весь Gemini, а не про одну модель:
    одна попытка вместо запроса к каждой модели по очереди."""
    gem = fake_gemini({"gem-a": [project_error], "gem-b": [json.dumps(GOOD)]})
    http.on("openai/gpt-oss-120b", chat(GOOD))
    assert (await ask())[1] == "groq:openai/gpt-oss-120b"
    assert (await ask())[1] == "groq:openai/gpt-oss-120b"
    assert gem.calls == ["gem-a"]


async def test_bad_key_pauses_whole_provider(chain_env, fake_gemini, http, clock) -> None:
    gem = fake_gemini({
        "gem-a": [api_error(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT")],
        "gem-b": [json.dumps(GOOD)],
    })
    http.on("openai/gpt-oss-120b", chat(GOOD))
    await ask()
    await ask()
    assert gem.calls == ["gem-a"]  # ни gem-b, ни повторных запросов к Gemini


async def test_everything_paused_raises_without_requests(chain_env, fake_gemini, http, clock) -> None:
    fake_gemini({"gem-a": [GEMINI_QUOTA], "gem-b": [GEMINI_QUOTA]})
    http.on("openai/gpt-oss-120b", error(429, "Rate limit reached for requests per minute"))
    http.on("qwen/qwen3.8-27b", error(401, "Invalid API Key"))
    with pytest.raises(AIUnavailable):
        await ask()
    sent = len(http.requests)
    with pytest.raises(AIUnavailable, match="на паузе"):
        await ask()
    assert len(http.requests) == sent


async def test_transient_errors_pause_briefly(chain_env, http, clock) -> None:
    chain_env(GEMINI_API_KEY="", GROQ_MODELS="openai/gpt-oss-120b")
    http.on("openai/gpt-oss-120b", error(503, "overloaded"), chat(GOOD))
    with pytest.raises(AIUnavailable):
        await ask()
    clock.advance(provider.SHORT_COOLDOWN_SEC + 1)
    assert (await ask())[0] == GOOD


async def test_bad_answer_does_not_pause(chain_env, http, clock) -> None:
    chain_env(GEMINI_API_KEY="", GROQ_MODELS="openai/gpt-oss-120b")
    http.on("openai/gpt-oss-120b", chat("не JSON"), chat(GOOD))
    with pytest.raises(AIUnavailable):
        await ask()
    assert (await ask())[0] == GOOD  # сразу, без ожидания


@pytest.mark.parametrize(
    ("err", "seconds"),
    [
        (ProviderError(Failure.QUOTA), provider.QUOTA_COOLDOWN_SEC),
        (ProviderError(Failure.QUOTA, retry_after=5), 30),
        (ProviderError(Failure.QUOTA, retry_after=90), 90),
        (ProviderError(Failure.QUOTA, retry_after=40, daily=True), provider.DAILY_QUOTA_COOLDOWN_SEC),
        (ProviderError(Failure.BILLING), provider.LONG_COOLDOWN_SEC),
        (ProviderError(Failure.AUTH), provider.LONG_COOLDOWN_SEC),
        (ProviderError(Failure.NOT_FOUND), provider.LONG_COOLDOWN_SEC),
        (ProviderError(Failure.OVERLOADED), provider.SHORT_COOLDOWN_SEC),
        (ProviderError(Failure.TIMEOUT), provider.SHORT_COOLDOWN_SEC),
        (ProviderError(Failure.NETWORK), 0),
        (ProviderError(Failure.BAD_ANSWER), 0),
        (ProviderError(Failure.BAD_REQUEST), 0),
    ],
)
def test_cooldown_durations(err, seconds) -> None:
    assert provider.cooldown_sec(err) == seconds


# --- Время и очередь ----------------------------------------------------------------------------------


async def test_timeout_defers_provider_so_others_get_the_time(chain_env, fake_gemini, http) -> None:
    """gem-a не ответила вовремя — дальше спрашивается Groq, а не gem-b того же (медленного) Gemini."""
    gem = fake_gemini({"gem-a": [asyncio.TimeoutError()], "gem-b": [json.dumps(GOOD)]})
    http.on("openai/gpt-oss-120b", chat(GOOD))
    assert (await ask())[1] == "groq:openai/gpt-oss-120b"
    assert gem.calls == ["gem-a"]


async def test_timeout_with_single_provider_still_tries_next_model(chain_env, fake_gemini) -> None:
    chain_env(GROQ_API_KEY="")
    gem = fake_gemini({"gem-a": [asyncio.TimeoutError()], "gem-b": [json.dumps(GOOD)]})
    assert (await ask())[1] == "gem-b"
    assert gem.calls == ["gem-a", "gem-b"]


async def test_chain_stops_when_time_budget_is_spent(chain_env, http, clock, monkeypatch) -> None:
    class SlowModels(FakeGemini):
        async def generate_content(self, *, model: str, contents: list, config: Any) -> Any:
            clock.advance(provider.chain_budget_sec())  # «висели» всё отведённое время
            return await super().generate_content(model=model, contents=contents, config=config)

    slow = SlowModels({"gem-a": [asyncio.TimeoutError()], "gem-b": [json.dumps(GOOD)]})
    monkeypatch.setattr(gemini_module, "_client", SimpleNamespace(aio=SimpleNamespace(models=slow)))
    with pytest.raises(AIUnavailable, match="время на ответ AI истекло"):
        await ask()
    assert slow.calls == ["gem-a"] and http.requests == []


async def test_timeout_of_a_shortened_attempt_does_not_pause_the_model(chain_env, clock, monkeypatch) -> None:
    """gem-a «висела» полную попытку (AI_TIMEOUT_SEC + 5) — пауза 2 мин. gem-b досталось меньше (остаток
    времени перебора), и она не успела — это не повод ставить на паузу здоровую модель для всех."""
    chain_env(GROQ_API_KEY="")

    class Slow(FakeGemini):
        async def generate_content(self, *, model: str, contents: list, config: Any) -> Any:
            clock.advance(65 if model == "gem-a" else 10)
            return await super().generate_content(model=model, contents=contents, config=config)

    slow = Slow({"gem-a": [asyncio.TimeoutError(), json.dumps(GOOD)], "gem-b": [asyncio.TimeoutError(), json.dumps(GOOD)]})
    monkeypatch.setattr(gemini_module, "_client", SimpleNamespace(aio=SimpleNamespace(models=slow)))
    with pytest.raises(AIUnavailable):
        await ask()
    assert slow.calls == ["gem-a", "gem-b"]
    assert provider._cooldown_reason("gemini", "gem-a") is not None
    assert provider._cooldown_reason("gemini", "gem-b") is None
    assert (await ask())[1] == "gem-b"


async def test_waiting_for_a_busy_provider_counts_against_the_budget(chain_env, http) -> None:
    """Оба места у Groq заняты другими запросами: третий ждёт не дольше своего времени и сдаётся сам."""
    chain_env(GEMINI_API_KEY="")
    http.on("openai/gpt-oss-120b", chat(GOOD))
    semaphore = provider._semaphore("groq")
    for _ in range(provider._CONCURRENCY):
        await semaphore.acquire()
    started = asyncio.get_running_loop().time()
    with pytest.raises(AIUnavailable, match="провайдер занят"):
        await generate_json(system="s", parts=["p"], schema=SCHEMA, time_budget=provider._MIN_ATTEMPT_SEC + 0.3)
    assert asyncio.get_running_loop().time() - started < 2
    assert http.requests == []
    for _ in range(provider._CONCURRENCY):
        semaphore.release()
    assert (await ask())[0] == GOOD


async def test_time_budget_of_the_caller_limits_the_chain(chain_env, fake_gemini, http) -> None:
    gem = fake_gemini({"gem-a": [json.dumps(GOOD)]})
    with pytest.raises(AIUnavailable, match="время на ответ AI истекло"):
        await generate_json(system="s", parts=["p"], schema=SCHEMA, time_budget=1)
    assert gem.calls == [] and http.requests == []


async def test_openai_compat_attempt_never_outlives_its_timeout(chain_env, monkeypatch) -> None:
    """Попытка у OpenAI-совместимого провайдера укладывается в timeout целиком: без «+5 с» и с повтором
    (после отказа от response_format) только на оставшееся время."""
    chain_env(GEMINI_API_KEY="")

    async def hanging(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return chat(GOOD)

    monkeypatch.setattr(openai_compat, "_transport", httpx.MockTransport(hanging))
    monkeypatch.setattr(openai_compat, "_http", None)
    [groq] = active_chain()
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(ProviderError) as info:
        await groq.generate(model="openai/gpt-oss-120b", system="s", parts=["p"], schema=SCHEMA, max_output_tokens=10, timeout=0.3)
    assert info.value.kind == Failure.TIMEOUT and loop.time() - started < 2
    await openai_compat.close_http()

    timeouts: list[float] = []

    async def post(self, payload: dict, timeout: float) -> tuple[int, dict, str]:
        timeouts.append(timeout)
        await asyncio.sleep(0.3)
        if len(timeouts) == 1:
            return 400, {}, json.dumps({"error": {"message": "response_format json_schema is not supported"}})
        return 200, {}, json.dumps({"choices": [{"message": {"content": json.dumps(GOOD)}}]})

    monkeypatch.setattr(openai_compat.OpenAICompatProvider, "_post", post)
    await groq.generate(model="openai/gpt-oss-120b", system="s", parts=["p"], schema=SCHEMA, max_output_tokens=10, timeout=5)
    assert timeouts[0] == 5 and timeouts[1] <= 4.75


async def test_minute_limit_with_4006_in_digits_pauses_only_the_model_briefly(chain_env, fake_gemini, http, clock) -> None:
    """«Please retry in 21.400612s» (Gemini) и «Used 4006» (Groq) — минутные лимиты одной модели,
    а не дневной лимит аккаунта Cloudflare (код 4006): пауза ~30 с, остальные модели спрашиваются."""
    gem = fake_gemini({
        "gem-a": [api_error(429, _QUOTA_TEXT + " Please retry in 21.400612s.", "RESOURCE_EXHAUSTED"), json.dumps(GOOD)],
        "gem-b": [json.dumps(GOOD)],
    })
    assert (await ask())[1] == "gem-b"
    clock.advance(31)
    assert (await ask())[1] == "gem-a"
    assert gem.calls == ["gem-a", "gem-b", "gem-a"]

    chain_env(GEMINI_API_KEY="")
    tpm = "Rate limit reached on tokens per minute (TPM): Limit 8000, Used 4006, Requested 4500. Please try again in 18.2s."
    http.on("openai/gpt-oss-120b", error(429, tpm, code="rate_limit_exceeded"), chat(GOOD))
    http.on("qwen/qwen3.8-27b", chat(GOOD))
    assert (await ask())[1] == "groq:qwen/qwen3.8-27b"
    clock.advance(31)
    assert (await ask())[1] == "groq:openai/gpt-oss-120b"


async def test_request_too_large_tries_next_model_without_pause(chain_env, http) -> None:
    """Groq: запрос с фото больше минутного лимита токенов модели, видящей фото (413). Следующая модель
    того же провайдера получает текст без фото и отвечает; модель с фото на паузу не ставится."""
    chain_env(GEMINI_API_KEY="")
    http.on("qwen/qwen3.8-27b", error(413, "Request too large for model `qwen/qwen3.8-27b`"), chat(GOOD))
    http.on("openai/gpt-oss-120b", chat(GOOD))
    assert (await ask(evidence_parts()))[1] == "groq:openai/gpt-oss-120b"
    assert isinstance(http.body()["messages"][1]["content"], str)  # без фото — пометка вместо него
    assert (await ask(evidence_parts()))[1] == "groq:qwen/qwen3.8-27b"


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps(GOOD) + "\nГотово.",
        "Ответ: " + json.dumps(GOOD) + "\nГотово.",
        "```json\n" + json.dumps(GOOD) + "\n```\nЕсли нужно, уточню.",
        "Считаю {план 100}: " + json.dumps(GOOD),
    ],
)
async def test_json_object_with_text_around_is_accepted(chain_env, http, answer) -> None:
    chain_env(GEMINI_API_KEY="")
    http.on("openai/gpt-oss-120b", chat(answer))
    assert await ask() == (GOOD, "groq:openai/gpt-oss-120b")


def test_chain_budget_and_busy_flag_follow_timeout(set_env) -> None:
    from bot.ai.evaluate import evaluation_budget_sec
    from bot.handlers.task_create import _ai_busy_stale_sec

    set_env(AI_TIMEOUT_SEC="30")
    assert provider.chain_budget_sec() == 60
    assert evaluation_budget_sec() == 90
    assert _ai_busy_stale_sec() > provider.chain_budget_sec() + 30


# --- Файлы-подтверждения: изображения только моделям, которые их видят -----------------------------------


def evidence_parts() -> list:
    return [
        "Задача и факт",
        "Файл 1: акт.pdf (PDF). Содержимое файла — следующей частью.",
        genai_types.Part.from_bytes(data=PDF, mime_type="application/pdf"),
        "Файл 2: Фото 2 (изображение). Содержимое файла — следующей частью.",
        genai_types.Part.from_bytes(data=PNG, mime_type="image/png"),
    ]


async def test_images_go_to_vision_model_first_as_data_url(chain_env, http) -> None:
    chain_env(GEMINI_API_KEY="")
    http.on("qwen/qwen3.8-27b", chat(GOOD))
    assert (await ask(evidence_parts()))[1] == "groq:qwen/qwen3.8-27b"  # видит изображения — спрошена первой
    content = http.body()["messages"][1]["content"]
    assert isinstance(content, list)
    images = [item for item in content if item["type"] == "image_url"]
    assert [item["image_url"]["url"] for item in images] == ["data:image/png;base64," + base64.b64encode(PNG).decode()]
    text = " ".join(item["text"] for item in content if item["type"] == "text")
    assert "PDF-файл приложен" in text and "не читает PDF" in text
    assert base64.b64encode(PDF).decode() not in json.dumps(http.body())


async def test_no_vision_model_gets_text_notes_only(chain_env, http) -> None:
    chain_env(GEMINI_API_KEY="", AI_VISION_MODELS="")
    http.on("openai/gpt-oss-120b", chat(GOOD))
    assert (await ask(evidence_parts()))[1] == "groq:openai/gpt-oss-120b"
    content = http.body()["messages"][1]["content"]
    assert isinstance(content, str)  # без изображений — одна строка
    assert "Изображение приложено" in content and "не видит изображения" in content
    assert "PDF-файл приложен" in content and "base64" not in content


async def test_gemini_gets_files_as_bytes_and_gemma_gets_pdf_note(chain_env, fake_gemini) -> None:
    chain_env(GROQ_API_KEY="", GEMINI_MODELS="gemini-3.8-flash,gemma-4-31b-it")
    gem = fake_gemini({"gemini-3.8-flash": [GEMINI_QUOTA], "gemma-4-31b-it": [json.dumps(GOOD)]})
    assert (await ask(evidence_parts()))[1] == "gemma-4-31b-it"
    flash, gemma = gem.contents
    assert sum(isinstance(part, genai_types.Part) for part in flash) == 2       # PDF и фото — байтами
    assert [part.inline_data.mime_type for part in gemma if isinstance(part, genai_types.Part)] == ["image/png"]
    assert any(isinstance(part, str) and "PDF-файл приложен" in part for part in gemma)
    # Gemma не умеет «JSON по схеме» в API — схема в инструкции, без response_mime_type.
    assert gem.configs[0].response_mime_type == "application/json"
    # Инструкция — первой частью запроса (system_instruction у Gemma в API бывает выключен).
    assert gem.configs[1].response_mime_type is None and gem.configs[1].system_instruction is None
    assert gemma[0].startswith("Ты — помощник.") and "ФОРМАТ ОТВЕТА" in gemma[0]
    assert gem.configs[1].automatic_function_calling.disable is True


def test_build_user_content_limits_images() -> None:
    big = genai_types.Part.from_bytes(data=b"x" * 100, mime_type="image/jpeg")
    parts = ["текст", big, big, big]
    content = openai_compat.build_user_content(parts, vision=True, max_chars=10_000, max_images=2, max_image_bytes=1000)
    assert sum(item["type"] == "image_url" for item in content) == 2
    assert "слишком много изображений" in json.dumps(content, ensure_ascii=False)
    content = openai_compat.build_user_content(parts, vision=True, max_chars=10_000, max_image_bytes=50)
    assert isinstance(content, str) and content.count("изображение слишком большое") == 3


async def test_long_file_text_is_trimmed_to_provider_budget(chain_env, http) -> None:
    """Groq бесплатно — 8000 токенов в минуту: запрос вместе с системной инструкцией ужимается до лимита;
    сокращается только текст файла (середина данных), заголовок, «<<<» и «>>>» остаются."""
    chain_env(GEMINI_API_KEY="")
    http.on("openai/gpt-oss-120b", chat(GOOD))
    [file_part] = evidence_to_parts([EvidenceItem(name="отчёт.docx", kind="text", text="строка отчёта " * 5000 + "КОНЕЦ")])
    await ask(["ЗАДАЧА: проверить 100 договоров", file_part, "Ответь JSON."])
    system, content = (message["content"] for message in http.body()["messages"])
    assert len(system) + len(content) <= openai_compat.SPECS["groq"].max_input_chars + 10
    assert content.startswith("ЗАДАЧА: проверить 100 договоров")  # короткие части не тронуты
    assert "Файл 1: отчёт.docx (Word)" in content and "\n<<<\nстрока отчёта" in content
    assert "лимит бесплатного AI" in content and "КОНЕЦ" not in content
    assert content.endswith("лимит бесплатного AI]…\n>>>\n\nОтветь JSON.")


def data_text(label: str, body: str, order: int = TRIM_FILES, after: str = "") -> DataText:
    return DataText(f"{label}\n<<<\n{body}\n>>>{after}", order)


def test_fit_texts_cuts_only_the_longest_data_parts() -> None:
    texts = ["a" * 3000, data_text("Файл 1", "b" * 5000), data_text("Файл 2", "c" * 3000), data_text("Файл 3", "d" * 50)]
    fitted = openai_compat.fit_texts(texts, 5000)
    assert fitted[0] == texts[0] and fitted[3] == texts[3]  # обычные строки и короткие данные не тронуты
    assert sum(map(len, fitted)) <= 5000
    assert len(fitted[1]) == len(fitted[2])
    for part in fitted[1:3]:
        assert part.count("<<<") == 1 and part.endswith("…\n>>>")


def test_fit_texts_trims_files_before_the_fact_and_never_the_task() -> None:
    task = "ЗАДАЧА: " + "т" * 2000
    fact = data_text("ФАКТ", "Фактическое значение: 85\n" + "ф" * 1500, TRIM_FACT, after="\nПриложено файлов: 1.")
    file_part = data_text("Файл 1", "x" * 6000)
    fitted = openai_compat.fit_texts([task, fact, file_part], 4000)
    assert fitted[0] == task and fitted[1] == fact          # хватило сокращения файла
    assert len(fitted[2]) < 1000 and fitted[2].endswith("\n>>>")

    fitted = openai_compat.fit_texts([task, fact, file_part], 2600)  # файла мало — сокращается и факт
    assert fitted[0] == task
    assert fitted[1].startswith("ФАКТ\n<<<\nФактическое значение: 85\n")
    assert fitted[1].endswith("…\n>>>\nПриложено файлов: 1.")
    assert sum(map(len, fitted)) <= 2600 + 200  # обязательные заголовки и маркеры остаются в любом случае


def test_cut_data_keeps_markers_even_below_the_limit() -> None:
    part = data_text("Файл 1: отчёт", "z" * 500, after="\nстрока бота после блока")
    cut = openai_compat.fit_texts([part], 10)[0]
    assert cut.startswith("Файл 1: отчёт\n<<<\n") and cut.endswith("\n>>>\nстрока бота после блока")
    assert "z" not in cut and "лимит бесплатного AI" in cut


def test_trimmed_fact_block_stays_closed_and_keeps_numbers() -> None:
    """Groq: длинные «что сделано» / «результат» и три больших файла. Блок факта закрыт («>>>» на месте),
    фактическое значение, результат и строка «факт / план» доходят до AI; тексты файлов — тоже закрыты."""
    task, sub = make_pair()
    sub.fact_value, sub.fact_text, sub.result_text = 85, "Проверено " * 300, "Итог проверки " * 220
    evidence = [EvidenceItem(name=f"f{i}.docx", kind="text", text="строка " * 2100) for i in range(3)]
    parts = _build_parts(task, sub, evidence)
    spec = openai_compat.SPECS["groq"]
    content = openai_compat.build_user_content(parts, vision=False, max_chars=spec.max_input_chars - 2600)
    fact = content[content.index("ФАКТ ОТ СОТРУДНИКА") : content.index("ФАЙЛЫ-ПОДТВЕРЖДЕНИЯ")]
    assert fact.count("\n<<<\n") == 1 and fact.count("\n>>>\n") == 1
    inside = fact[fact.index("<<<") : fact.index(">>>")]
    assert "Фактическое значение: 85 договоров" in inside and "Какой получен результат: Итог" in inside
    assert "Выполнение по числам (факт / план × 100): 85 %." in fact.split(">>>")[1]
    assert content.count("\n<<<\n") == content.count("\n>>>") == 4  # факт + три файла
    assert content.rstrip().endswith("Ответь JSON с полями rationale, completeness, score.")


async def test_groq_image_takes_text_budget_and_only_one_is_sent(chain_env, http) -> None:
    """Изображение у Groq — 2048 токенов из 8000 в минуту: передаётся одно, и текста за него меньше."""
    chain_env(GEMINI_API_KEY="")
    http.on("qwen/qwen3.8-27b", chat(GOOD))
    photo = genai_types.Part.from_bytes(data=PNG, mime_type="image/png")
    [file_part] = evidence_to_parts([EvidenceItem(name="отчёт.docx", kind="text", text="строка " * 3000)])
    await ask(["ЗАДАЧА", file_part, "Фото 1:", photo, "Фото 2:", photo])
    system, content = (message["content"] for message in http.body()["messages"])
    assert sum(item["type"] == "image_url" for item in content) == 1
    text = "\n\n".join(item["text"] for item in content if item["type"] == "text")
    spec = openai_compat.SPECS["groq"]
    assert len(system) + len(text) <= spec.max_input_chars - spec.image_chars + 300
    assert "слишком много изображений" in text and text.count("\n>>>") == 1


# --- Ответ провайдера: формат JSON, ключи, совместимость -------------------------------------------------


async def test_unsupported_response_format_is_retried_as_json_object(chain_env, http) -> None:
    chain_env(GEMINI_API_KEY="")
    http.on(
        "openai/gpt-oss-120b",
        error(400, "response_format `json_schema` is not supported by this model", param="response_format"),
        chat(GOOD),
    )
    assert (await ask())[1] == "groq:openai/gpt-oss-120b"
    first, second = http.body(0), http.body(1)
    assert first["response_format"]["type"] == "json_schema" and first["reasoning_effort"] == "low"
    assert second["response_format"] == {"type": "json_object"} and "reasoning_effort" not in second


async def test_openrouter_uses_json_object_and_account_quota_pauses_provider(set_env, http, clock) -> None:
    set_env(AI_PROVIDER="auto", OPENROUTER_API_KEY=OPENROUTER_KEY, OPENROUTER_MODELS="m1:free,m2:free")
    http.on("m1:free", error(429, "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000."))
    http.on("m2:free", chat(GOOD))
    with pytest.raises(AIUnavailable):
        await ask()
    assert http.models == ["m1:free"]  # дневной лимит на весь аккаунт — m2 не спрашиваем
    assert http.body(0)["response_format"] == {"type": "json_object"}
    assert str(http.requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    clock.advance(30 * 60)
    with pytest.raises(AIUnavailable, match="на паузе"):
        await ask()


async def test_cloudflare_daily_allocation_pauses_provider(set_env, http) -> None:
    set_env(AI_PROVIDER="auto", CLOUDFLARE_API_TOKEN=CF_TOKEN, CLOUDFLARE_ACCOUNT_ID=CF_ACCOUNT)
    first, second = get_settings().cloudflare_models[:2]
    allocation = {"code": 4006, "message": "you have used up your daily free allocation of 10,000 neurons"}
    http.on(first, chat({"errors": [allocation], "success": False}, status=429))
    http.on(second, chat(GOOD))
    with pytest.raises(AIUnavailable):
        await ask()
    assert http.models == [first]


async def test_keys_never_logged(chain_env, fake_gemini, http, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    fake_gemini({
        "gem-a": [api_error(400, f"API key not valid: {GEMINI_KEY}", "INVALID_ARGUMENT")],
        "gem-b": [json.dumps(GOOD)],
    })
    http.on("openai/gpt-oss-120b", error(401, f"Invalid API Key {GROQ_KEY}"))
    http.on("qwen/qwen3.8-27b", chat(GOOD))
    with pytest.raises(AIUnavailable) as info:
        await ask()
    for secret in (GEMINI_KEY, GROQ_KEY):
        assert secret not in caplog.text
        assert secret not in str(info.value)


async def test_unexpected_provider_bug_skips_only_that_provider(chain_env, fake_gemini, http, monkeypatch) -> None:
    fake_gemini({"gem-a": [json.dumps(GOOD)], "gem-b": [json.dumps(GOOD)]})

    async def broken(*args: Any, **kwargs: Any) -> str:
        raise KeyError("bug")

    monkeypatch.setattr(gemini_module.GeminiProvider, "generate", broken)
    http.on("openai/gpt-oss-120b", chat(GOOD))
    assert (await ask())[1] == "groq:openai/gpt-oss-120b"


# --- Распознавание ошибок --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "message", "kind", "scope", "daily"),
    [
        (429, "You exceeded your current quota, please check your plan and billing details. limit: 20", Failure.QUOTA, "model", False),
        (429, "Quota exceeded for metric GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 20", Failure.QUOTA, "model", True),
        (429, "Quota exceeded for metric: free_tier_requests, limit: 0, model: gemini-3.8-flash", Failure.BILLING, "model", False),
        (429, "Rate limit reached for model on tokens per day (TPD)", Failure.QUOTA, "model", True),
        (429, "Rate limit exceeded: free-models-per-day. Add 10 credits", Failure.QUOTA, "provider", True),
        (400, "4006: you have used up your daily free allocation of 10,000 neurons", Failure.QUOTA, "provider", True),
        # Минутный лимит, в цифрах которого случайно есть «4006», — не дневной лимит аккаунта Cloudflare.
        (429, "RESOURCE_EXHAUSTED Quota exceeded, limit: 15. Please retry in 21.400612s.", Failure.QUOTA, "model", False),
        (429, "Rate limit reached on tokens per minute (TPM): Limit 8000, Used 4006, Requested 4500. Please try again in 18.2s.", Failure.QUOTA, "model", False),
        (429, "code=4006 you have used up your allocation", Failure.QUOTA, "provider", True),
        (429, '{"errors": [{"code": 4006, "message": "limit"}]}', Failure.QUOTA, "provider", True),
        (400, "FAILED_PRECONDITION User location is not supported for the API use.", Failure.BILLING, "provider", False),
        (400, "FAILED_PRECONDITION Gemini API free tier is not available. Enable billing.", Failure.BILLING, "provider", False),
        (403, "PERMISSION_DENIED Generative Language API has not been used in project 1 before or it is disabled. SERVICE_DISABLED", Failure.BILLING, "provider", False),
        (403, "PERMISSION_DENIED Requests to this API are blocked. API_KEY_SERVICE_BLOCKED", Failure.BILLING, "provider", False),
        (400, "FAILED_PRECONDITION This model is not available on the free tier", Failure.NOT_FOUND, "model", False),
        (403, "PERMISSION_DENIED Your project has been denied access.", Failure.BILLING, "provider", False),
        (403, "PERMISSION_DENIED Model requires a paid tier", Failure.BILLING, "model", False),
        (402, "Payment required", Failure.BILLING, "provider", False),
        (401, "Invalid API Key", Failure.AUTH, "provider", False),
        (400, "INVALID_ARGUMENT API key not valid. Please pass a valid API key.", Failure.AUTH, "provider", False),
        (403, "Authentication error", Failure.AUTH, "provider", False),
        (404, "models/x is not found", Failure.NOT_FOUND, "model", False),
        (400, "The model `x` has been decommissioned and is no longer supported", Failure.NOT_FOUND, "model", False),
        (400, "json_validate_failed: Failed to generate JSON", Failure.BAD_ANSWER, "model", False),
        (503, "overloaded", Failure.OVERLOADED, "model", False),
        (408, "timeout", Failure.OVERLOADED, "model", False),
        # Запрос слишком большой — только для этой модели (следующая может принять: без фото, больший лимит).
        (413, "Request too large", Failure.BAD_REQUEST, "model", False),
        (429, "Request too large for model `qwen/qwen3.8-27b` on tokens per minute (TPM): Limit 8000, Requested 9200, please reduce your message size and try again.", Failure.BAD_REQUEST, "model", False),
        (400, "This model's maximum context length is 8192 tokens. context_length_exceeded", Failure.BAD_REQUEST, "model", False),
        (400, "Invalid request: messages[1].content too long", Failure.BAD_REQUEST, "provider", False),
    ],
)
def test_classify_http(status, message, kind, scope, daily) -> None:
    err = classify_http(status, message)
    assert (err.kind, err.scope, err.daily) == (kind, scope, daily)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("Please retry in 37.5s.", 37.5),
        ("Please try again in 7m12.5s.", 432.5),
        ('"retryDelay": "40s"', 40),
        ("try again in 1h2m", 3720),
        ("no hint", None),
    ],
)
def test_parse_retry_after(text, seconds) -> None:
    assert parse_retry_after(text) == seconds


# --- Журнал при запуске --------------------------------------------------------------------------------


def test_startup_log_lists_chain_without_keys(chain_env, caplog) -> None:
    from bot.main import _log_ai_chain

    caplog.set_level(logging.INFO, logger="bot.main")
    chain_env(AI_PROVIDERS="gemini,groq,github", CLOUDFLARE_API_TOKEN=CF_TOKEN)
    _log_ai_chain(get_settings())
    assert "gemini (gem-a, gem-b) → groq (openai/gpt-oss-120b, qwen/qwen3.8-27b) → расчёт по правилам" in caplog.text
    assert "github" in caplog.text and "CLOUDFLARE_ACCOUNT_ID" in caplog.text
    for secret in (GEMINI_KEY, GROQ_KEY, CF_TOKEN):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"GEMINI_API_KEY": "", "GROQ_API_KEY": ""}, "не задан ни один ключ"),
        ({"AI_PROVIDER": "none"}, "AI_PROVIDER=none"),
        ({"GROQ_API_KEY": ""}, "запасного провайдера нет"),
    ],
)
def test_startup_log_when_ai_off_or_single(chain_env, caplog, env, expected) -> None:
    from bot.main import _log_ai_chain

    caplog.set_level(logging.INFO, logger="bot.main")
    chain_env(**env)
    _log_ai_chain(get_settings())
    assert expected in caplog.text
