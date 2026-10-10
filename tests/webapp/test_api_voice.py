"""Голосовой ввод в приложении: ``POST /api/voice`` (docs/MINIAPP_SPEC.md §8.10) — запись телом запроса,
ответ — распознанный текст, а с ``?mode=task`` ещё и поля формы. Распознавание подменено, в сеть тесты не ходят."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from bot.ai import dictate
from bot.ai.provider import AIUnavailable
from bot.webapp import api

from .conftest import EMP, MGR, MiniApp

WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 200


@dataclass
class Ear:
    heard: str = "Проверено 95 договоров"
    task: dict[str, Any] = field(default_factory=dict)
    error: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def ear(monkeypatch: pytest.MonkeyPatch) -> Ear:
    fake = Ear()

    async def fake_generate_json(**kwargs: Any) -> tuple[dict, str]:
        fake.calls.append(kwargs)
        if fake.error is not None:
            raise fake.error
        if "transcript" not in kwargs["schema"]["properties"]:
            return {"text": fake.heard, "language": "ru"}, "gemini-test"
        empty = {"assignee_id": None, "title": None, "expected_result": None, "plan_value": None,
                 "plan_unit": None, "deadline": None}
        return {"transcript": fake.heard, **empty, **fake.task}, "gemini-test"

    monkeypatch.setattr(dictate, "ai_available", lambda: True)
    monkeypatch.setattr(dictate, "generate_json", fake_generate_json)
    return fake


async def record(ma: MiniApp, *, as_: int, body: bytes = WEBM, mime: str = "audio/webm;codecs=opus", **params: Any) -> Any:
    return await ma.post("/api/voice", as_=as_, data=body, headers={"Content-Type": mime}, params=params or None)


async def test_field_dictation_returns_text(ma: MiniApp, ear: Ear) -> None:
    await ma.seed_team(2)
    resp = await record(ma, as_=EMP)
    assert resp.status == 200, resp.data
    assert resp.data == {"text": "Проверено 95 договоров"}
    (call,) = ear.calls
    audio = call["parts"][0].inline_data
    assert audio.mime_type == "audio/webm" and audio.data == WEBM and call["purpose"] == "transcribe"


async def test_iphone_recording_is_accepted(ma: MiniApp, ear: Ear) -> None:
    await ma.seed_team(2)
    resp = await record(ma, as_=EMP, mime="audio/mp4")
    assert resp.status == 200 and ear.calls[0]["parts"][0].inline_data.mime_type == "audio/m4a"


async def test_manager_dictates_whole_task(ma: MiniApp, ear: Ear, frozen: Any) -> None:
    _, (emp, _) = await ma.seed_team(2)
    ear.heard = "Иванову до пятницы проверить 100 договоров и сдать отчёт"
    ear.task = {"assignee_id": emp.id, "title": "Проверка договоров", "expected_result": "Проверить 100 договоров и сдать отчёт",
                "plan_value": 100, "plan_unit": "договоров", "deadline": "2026-10-09T15:30"}

    resp = await record(ma, as_=MGR, mode="task")

    assert resp.status == 200, resp.data
    assert resp["text"] == ear.heard
    assert resp["task"] == {
        "assignee_id": emp.id, "title": "Проверка договоров", "expected_result": "Проверить 100 договоров и сдать отчёт",
        "plan_value": 100.0, "plan_unit": "договоров", "deadline_date": "2026-10-09", "deadline_time": "15:30",
        "source": "ai",
    }
    prompt = ear.calls[0]["system"]
    assert f"id {emp.id}: {emp.full_name}" in prompt and "2026-10-02 12:00, пятница" in prompt


async def test_employee_dictation_has_no_assignee(ma: MiniApp, ear: Ear, frozen: Any) -> None:
    _, (emp, _) = await ma.seed_team(2)
    ear.task = {"assignee_id": emp.id, "title": "Справка", "deadline": None}
    resp = await record(ma, as_=EMP, mode="task")
    assert resp.status == 200
    assert resp["task"]["assignee_id"] is None and resp["task"]["deadline_date"] is None
    assert "Сотрудники:" not in ear.calls[0]["system"]


async def test_unrecognised_speech_is_422_with_user_text(ma: MiniApp, ear: Ear) -> None:
    await ma.seed_team(2)
    ear.heard = "  "
    silent = await record(ma, as_=EMP)
    assert silent.status == 422 and silent.code == "voice_failed" and "Не удалось разобрать речь" in silent.error
    ear.error = AIUnavailable("лимит")
    failed = await record(ma, as_=EMP, mode="task")
    assert failed.status == 422 and "не получилось распознать" in failed.error
    wrong = await record(ma, as_=EMP, mime="application/pdf")
    assert wrong.status == 422 and "Такую запись" in wrong.error


async def test_voice_without_ai_asks_to_type(ma: MiniApp) -> None:
    await ma.seed_team(2)
    resp = await record(ma, as_=EMP)
    assert resp.status == 422 and "текстом" in resp.error


async def test_bad_requests(ma: MiniApp, ear: Ear, monkeypatch: pytest.MonkeyPatch) -> None:
    await ma.seed_team(2)
    assert (await record(ma, as_=EMP, body=b"")).status == 400
    assert (await record(ma, as_=EMP, mode="song")).status == 400
    monkeypatch.setattr(api, "VOICE_MAX_BYTES", 100)
    big = await record(ma, as_=EMP)
    assert big.status == 413 and "слишком длинная" in big.error
    assert ear.calls == []


async def test_voice_is_rate_limited_per_user(ma: MiniApp, ear: Ear, monkeypatch: pytest.MonkeyPatch) -> None:
    await ma.seed_team(2)
    monkeypatch.setattr(api, "VOICE_LIMITS", ((2, 60.0),))
    assert (await record(ma, as_=EMP)).status == 200
    assert (await record(ma, as_=EMP)).status == 200
    third = await record(ma, as_=EMP)
    assert third.status == 429 and "Слишком много записей" in third.error
    assert (await record(ma, as_=MGR)).status == 200  # лимит — на человека


async def test_team_daily_limit_turns_voice_off(ma: MiniApp, ear: Ear, monkeypatch: pytest.MonkeyPatch) -> None:
    await ma.seed_team(2)
    monkeypatch.setattr(api, "VOICE_TEAM_PER_DAY", 1)
    assert (await record(ma, as_=EMP)).status == 200
    resp = await record(ma, as_=MGR)
    assert resp.status == 422 and "не распознаётся" in resp.error
    assert len(ear.calls) == 1


async def test_me_tells_if_voice_is_available(ma: MiniApp, ear: Ear, monkeypatch: pytest.MonkeyPatch) -> None:
    await ma.seed_team(2)
    config = (await ma.get("/api/me", as_=EMP))["config"]
    assert config["voice_enabled"] is True and config["voice_max_sec"] == 120
    monkeypatch.setattr(dictate, "ai_available", lambda: False)
    assert (await ma.get("/api/me", as_=EMP))["config"]["voice_enabled"] is False
