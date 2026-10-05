"""bot.ai: оценка и подсказка по правилам, работа без AI, перебор моделей Gemini (фейковый клиент)."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from openpyxl import Workbook

from bot.ai import evaluate as evaluate_module
from bot.ai import formulate as formulate_module
from bot.ai import provider
from bot.ai.evaluate import Evaluation, evaluate_submission, rules_score
from bot.ai.evidence import EvidenceItem, collect_evidence, evidence_to_parts
from bot.ai.formulate import ResultSuggestion, rules_suggestion, suggest_expected_result
from bot.ai.provider import AIUnavailable, ai_available, generate_json
from bot.db.models import Attachment, AttachmentKind, Submission, Task

DEADLINE = datetime(2026, 10, 5, 13, 0)


def make_pair(*, plan_value: float | None = 100, fact_value: float | None = 110, late_days: float = 0.0):
    """Задача и сдача без БД (evaluate_submission не должен требовать сессию)."""
    task = Task(
        id=1, title="Анализ договоров", expected_result="Проверить 100 договоров", plan_value=plan_value,
        plan_unit="договоров", deadline=DEADLINE, weight=20,
    )
    sub = Submission(
        id=1, attempt=1, fact_text="Проверено договоров", result_text="Отчёт", fact_value=fact_value,
        created_at=DEADLINE, deadline_at_submit=DEADLINE, is_late=late_days > 0, late_days=late_days,
    )
    return task, sub


# --- rules_score ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "fact", "late", "expected"),
    [
        (100, 110, 0, 110),     # ТЗ: план 100, факт 110 -> 110 %
        (100, 100, 2.5, 95),    # ТЗ: полностью, но с задержкой -> 95 %
        (100, 100, 0, 100),
        (None, None, 0, 100),   # без чисел — база 100
        (100, None, 1, 98),     # нет факта — база 100, штраф 2 п.п.
        (100, 100, 30, 80),     # штраф не больше 20 п.п.
        (100, 500, 0, 150),     # не выше max_score
        (100, 0, 0, 0),
        (100, 5, 10, 0),        # не ниже 0
        (0, 50, 0, 100),        # план 0 — база 100
        (3, 1, 0, 33),
        (200, 101, 0, 51),      # 50.5 -> 51 (половина вверх)
    ],
)
def test_rules_score(plan, fact, late, expected) -> None:
    score, explanation = rules_score(plan, fact, late)
    assert score == expected
    assert isinstance(explanation, str) and explanation
    assert "Итог" in explanation


def test_rules_score_explains_lateness() -> None:
    _, on_time = rules_score(100, 100, 0)
    assert "в срок" in on_time
    _, late = rules_score(100, 100, 2.5)
    assert "опоздани" in late and "штраф" in late


def test_rules_score_uses_settings(set_env) -> None:
    set_env(LATE_PENALTY_PER_DAY="10", LATE_PENALTY_MAX="15", MAX_SCORE="120")
    assert rules_score(100, 100, 1)[0] == 90
    assert rules_score(100, 100, 5)[0] == 85
    assert rules_score(100, 300, 0)[0] == 120


# --- rules_suggestion -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "value", "unit"),
    [
        ("проверить 100 договоров и представить отчёт", 100, "договоров"),
        ("обзвонить 1 200 клиентов", 1200, "клиентов"),
        ("снизить брак на 10,5 %", 10.5, "%"),
        ("до 5 октября подготовить 3 отчёта", 3, "отчёта"),
        ("к 05.10 оформить 12 актов", 12, "актов"),
    ],
)
def test_rules_suggestion_extracts_plan(raw: str, value: float, unit: str) -> None:
    suggestion = rules_suggestion("Задача", raw)
    assert isinstance(suggestion, ResultSuggestion)
    assert suggestion.source == "rules"
    assert suggestion.plan_value == value
    assert suggestion.plan_unit == unit
    assert suggestion.note is None
    assert suggestion.expected_result.lower() == " ".join(raw.split()).lower()


def test_rules_suggestion_without_number_gives_hint() -> None:
    suggestion = rules_suggestion("Отчёт", "  подготовить   отчёт по закупкам ")
    assert suggestion.plan_value is None and suggestion.plan_unit is None
    assert suggestion.expected_result == "Подготовить отчёт по закупкам"
    assert suggestion.note and suggestion.note.startswith("Добавьте число или критерий приёмки")
    assert len(suggestion.note) <= 200


def test_rules_suggestion_falls_back_to_title() -> None:
    assert rules_suggestion("Анализ договоров", "").expected_result == "Анализ договоров"


# --- Без AI: никогда не бросает, работает на правилах ------------------------------------------------


def test_ai_disabled_in_tests() -> None:
    assert ai_available() is False


async def test_evaluate_without_ai_uses_rules() -> None:
    task, sub = make_pair(plan_value=100, fact_value=110)
    result = await evaluate_submission(task, sub)
    assert isinstance(result, Evaluation)
    assert (result.score, result.source, result.model) == (110, "rules", None)
    # SPEC 4.3: без ключа / при AI_PROVIDER=none — тот же префикс, что и при сбое AI.
    assert result.rationale.startswith("Расчёт по правилам (AI недоступен): ")


async def test_evaluate_without_ai_late_example() -> None:
    task, sub = make_pair(plan_value=100, fact_value=100, late_days=2.5)
    result = await evaluate_submission(task, sub, [EvidenceItem(name="a.pdf", kind="skipped", note="нет")])
    assert result.score == 95 and result.source == "rules"


async def test_suggest_without_ai_uses_rules() -> None:
    suggestion = await suggest_expected_result("Анализ", "проверить 100 договоров", "5 октября")
    assert suggestion == rules_suggestion("Анализ", "проверить 100 договоров")


async def test_generate_json_without_ai_raises_unavailable() -> None:
    with pytest.raises(AIUnavailable):
        await generate_json(system="s", parts=["p"], schema={"type": "object"})


# --- С AI: ответы и сбои (generate_json подменён) --------------------------------------------------------


@pytest.fixture
def ai_on(set_env) -> None:
    set_env(AI_PROVIDER="gemini", GEMINI_API_KEY="test-key-123", GEMINI_MODELS="model-a,model-b,model-c")
    assert ai_available()


def fake_generate(result: Any) -> Callable[..., Any]:
    async def _generate(**_: Any) -> tuple[dict, str]:
        if isinstance(result, BaseException):
            raise result
        return result, "model-a"

    return _generate


@pytest.mark.parametrize(
    "failure", [AIUnavailable("лимит"), RuntimeError("boom"), asyncio.TimeoutError()]
)
async def test_evaluate_falls_back_to_rules_on_failure(ai_on, monkeypatch, failure) -> None:
    monkeypatch.setattr(evaluate_module, "generate_json", fake_generate(failure))
    task, sub = make_pair(plan_value=100, fact_value=110)
    result = await evaluate_submission(task, sub)
    assert result.source == "rules" and result.score == 110
    assert result.rationale.startswith("Расчёт по правилам (AI недоступен)")


async def test_evaluate_uses_ai_answer_and_clamps(ai_on, monkeypatch) -> None:
    monkeypatch.setattr(
        evaluate_module, "generate_json",
        fake_generate({"score": 180.4, "rationale": "  План 100, факт 110. ", "completeness": "exceeded"}),
    )
    task, sub = make_pair()
    result = await evaluate_submission(task, sub)
    assert (result.score, result.source, result.model) == (150, "ai", "model-a")
    assert result.rationale == "План 100, факт 110."


async def test_evaluate_answer_without_score_falls_back(ai_on, monkeypatch) -> None:
    monkeypatch.setattr(evaluate_module, "generate_json", fake_generate({"rationale": "нет оценки"}))
    task, sub = make_pair(plan_value=100, fact_value=90)
    result = await evaluate_submission(task, sub)
    assert result.source == "rules" and result.score == 90


async def test_suggest_with_ai(ai_on, monkeypatch) -> None:
    monkeypatch.setattr(
        formulate_module, "generate_json",
        fake_generate({"expected_result": "проверить 100 договоров и сдать отчёт", "plan_value": 100,
                       "plan_unit": "договоров", "note": None}),
    )
    suggestion = await suggest_expected_result("Анализ", "посмотреть договоры")
    assert suggestion.source == "ai"
    assert suggestion.expected_result == "Проверить 100 договоров и сдать отчёт"
    assert (suggestion.plan_value, suggestion.plan_unit, suggestion.note) == (100, "договоров", None)


@pytest.mark.parametrize("failure", [AIUnavailable("нет"), ValueError("плохой ответ")])
async def test_suggest_falls_back_on_failure(ai_on, monkeypatch, failure) -> None:
    monkeypatch.setattr(formulate_module, "generate_json", fake_generate(failure))
    suggestion = await suggest_expected_result("Анализ", "проверить 100 договоров")
    assert suggestion.source == "rules" and suggestion.plan_value == 100


async def test_suggest_retry_asks_for_another_variant(ai_on, monkeypatch) -> None:
    """«🔁 Другой вариант» = повторный вызов с теми же словами: модель видит прежний вариант."""
    monkeypatch.setattr(formulate_module, "_seen", OrderedDict())
    prompts: list[str] = []
    answers = iter([
        "Проверить 100 договоров и сдать отчёт",
        "Сдать реестр 100 проверенных договоров",
        "Сдать отчёт о закупках",
    ])

    async def _generate(*, parts: list, **_: Any) -> tuple[dict, str]:
        prompts.append(parts[0])
        return {"expected_result": next(answers), "plan_value": 100, "plan_unit": "договоров", "note": None}, "m"

    monkeypatch.setattr(formulate_module, "generate_json", _generate)
    first = await suggest_expected_result("Анализ", "посмотреть 100 договоров")
    second = await suggest_expected_result("Анализ", "посмотреть 100 договоров")
    await suggest_expected_result("Отчёт", "сдать отчёт")  # другие слова — прежние варианты не нужны
    assert "Уже предложенные варианты" not in prompts[0]
    assert f"«{first.expected_result}»" in prompts[1]
    assert second.expected_result != first.expected_result
    assert "Уже предложенные варианты" not in prompts[2]


async def test_suggest_with_empty_ai_answer_falls_back(ai_on, monkeypatch) -> None:
    monkeypatch.setattr(formulate_module, "generate_json", fake_generate({"expected_result": "  "}))
    assert (await suggest_expected_result("Анализ", "сдать 5 актов")).source == "rules"


# --- provider.generate_json: перебор моделей на фейковом клиенте -------------------------------------


class FakeModels:
    """client.aio.models: для каждой модели — заранее заданный ответ или исключение."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.calls: list[str] = []
        self.configs: list[Any] = []

    async def generate_content(self, *, model: str, contents: list, config: Any) -> Any:
        self.calls.append(model)
        self.configs.append(config)
        outcome = self.script[model]
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(text=outcome)


