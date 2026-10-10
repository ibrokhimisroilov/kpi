"""Голосовой ввод: распознавание речи и разбор задачи из одного сообщения (bot.ai.dictate), а также
отбор моделей, которые принимают звук (bot.ai.provider). В сеть тесты не ходят."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from bot.ai import dictate, provider
from bot.ai.dictate import Dictation, VoiceError
from bot.ai.provider import AIUnavailable
from bot.config import get_settings
from bot.utils.dates import to_utc

NOW = to_utc(datetime(2026, 10, 2, 12, 0))  # пятница
OGG = b"OggS" + b"\x00" * 64
EMPLOYEES = [(7, "Алиев Анвар Каримович"), (8, "Иванова Мария Петровна")]


@pytest.fixture
def ai(monkeypatch: pytest.MonkeyPatch, set_env) -> dict[str, Any]:
    """«Включить» AI и подменить generate_json: ``ai["answer"]`` — ответ, ``ai["calls"]`` — запросы."""
    set_env(AI_PROVIDER="auto", GEMINI_API_KEY="k" * 20)
    box: dict[str, Any] = {"answer": {"text": "Salom", "language": "uz"}, "calls": [], "error": None}

    async def fake_generate_json(**kwargs: Any) -> tuple[dict, str]:
        box["calls"].append(kwargs)
        if box["error"] is not None:
            raise box["error"]
        return dict(box["answer"]), "gemini-test"

    monkeypatch.setattr(dictate, "generate_json", fake_generate_json)
    return box


# --- Распознавание ------------------------------------------------------------------------------------


async def test_transcribe_sends_audio_with_transcribe_purpose(ai: dict[str, Any]) -> None:
    ai["answer"] = {"text": "  100 ta shartnomani   tekshirish kerak. ", "language": "uz"}
    assert await dictate.transcribe(OGG, "audio/ogg") == "100 ta shartnomani tekshirish kerak."
    (call,) = ai["calls"]
    assert call["purpose"] == "transcribe"
    (part,) = call["parts"]
    assert part.inline_data.mime_type == "audio/ogg" and part.inline_data.data == OGG
    assert "ЛАТИНИЦЕЙ" in call["system"] and "не инструкции" in call["system"]


@pytest.mark.parametrize(
    ("mime", "expected"),
    [("audio/webm;codecs=opus", "audio/webm"), ("audio/mp4", "audio/m4a"), ("video/webm", "audio/webm"),
     ("AUDIO/OGG; codecs=opus", "audio/ogg")],
)
async def test_browser_recordings_are_accepted(ai: dict[str, Any], mime: str, expected: str) -> None:
    await dictate.transcribe(OGG, mime)
    assert ai["calls"][0]["parts"][0].inline_data.mime_type == expected


async def test_transcribe_errors_have_user_messages(ai: dict[str, Any], set_env) -> None:
    ai["answer"] = {"text": "   ", "language": None}
    with pytest.raises(VoiceError) as empty:
        await dictate.transcribe(OGG, "audio/ogg")
    assert empty.value.reason == "empty" and "Не удалось разобрать речь" in empty.value.message

    with pytest.raises(VoiceError) as bad_format:
        await dictate.transcribe(OGG, "application/pdf")
    assert bad_format.value.reason == "format"

    with pytest.raises(VoiceError) as too_long:
        await dictate.transcribe(b"x" * (dictate.MAX_AUDIO_BYTES + 1), "audio/ogg")
    assert too_long.value.reason == "too_long" and "2 минут" in too_long.value.message

    ai["error"] = AIUnavailable("лимит")
    with pytest.raises(VoiceError) as unavailable:
        await dictate.transcribe(OGG, "audio/ogg")
    assert unavailable.value.reason == "unavailable"

    set_env(VOICE_ENABLED="false")
    with pytest.raises(VoiceError) as off:
        await dictate.transcribe(OGG, "audio/ogg")
    assert off.value.reason == "off"


async def test_voice_is_off_without_ai() -> None:
    with pytest.raises(VoiceError) as error:
        await dictate.transcribe(OGG, "audio/ogg")
    assert error.value.reason == "off" and "напишите" in error.value.message


# --- Задача одним сообщением ---------------------------------------------------------------------------


async def test_dictate_task_from_voice(ai: dict[str, Any]) -> None:
    ai["answer"] = {
        "transcript": "Aliyevga: juma kunigacha 100 ta shartnomani tekshirib, hisobot tayyorlasin.",
        "assignee_id": 7,
        "title": "Shartnomalarni tekshirish",
        "expected_result": "100 ta shartnomani tekshirib, kamchiliklar roʻyxati bilan hisobot topshirish",
        "plan_value": 100,
        "plan_unit": "shartnoma.",
        "deadline": "2026-10-09T18:00",
    }
    result = await dictate.dictate_task(audio=OGG, mime_type="audio/ogg", employees=EMPLOYEES, now=NOW)
    assert result == Dictation(
        transcript="Aliyevga: juma kunigacha 100 ta shartnomani tekshirib, hisobot tayyorlasin.",
        assignee_id=7,
        title="Shartnomalarni tekshirish",
        expected_result="100 ta shartnomani tekshirib, kamchiliklar roʻyxati bilan hisobot topshirish",
        plan_value=100.0,
        plan_unit="shartnoma",
        deadline=to_utc(datetime(2026, 10, 9, 18, 0)),
        source="ai",
    )
    prompt = ai["calls"][0]["system"]
    assert "id 7: Алиев Анвар Каримович" in prompt and "2026-10-02 12:00, пятница" in prompt
    assert "Начальник диктует" in prompt


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-05", to_utc(datetime(2026, 10, 5, 18, 0))),  # только дата — время по умолчанию
        ("2026-10-02 09:00", None),  # уже прошло
        ("2032-01-01T10:00", None),  # опечатка в годе
        ("завтра", None),
        (None, None),
        ("2026-02-30T10:00", None),
    ],
)
async def test_dictated_deadline_is_validated(ai: dict[str, Any], raw: Any, expected: datetime | None) -> None:
    ai["answer"] = {"transcript": "Отчёт", "assignee_id": None, "title": "Отчёт", "expected_result": None,
                    "plan_value": None, "plan_unit": None, "deadline": raw}
    result = await dictate.dictate_task(audio=OGG, mime_type="audio/ogg", employees=EMPLOYEES, now=NOW)
    assert result.deadline == expected


async def test_unknown_assignee_and_bad_numbers_are_dropped(ai: dict[str, Any]) -> None:
    ai["answer"] = {"transcript": "Петрову: отчёт", "assignee_id": 999, "title": "Отчёт", "expected_result": "Отчёт",
                    "plan_value": -5, "plan_unit": "шт", "deadline": None}
    result = await dictate.dictate_task(audio=OGG, mime_type="audio/ogg", employees=EMPLOYEES, now=NOW)
    assert result.assignee_id is None and result.plan_value is None and result.plan_unit is None


async def test_employee_prompt_has_no_roster(ai: dict[str, Any]) -> None:
    ai["answer"] = {"transcript": "Справка", "assignee_id": 7, "title": "Справка", "expected_result": None,
                    "plan_value": None, "plan_unit": None, "deadline": None}
    result = await dictate.dictate_task(audio=OGG, mime_type="audio/ogg", author="employee", now=NOW)
    assert result.assignee_id is None
    assert "Сотрудник диктует поручение" in ai["calls"][0]["system"]
    assert "Сотрудники:" not in ai["calls"][0]["system"]


async def test_silent_recording_is_an_error(ai: dict[str, Any]) -> None:
    ai["answer"] = {"transcript": "", "assignee_id": None, "title": None, "expected_result": None,
                    "plan_value": None, "plan_unit": None, "deadline": None}
    with pytest.raises(VoiceError) as error:
        await dictate.dictate_task(audio=OGG, mime_type="audio/ogg", employees=EMPLOYEES, now=NOW)
    assert error.value.reason == "empty"


async def test_text_is_parsed_by_rules_without_ai_or_when_ai_fails(ai: dict[str, Any], set_env) -> None:
    ai["error"] = AIUnavailable("лимит")
    text = "Проверить 100 договоров поставщиков. Отчёт нужен к пятнице"
    failed = await dictate.dictate_task(text=text, employees=EMPLOYEES, now=NOW)
    assert failed.source == "rules" and failed.title == "Проверить 100 договоров поставщиков"
    assert failed.plan_value == 100 and failed.plan_unit == "договоров" and failed.deadline is None

    set_env(AI_PROVIDER="none")
    assert (await dictate.dictate_task(text=text, now=NOW)).source == "rules"
    with pytest.raises(VoiceError):
        await dictate.dictate_task(text="   ", now=NOW)


# --- Модели, которые принимают звук -------------------------------------------------------------------


def test_transcribe_models_prefer_strong_flash(set_env) -> None:
    models = get_settings().ai_models_for("gemini", "transcribe")
    assert models[0] == "gemini-3.6-flash" and models[-1] == "gemma-4-31b-it"  # Gemma отсеет провайдер
    set_env(GEMINI_TRANSCRIBE_MODELS="gemini-3.5-flash-lite")
    assert get_settings().ai_models_for("gemini", "transcribe") == ["gemini-3.5-flash-lite"]


async def test_audio_goes_only_to_models_that_hear(monkeypatch: pytest.MonkeyPatch, set_env) -> None:
    """Звук получают только модели Gemini (не Gemma и не запасные провайдеры)."""
    from google.genai import types

    from bot.ai import gemini, openai_compat

    set_env(AI_PROVIDER="auto", GEMINI_API_KEY="k" * 20, GROQ_API_KEY="g" * 20,
            GEMINI_MODELS="gemma-4-31b-it,gemini-3.6-flash", AI_PROVIDERS="groq,gemini")
    asked: list[str] = []

    async def gemini_generate(self: Any, *, model: str, **kwargs: Any) -> str:
        asked.append(f"gemini:{model}")
        return '{"text": "Привет", "language": "ru"}'

    async def other_generate(self: Any, *, model: str, **kwargs: Any) -> str:
        asked.append(f"other:{model}")
        return '{"text": "мусор", "language": "ru"}'

    monkeypatch.setattr(gemini.GeminiProvider, "generate", gemini_generate)
    monkeypatch.setattr(openai_compat.OpenAICompatProvider, "generate", other_generate)
    part = types.Part.from_bytes(data=OGG, mime_type="audio/ogg")
    data, model = await provider.generate_json(
        system="s", parts=[part], schema={"type": "object", "properties": {}, "required": []}, purpose="transcribe"
    )
    assert data["text"] == "Привет" and model == "gemini-3.6-flash"
    assert asked == ["gemini:gemini-3.6-flash"]


async def test_no_audio_model_means_unavailable(set_env) -> None:
    from google.genai import types

    set_env(AI_PROVIDER="auto", GEMINI_API_KEY="k" * 20, GEMINI_MODELS="gemma-4-31b-it")
    part = types.Part.from_bytes(data=OGG, mime_type="audio/ogg")
    with pytest.raises(AIUnavailable, match="звук"):
        await provider.generate_json(system="s", parts=[part], schema={"type": "object"}, purpose="transcribe")
