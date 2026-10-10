"""Голосовой ввод в чате (SPEC.md §13): ответ на вопрос диалога голосом и задача одним голосовым сообщением.

Бот целиком на фейковом Telegram API (tests/e2e/fakebot.py). Распознавание подменено: фикстура ``ear``
включает его и отдаёт заранее заданный текст — в сеть тесты не ходят.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from bot.ai import dictate
from bot.ai.provider import AIUnavailable
from bot.db.models import Priority, Task, TaskSource, TaskStatus, User
from bot.ui.texts import BTN_NEW_TASK, BTN_PROPOSE
from bot.utils.dates import to_local, utcnow

from .fakebot import MANAGER_TG_ID, BotHarness

pytestmark = pytest.mark.asyncio

MGR = MANAGER_TG_ID
EMP = 2001
EMP2 = 2002
NEWBIE = 5001


@dataclass
class Ear:
    """Подмена распознавания: ``heard`` — дословный текст, ``task`` — поля задачи из сообщения."""

    heard: str = "Анализ договоров поставщиков"
    task: dict[str, Any] = field(default_factory=dict)
    error: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def answer(self, schema: dict) -> dict[str, Any]:
        if "transcript" not in schema["properties"]:
            return {"text": self.heard, "language": "ru"}
        fields = {"assignee_id": None, "title": None, "expected_result": None, "plan_value": None,
                  "plan_unit": None, "deadline": None}
        return {"transcript": self.heard, **fields, **self.task}


@pytest.fixture
def ear(monkeypatch: pytest.MonkeyPatch) -> Ear:
    fake = Ear()

    async def fake_generate_json(**kwargs: Any) -> tuple[dict, str]:
        fake.calls.append(kwargs)
        if fake.error is not None:
            raise fake.error
        return fake.answer(kwargs["schema"]), "gemini-test"

    monkeypatch.setattr(dictate, "ai_available", lambda: True)
    monkeypatch.setattr(dictate, "generate_json", fake_generate_json)
    return fake


def in_days(days: int, hour: int = 18) -> str:
    """Срок через ``days`` дней по местному времени — в том виде, в каком его возвращает AI."""
    local = to_local(utcnow() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return local.strftime("%Y-%m-%dT%H:%M")


async def _team(h: BotHarness) -> tuple[User, User, User]:
    mgr = await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    emp = await h.seed_user(EMP, "Алиев Анвар Каримович", position="Юрист")
    emp2 = await h.seed_user(EMP2, "Иванова Мария Петровна", position="Экономист")
    for tg_id in (MGR, EMP, EMP2):
        await h.send_command(tg_id, "start")
    return mgr, emp, emp2


async def _open_task(h: BotHarness, mgr: User, emp: User, title: str = "Анализ договоров") -> int:
    async with h.db() as s:
        task = Task(
            title=title, expected_result="Проверить 100 договоров", plan_value=100.0, plan_unit="договоров",
            deadline=utcnow() + timedelta(days=3), priority=Priority.MEDIUM, weight=20, status=TaskStatus.ACTIVE,
            source=TaskSource.MANAGER, assignee_id=emp.id, created_by_id=mgr.id, manager_id=mgr.id,
        )
        s.add(task)
        await s.commit()
        return task.id


# --- Начальник: задача одним сообщением -------------------------------------------------------------


async def test_manager_dictates_whole_task(app: BotHarness, ear: Ear) -> None:
    h = app
    _mgr, emp, _ = await _team(h)
    deadline_text = in_days(3)
    ear.heard = "Aliyevga: juma kunigacha 100 ta shartnomani tekshirib, hisobot tayyorlasin"
    ear.task = {
        "assignee_id": emp.id, "title": "Shartnomalarni tekshirish",
        "expected_result": "100 ta shartnomani tekshirib, hisobot topshirish",
        "plan_value": 100, "plan_unit": "shartnoma", "deadline": deadline_text,
    }

    await h.send_voice(MGR)

    assert f"🎤 {ear.heard}" in [m.content for m in h.messages(MGR)]
    step = h.last_text(MGR)
    assert "Понял так" in step and "Алиев Анвар Каримович" in step and "Shartnomalarni tekshirish" in step
    assert "100 shartnoma" in step and "приоритет" in step  # дальше — только приоритет и вес
    assert "id %s: Алиев Анвар Каримович" % emp.id in ear.calls[0]["system"]
    assert ear.calls[0]["parts"][1].inline_data.mime_type == "audio/ogg"

    await h.press_button(MGR, "Средний")
    await h.press_button(MGR, "20 %")
    summary = h.last_text(MGR)
    assert "Shartnomalarni tekshirish" in summary and "Алиев Анвар Каримович" in summary
    log = await h.press_button(MGR, "Создать")

    task = await h.scalar(select(Task))
    assert task.title == "Shartnomalarni tekshirish" and task.assignee_id == emp.id
    assert task.expected_result == "100 ta shartnomani tekshirib, hisobot topshirish"
    assert task.description == ear.heard  # исходные слова начальника
    assert (task.plan_value, task.plan_unit, task.weight, task.priority) == (100, "shartnoma", 20, Priority.MEDIUM)
    assert to_local(task.deadline).strftime("%Y-%m-%dT%H:%M") == deadline_text
    assert "Вам поставлена новая задача" in log.to(EMP).text
    assert await h.get_state(MGR) is None


async def test_manager_voice_asks_only_for_what_is_missing(app: BotHarness, ear: Ear) -> None:
    """Сотрудник не назван и срока нет: мастер спрашивает их, остальное уже заполнено."""
    h = app
    await _team(h)
    ear.heard = "Подготовить отчёт по продажам"
    ear.task = {"title": "Отчёт по продажам", "expected_result": "Подготовить отчёт по продажам за октябрь"}

    await h.send_voice(MGR)
    assert "Выберите сотрудника" in h.last_text(MGR)
    await h.press_button(MGR, "Иванова")
    assert "срок" in h.last_text(MGR).lower()
    await h.press_button(MGR, h.buttons(MGR)[0])
    await h.press_button(MGR, "Высокий")
    await h.press_button(MGR, "10 %")
    assert "Отчёт по продажам" in h.last_text(MGR) and "Иванова Мария Петровна" in h.last_text(MGR)
    await h.press_button(MGR, "Создать")
    task = await h.scalar(select(Task))
    assert task.title == "Отчёт по продажам" and task.priority == Priority.HIGH and task.weight == 10


async def test_voice_on_first_step_of_new_task_is_whole_task(app: BotHarness, ear: Ear) -> None:
    h = app
    _mgr, emp, _ = await _team(h)
    await h.press_menu(MGR, BTN_NEW_TASK)
    assert await h.get_state(MGR) == "CreateTaskSG:assignee"
    ear.task = {"assignee_id": emp.id, "title": "Анализ договоров"}

    await h.send_voice(MGR)

    assert await h.get_state(MGR) == "CreateTaskSG:result_raw"  # сотрудник и название есть — спрашиваем результат
    data = await h.get_data(MGR)
    assert data["assignee_id"] == emp.id and data["title"] == "Анализ договоров" and data["dictated"] is True


async def test_manager_answers_dialog_question_by_voice(app: BotHarness, ear: Ear) -> None:
    h = app
    await _team(h)
    await h.press_menu(MGR, BTN_NEW_TASK)
    await h.press_button(MGR, "Алиев")
    assert await h.get_state(MGR) == "CreateTaskSG:title"
    ear.heard = "Анализ договоров поставщиков"

    await h.send_voice(MGR)

    assert "🎤 Анализ договоров поставщиков" in [m.content for m in h.messages(MGR)]
    assert await h.get_state(MGR) == "CreateTaskSG:result_raw"
    assert (await h.get_data(MGR))["title"] == "Анализ договоров поставщиков"
    assert "Задача: Анализ договоров поставщиков" in h.last_text(MGR)


async def test_unrecognised_voice_keeps_the_dialog_step(app: BotHarness, ear: Ear) -> None:
    h = app
    await _team(h)
    await h.press_menu(MGR, BTN_NEW_TASK)
    await h.press_button(MGR, "Алиев")

    ear.heard = "  "
    await h.send_voice(MGR)
    assert "Не удалось разобрать речь" in h.last_text(MGR)
    ear.error = AIUnavailable("лимит")
    await h.send_voice(MGR)
    assert "не получилось распознать" in h.last_text(MGR)
    calls = len(ear.calls)
    await h.send_voice(MGR, duration=600)
    assert "слишком длинная" in h.last_text(MGR) and len(ear.calls) == calls  # длинную запись даже не слушаем

    assert await h.get_state(MGR) == "CreateTaskSG:title"
    ear.error, ear.heard = None, "Анализ договоров"
    await h.send_voice(MGR)
    assert await h.get_state(MGR) == "CreateTaskSG:result_raw"


async def test_voice_without_ai_asks_to_type(app: BotHarness) -> None:
    h = app
    await _team(h)
    await h.send_voice(MGR)
    assert "напишите, пожалуйста, текстом" in h.last_text(MGR)
    assert await h.get_state(MGR) is None


# --- Сотрудник -------------------------------------------------------------------------------------


async def test_employee_voice_becomes_result_of_the_only_open_task(app: BotHarness, ear: Ear) -> None:
    h = app
    mgr, emp, _ = await _team(h)
    task_id = await _open_task(h, mgr, emp)
    ear.heard = "Проверил 95 договоров, в 7 нашёл нарушения"

    await h.send_voice(EMP)
    assert "Что это?" in h.last_text(EMP)
    assert h.buttons(EMP)[:2] == ["➕ Новое поручение", "✅ Результат по задаче"]
    await h.press_button(EMP, "Результат по задаче")

    assert await h.get_state(EMP) == "SubmitSG:result"
    data = await h.get_data(EMP)
    assert data["task_id"] == task_id and data["fact"] == ear.heard
    assert "Какой получен результат" in h.last_text(EMP)


async def test_employee_picks_task_for_dictated_result(app: BotHarness, ear: Ear) -> None:
    h = app
    mgr, emp, _ = await _team(h)
    await _open_task(h, mgr, emp, "Анализ договоров")
    second = await _open_task(h, mgr, emp, "Отчёт по закупкам")
    ear.heard = "Отчёт готов, отправил в бухгалтерию"

    await h.send_voice(EMP)
    await h.press_button(EMP, "Результат по задаче")
    assert "По какой задаче этот результат?" in h.last_text(EMP)
    await h.press_button(EMP, "Отчёт по закупкам")

    data = await h.get_data(EMP)
    assert await h.get_state(EMP) == "SubmitSG:result" and data["task_id"] == second and data["fact"] == ear.heard


async def test_employee_voice_becomes_proposal(app: BotHarness, ear: Ear) -> None:
    h = app
    mgr, emp, _ = await _team(h)
    await _open_task(h, mgr, emp)
    deadline_text = in_days(2)
    ear.heard = "Справка по пяти договорам к среде"
    ear.task = {"title": "Справка по договорам", "expected_result": "Подготовить справку по 5 договорам",
                "plan_value": 5, "plan_unit": "договоров", "deadline": deadline_text}

    await h.send_voice(EMP)
    await h.press_button(EMP, "Новое поручение")
    summary = h.last_text(EMP)
    assert "Проверьте поручение перед отправкой" in summary and "Справка по договорам" in summary
    assert "5 договоров" in summary
    log = await h.press_button(EMP, "Отправить начальнику")

    proposal = await h.scalar(select(Task).where(Task.status == TaskStatus.PROPOSED))
    assert proposal.title == "Справка по договорам" and proposal.assignee_id == emp.id
    assert proposal.plan_value == 5 and to_local(proposal.deadline).strftime("%Y-%m-%dT%H:%M") == deadline_text
    assert "Сотрудник внёс поручение" in log.to(MGR).text


async def test_employee_without_open_tasks_goes_straight_to_proposal(app: BotHarness, ear: Ear) -> None:
    h = app
    await _team(h)
    ear.heard = "Подготовить справку"
    ear.task = {"title": "Справка"}

    await h.send_voice(EMP)

    assert await h.get_state(EMP) == "ProposeTaskSG:result"  # название есть — спрашиваем результат
    assert (await h.get_data(EMP))["title"] == "Справка"


async def test_voice_on_first_step_of_proposal(app: BotHarness, ear: Ear) -> None:
    h = app
    mgr, emp, _ = await _team(h)
    await _open_task(h, mgr, emp)
    await h.press_menu(EMP, BTN_PROPOSE)
    ear.task = {"title": "Справка", "expected_result": "Подготовить справку по 5 договорам", "plan_value": 5,
                "plan_unit": "договоров"}

    await h.send_voice(EMP)

    assert await h.get_state(EMP) == "ProposeTaskSG:deadline"  # вопроса «что это?» нет: кнопка уже нажата
    assert "Какой срок выполнения" in h.last_text(EMP)


async def test_registration_is_typed_not_dictated(app: BotHarness, ear: Ear) -> None:
    h = app
    await h.send_command(NEWBIE, "start", first_name="Новичок")
    assert await h.get_state(NEWBIE) == "RegistrationSG:full_name"
    await h.send_voice(NEWBIE)
    assert ear.calls == []
    assert await h.get_state(NEWBIE) == "RegistrationSG:full_name"