@pytest.fixture
def fake_client(ai_on, monkeypatch) -> Callable[[dict[str, Any]], FakeModels]:
    def _install(script: dict[str, Any]) -> FakeModels:
        models = FakeModels(script)
        monkeypatch.setattr(provider, "_client", SimpleNamespace(aio=SimpleNamespace(models=models)))
        return models

    return _install


def api_error(cls: type[genai_errors.APIError], code: int, message: str, status: str) -> genai_errors.APIError:
    return cls(code, {"error": {"code": code, "message": message, "status": status}})


async def test_provider_falls_through_models(fake_client) -> None:
    models = fake_client({
        "model-a": api_error(genai_errors.ClientError, 429, "Quota exceeded", "RESOURCE_EXHAUSTED"),
        "model-b": api_error(genai_errors.ServerError, 503, "Overloaded", "UNAVAILABLE"),
        "model-c": '```json\n{"score": 90, "rationale": "ok"}\n```',
    })
    data, model = await generate_json(system="sys", parts=["prompt"], schema={"type": "object"})
    assert data == {"score": 90, "rationale": "ok"}
    assert model == "model-c"
    assert models.calls == ["model-a", "model-b", "model-c"]
    config = models.configs[0]
    assert config.response_mime_type == "application/json"
    assert config.system_instruction == "sys"
    assert config.temperature == pytest.approx(0.2)


@pytest.mark.parametrize(
    "first_failure",
    [
        api_error(genai_errors.ClientError, 404, "models/model-a is not found", "NOT_FOUND"),
        asyncio.TimeoutError(),
        ConnectionResetError("reset"),
        RuntimeError("неожиданная ошибка SDK"),
        "это не JSON",
        "",
        "[1, 2, 3]",
    ],
)
async def test_provider_next_model_on_failure(fake_client, first_failure) -> None:
    models = fake_client({"model-a": first_failure, "model-b": '{"ok": true}', "model-c": '{"ok": false}'})
    data, model = await generate_json(system="s", parts=["p"], schema={"type": "object"})
    assert (data, model) == ({"ok": True}, "model-b")
    assert models.calls == ["model-a", "model-b"]


async def test_provider_all_models_fail(fake_client) -> None:
    quota = api_error(genai_errors.ClientError, 429, "Quota exceeded", "RESOURCE_EXHAUSTED")
    models = fake_client({"model-a": quota, "model-b": asyncio.TimeoutError(), "model-c": "not json"})
    with pytest.raises(AIUnavailable):
        await generate_json(system="s", parts=["p"], schema={"type": "object"})
    assert models.calls == ["model-a", "model-b", "model-c"]


async def test_provider_bad_key_stops_immediately(fake_client, caplog) -> None:
    models = fake_client({
        "model-a": api_error(genai_errors.ClientError, 400, "API key not valid: test-key-123", "INVALID_ARGUMENT"),
        "model-b": '{"ok": true}',
        "model-c": '{"ok": true}',
    })
    with pytest.raises(AIUnavailable):
        await generate_json(system="s", parts=["p"], schema={"type": "object"})
    assert models.calls == ["model-a"]  # от модели не зависит — дальше не перебираем
    assert "test-key-123" not in caplog.text  # ключ API не попадает в лог


async def test_evaluate_with_fake_client_end_to_end(fake_client) -> None:
    fake_client({
        "model-a": api_error(genai_errors.ClientError, 429, "Quota exceeded", "RESOURCE_EXHAUSTED"),
        "model-b": '{"score": 104.6, "rationale": "Факт больше плана.", "completeness": "exceeded"}',
        "model-c": '{"score": 1}',
    })
    task, sub = make_pair()
    result = await evaluate_submission(task, sub)
    assert (result.score, result.source, result.model) == (105, "ai", "model-b")


async def test_evaluate_with_all_models_exhausted(fake_client) -> None:
    quota = api_error(genai_errors.ClientError, 429, "Quota exceeded", "RESOURCE_EXHAUSTED")
    fake_client({"model-a": quota, "model-b": quota, "model-c": quota})
    task, sub = make_pair(plan_value=100, fact_value=100, late_days=2.5)
    result = await evaluate_submission(task, sub)
    assert (result.score, result.source) == (95, "rules")
    assert result.rationale.startswith("Расчёт по правилам (AI недоступен)")


# --- evidence: файлы-подтверждения (фейковый бот, без сети) -----------------------------------------


class FakeBot:
    """bot.download(file_id) -> BytesIO из словаря; неизвестный file_id — ошибка сети."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.downloads: list[str] = []

    async def download(self, file_id: str, timeout: int = 30) -> BytesIO:
        self.downloads.append(file_id)
        if file_id not in self.files:
            raise ConnectionError("network down")
        return BytesIO(self.files[file_id])


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Договор", "Статус"])
    ws.append(["№ 1", "проверен"])
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def attachment(file_id: str, name: str | None, kind: AttachmentKind = AttachmentKind.DOCUMENT,
               mime: str | None = None, size: int | None = None) -> Attachment:
    return Attachment(kind=kind, file_id=file_id, file_name=name, mime_type=mime, file_size=size)


async def test_collect_evidence_formats() -> None:
    bot = FakeBot({
        "txt": "Проверено 110 договоров".encode("cp1251"),
        "xlsx": _xlsx_bytes(),
        "pdf": b"%PDF-1.4 fake",
        "photo": b"\xff\xd8\xff fake jpeg",
        "video": b"video",
        "doc": b"old word",
    })
    items = await collect_evidence(bot, [
        attachment("txt", "итог.txt", mime="text/plain"),
        attachment("xlsx", "Analysis.xlsx"),
        attachment("pdf", "акт.pdf", mime="application/pdf"),
        attachment("photo", None, kind=AttachmentKind.PHOTO),
        attachment("video", "запись.mp4", kind=AttachmentKind.VIDEO),
        attachment("doc", "старый.doc", mime="application/msword"),
        attachment("huge", "большой.pdf", mime="application/pdf", size=50 * 1024 * 1024),
        attachment("missing", "нет.txt", mime="text/plain"),
    ])
    kinds = [(item.name, item.kind) for item in items]
    assert kinds == [
        ("итог.txt", "text"),
        ("Analysis.xlsx", "text"),
        ("акт.pdf", "pdf"),
        ("Фото 4", "image"),
        ("запись.mp4", "skipped"),
        ("старый.doc", "skipped"),
        ("большой.pdf", "skipped"),
        ("нет.txt", "skipped"),
    ]
    assert items[0].text == "Проверено 110 договоров"
    assert "Договор | Статус" in items[1].text and "проверен" in items[1].text
    assert items[2].data == b"%PDF-1.4 fake" and items[3].mime_type == "image/jpeg"
    assert all(item.note for item in items if item.kind == "skipped")
    assert "video" not in bot.downloads and "doc" not in bot.downloads and "huge" not in bot.downloads

    parts = evidence_to_parts(items)
    assert sum(isinstance(part, genai_types.Part) for part in parts) == 2  # PDF и фото — байтами
    assert any(isinstance(part, str) and "Analysis.xlsx (Excel)" in part for part in parts)
    assert any(isinstance(part, str) and "не передан на анализ" in part for part in parts)


async def test_collect_evidence_disabled_by_settings(set_env) -> None:
    set_env(AI_READ_FILES="false")
    bot = FakeBot({"txt": b"text"})
    items = await collect_evidence(bot, [attachment("txt", "a.txt", mime="text/plain")])
    assert [item.kind for item in items] == ["skipped"]
    assert bot.downloads == []


async def test_collect_evidence_never_raises() -> None:
    class BrokenBot:
        async def download(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("boom")

    items = await collect_evidence(BrokenBot(), [attachment("x", "a.pdf", mime="application/pdf")])
    assert [item.kind for item in items] == ["skipped"]
