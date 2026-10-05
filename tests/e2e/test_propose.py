"""Сценарии «Сотрудник вносит устное поручение» и решение руководителя по нему (SPEC 7.4, ТЗ п. 2).

Сотрудник: «➕ Добавить поручение» → название → ожидаемый результат (подсказка AI или правил) →
плановое число (если нужно) → срок → сводка → «📤 Отправить руководителю». Все активные
руководители получают карточку с кнопками [✅ Подтвердить] [✏️ Изменить] [❌ Отклонить].

Руководитель: подтверждает (вес → приоритет), корректирует (название / результат / план / срок),
отклоняет (с причиной или без). Второй руководитель, нажавший кнопку по уже решённому
предложению, получает alert. «📥 Предложения» — очередь с пагинацией. Подтверждённое и
выполненное поручение попадает в KPI как «внесённое самостоятельно».

AI в тестах не ходит в сеть: по умолчанию AI выключен (правила), фикстура ``gemini`` подменяет
``generate_json`` в bot.ai.formulate заранее заданными ответами «Gemini».
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update

from bot.ai import evaluate as evaluate_module
from bot.ai import formulate as formulate_module
from bot.ai import provider as provider_module
from bot.ai.provider import AIUnavailable
from bot.config import get_settings
from bot.db.models import EventType, Priority, Role, Task, TaskEvent, TaskSource, TaskStatus, User, UserStatus
from bot.services import tasks as tasks_svc
from bot.services import users as users_svc
from bot.ui.callbacks import ListCB, TaskCB
from bot.ui.texts import BTN_MY_KPI, BTN_MY_TASKS, BTN_PROPOSALS, BTN_PROPOSE
from bot.utils.dateparse import iso_to_deadline
from bot.utils.dates import to_local, utcnow

from .fakebot import MANAGER_TG_ID, BotHarness

pytestmark = pytest.mark.asyncio

MGR = MANAGER_TG_ID          # Петрова Анна Сергеевна
MGR2 = 1002                  # Соколов Олег Петрович — второй руководитель
EMP = 2001                   # Иванов Иван Иванович — сотрудник, вносит поручения
EMP2 = 2002                  # Сидорова Мария Олеговна — ещё один сотрудник

TITLE = "Анализ договоров поставщиков"
RAW_RESULT = "проверить 100 договоров и сделать отчёт"

ALREADY = "Предложение уже обработано"
STALE = "Эта кнопка уже неактуальна"
NO_RIGHTS = "Недостаточно прав"
PROPOSAL_BUTTONS = ["✅ Подтвердить", "✏️ Изменить", "❌ Отклонить"]


# --- Подготовка ------------------------------------------------------------------------------------


async def team(h: BotHarness, *, second_manager: bool = True, second_employee: bool = False) -> None:
    """Руководитель(и) и сотрудник(и) уже в системе; у всех показано главное меню."""
    await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    people = [MGR]
    if second_manager:
        await h.seed_user(MGR2, "Соколов Олег Петрович", role="manager")
        people.append(MGR2)
    await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    people.append(EMP)
    if second_employee:
        await h.seed_user(EMP2, "Сидорова Мария Олеговна", position="Экономист")
        people.append(EMP2)
    for uid in people:
        await h.send_command(uid, "menu")


def tomorrow_deadline() -> datetime:
    """Срок кнопки «Завтра»: завтра в 18:00 по местному времени (naive UTC)."""
    return iso_to_deadline((to_local(utcnow()) + timedelta(days=1)).date().isoformat())


async def fill_draft(
    h: BotHarness,
    uid: int = EMP,
    *,
    title: str = TITLE,
    result: str = RAW_RESULT,
    choice: str = "Принять",
    deadline: str = "Завтра",
) -> None:
    """Сотрудник проходит диалог до сводки (результат с числом — шаг плана не нужен)."""
    await h.press_menu(uid, BTN_PROPOSE)
    await h.send_text(uid, title)
    await h.send_text(uid, result)
    await h.press_button(uid, choice)
    await h.press_button(uid, deadline)
    assert "Проверьте поручение перед отправкой" in h.last_text(uid)


async def propose(h: BotHarness, uid: int = EMP, **kwargs: Any) -> int:
    """Сотрудник вносит поручение целиком; возвращает номер задачи."""
    await fill_draft(h, uid, **kwargs)
    await h.press_button(uid, "Отправить руководителю")
    return await h.scalar(select(func.max(Task.id)))


async def seed_proposal(h: BotHarness, tg_id: int, title: str, *, deadline: datetime | None = None) -> int:
    """Поручение, внесённое сотрудником раньше (напрямую через сервис, без диалога)."""
    async with h.db() as session:
        employee = await users_svc.get_by_tg(session, tg_id)
        task = await tasks_svc.propose_task(
            session,
            employee=employee,
            title=title,
            expected_result=f"{title}: сдать отчёт",
            deadline=deadline or utcnow() + timedelta(days=3),
        )
        await session.commit()
        return task.id


async def events(h: BotHarness, task_id: int) -> list[TaskEvent]:
    return await h.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id))


async def task_count(h: BotHarness) -> int:
    return await h.scalar(select(func.count()).select_from(Task))


async def approve(h: BotHarness, uid: int = MGR, *, weight: str = "20 %", priority: str = "Средний") -> None:
    await h.press_button(uid, "Подтвердить")
    await h.press_button(uid, weight)
    await h.press_button(uid, priority)


async def move_deadline_to_past(h: BotHarness, task_id: int) -> None:
    """Прошло время: срок поручения остался в прошлом."""
    async with h.db() as session:
        await session.execute(
            update(Task).where(Task.id == task_id).values(deadline=utcnow() - timedelta(hours=2))
        )
        await session.commit()


# --- «Gemini» без сети -----------------------------------------------------------------------------


class FakeGemini:
    """Подмена generate_json: отдаёт заранее заданные ответы по очереди и запоминает запросы."""

    def __init__(self) -> None:
        self.answers: list[Any] = []
        self.prompts: list[str] = []

    def answer(self, expected_result: str, plan_value: float | None = None, plan_unit: str | None = None,
               note: str | None = None) -> None:
        self.answers.append(
            {"expected_result": expected_result, "plan_value": plan_value, "plan_unit": plan_unit, "note": note}
        )

    def fail(self, exc: BaseException | None = None) -> None:
        self.answers.append(exc or AIUnavailable("429: исчерпан бесплатный лимит"))

    async def __call__(self, *, system: str, parts: list, schema: dict, **_: Any) -> tuple[dict, str]:
        self.prompts.append(str(parts[0]))
        assert self.answers, "Gemini спросили больше раз, чем ожидал тест"
        outcome = self.answers.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome, "gemini-test"


@pytest.fixture
def gemini(app: BotHarness, monkeypatch: pytest.MonkeyPatch) -> FakeGemini:
    """AI включён (ключ задан), но вместо сети — FakeGemini."""
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODELS", "gemini-test")
    get_settings.cache_clear()
    fake = FakeGemini()
    monkeypatch.setattr(formulate_module, "_seen", OrderedDict())
    monkeypatch.setattr(formulate_module, "generate_json", fake)

    async def no_network(**_: Any) -> tuple[dict, str]:
        raise AIUnavailable("в тестах сети нет")

    monkeypatch.setattr(evaluate_module, "generate_json", no_network)
    monkeypatch.setattr(provider_module, "generate_json", no_network)
    return fake


# =================================================================================================
#  Сотрудник вносит поручение
# =================================================================================================


async def test_employee_proposes_task_and_every_active_manager_is_notified(app: BotHarness) -> None:
    """Иванову устно поручили проверить договоры. Он вносит поручение в бота (AI выключен —
    формулировку проверяют правила), отправляет руководителю — и оба активных руководителя
    получают карточку с кнопками решения. Заблокированный руководитель ничего не получает,
    а руководитель, который сам заблокировал бота, не мешает остальным."""
    h = app
    await team(h)
    await h.seed_user(1003, "Орлов Пётр Ильич", role="manager", status="blocked")
    await h.seed_user(1004, "Крылов Денис Андреевич", role="manager")
    h.api.blocked_chats.add(1004)

    log = await h.press_menu(EMP, BTN_PROPOSE)
    assert "Новое поручение" in log.text and "Шаг 1/3" in log.text
    assert h.buttons(EMP) == ["✖️ Отмена"]
    assert await h.get_state(EMP) == "ProposeTaskSG:title"

    log = await h.send_text(EMP, f"  {TITLE}  ")
    assert f"Задача: {TITLE}" in log.text and "Шаг 2/3" in log.text

    log = await h.send_text(EMP, RAW_RESULT)
    assert "⏳ Формулирую измеримый результат" in log.texts[0]
    suggestion = h.last_text(EMP)
    assert "AI сейчас недоступен — проверено по правилам" in suggestion
    assert "Проверить 100 договоров и сделать отчёт" in suggestion
    assert "📏 План: 100 договоров" in suggestion
    assert f"Вы написали: {RAW_RESULT}" in suggestion
    assert h.buttons(EMP) == [
        "✅ Принять", "🔁 Другой вариант", "✏️ Свой вариант", "📝 Оставить как написал", "✖️ Отмена",
    ]

    log = await h.press_button(EMP, "Принять")
    assert log.alert == "✅ Принято"
    assert "Шаг 3/3" in h.last_text(EMP) and "Какой срок" in h.last_text(EMP)
    assert any(button.startswith("Завтра") for button in h.buttons(EMP))

    await h.press_button(EMP, "Завтра")
    summary = h.last_text(EMP)
    assert "Проверьте поручение перед отправкой" in summary
    assert f"Задача: {TITLE}" in summary
    assert "Ожидаемый результат: Проверить 100 договоров и сделать отчёт" in summary
    assert "План: 100 договоров" in summary
    assert "Исполнитель: вы" in summary
    assert h.buttons(EMP) == ["📤 Отправить руководителю", "✏️ Изменить", "✖️ Отмена"]
    assert await task_count(h) == 0, "до отправки в БД ничего не пишется"

    log = await h.press_button(EMP, "Отправить руководителю")
    assert log.alert == "📤 Отправлено"
    assert "Поручение #1 отправлено руководителю на подтверждение" in h.last_text(EMP)
    assert "Внесена сотрудником" in h.last_text(EMP) and "Исполнитель:" not in h.last_text(EMP)
    assert h.buttons(EMP) == []
    assert await h.get_state(EMP) is None

    for manager in (MGR, MGR2):
        card = h.find_message(manager, "Сотрудник внёс поручение")
        assert TITLE in card.content and "Иванов И. И." in card.content
        assert "Внесена сотрудником (устное поручение)" in card.content
        assert "Статус: 📥 На подтверждении" in card.content
        assert card.button_texts == PROPOSAL_BUTTONS
    assert h.messages(1003) == []
    assert not log.to(1004).texts

    task = await h.get_task(1)
    employee = await h.get_user(EMP)
    assert task.status == TaskStatus.PROPOSED and task.source == TaskSource.EMPLOYEE
    assert task.assignee_id == task.created_by_id == employee.id and task.manager_id is None
    assert task.title == TITLE
    assert task.expected_result == "Проверить 100 договоров и сделать отчёт"
    assert (task.plan_value, task.plan_unit) == (100, "договоров")
    assert task.deadline == tomorrow_deadline()
    assert [event.type for event in await events(h, 1)] == [EventType.PROPOSED]


async def test_proposal_card_does_not_pretend_weight_and_priority_were_chosen(app: BotHarness) -> None:
    """Вес и приоритет поручению назначает руководитель при подтверждении. До этого в БД лежат
    временные значения (вес 10 %, средний приоритет), и карточка не должна выдавать их за
    выбор сотрудника: руководитель увидит «Вес: 10 %» и решит, что так предложил сотрудник."""
    h = app
    await team(h, second_manager=False)
    await propose(h)
    card = h.find_message(MGR, "Сотрудник внёс поручение").content
    assert "Вес: 10 %" not in card
    assert "Приоритет: 🟡 Средний" not in card
    assert "Вес: 10 %" not in h.find_message(EMP, "отправлено руководителю").content


async def test_ai_turns_vague_words_into_measurable_result(app: BotHarness, gemini: FakeGemini) -> None:
    """Сотрудник пишет расплывчато. «Gemini» предлагает измеримую формулировку с планом и советом;
    сотрудник просит другой вариант (модель видит прежний), принимает его — шаг «план» не нужен,
    число уже есть. Руководитель видит и формулировку AI, и исходные слова сотрудника."""
    h = app
    await team(h)
    gemini.answer("Проверить 100 договоров поставщиков и сдать отчёт", 100, "договоров",
                  note="Уточните форму отчёта")
    gemini.answer("Сдать реестр 100 проверенных договоров с перечнем нарушений", 100, "договоров")

    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, TITLE)
    await h.send_text(EMP, "посмотреть договоры поставщиков, их около сотни, нужен отчёт")
    text = h.last_text(EMP)
    assert "🤖 Предлагаю измеримую формулировку результата" in text
    assert "Проверить 100 договоров поставщиков и сдать отчёт" in text
    assert "📏 План: 100 договоров" in text and "💡 Уточните форму отчёта" in text
    assert "Вы написали: посмотреть договоры поставщиков" in text
    assert TITLE in gemini.prompts[0]

    log = await h.press_button(EMP, "Другой вариант")
    assert "⏳ Формулирую другой вариант" in log.texts[0]
    assert "Сдать реестр 100 проверенных договоров" in h.last_text(EMP)
    assert "«Проверить 100 договоров поставщиков и сдать отчёт»" in gemini.prompts[1]

    await h.press_button(EMP, "Принять")
    assert "Шаг 3/3" in h.last_text(EMP), "план уже известен — сразу срок"
    await h.press_button(EMP, "Завтра")
    await h.press_button(EMP, "Отправить руководителю")

    task = await h.get_task(1)
    assert task.expected_result == "Сдать реестр 100 проверенных договоров с перечнем нарушений"
    assert (task.plan_value, task.plan_unit) == (100, "договоров")
    assert task.description == "посмотреть договоры поставщиков, их около сотни, нужен отчёт"
    card = h.find_message(MGR, "Сотрудник внёс поручение").content
    assert "Сдать реестр 100 проверенных договоров" in card
    assert "Описание: посмотреть договоры поставщиков" in card


async def test_ai_retries_are_limited_and_repeated_variant_is_explained(
    app: BotHarness, gemini: FakeGemini
) -> None:
    """Модель упрямо возвращает одну и ту же формулировку: бот честно говорит об этом, а после
    трёх повторов предлагает принять вариант или написать свой — без новых запросов к AI."""
    h = app
    await team(h, second_manager=False)
    for _ in range(4):
        gemini.answer("Проверить 100 договоров", 100, "договоров")

    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, TITLE)
    await h.send_text(EMP, RAW_RESULT)
    for _ in range(3):
        await h.press_button(EMP, "Другой вариант")
        assert "AI предложил тот же вариант" in h.last_text(EMP)
    log = await h.press_button(EMP, "Другой вариант")
    assert "Вариантов достаточно" in log.alert
    assert len(gemini.prompts) == 4
    assert await h.get_state(EMP) == "ProposeTaskSG:ai"


async def test_ai_outage_falls_back_to_rules_and_retry_reaches_ai(app: BotHarness, gemini: FakeGemini) -> None:
    """Бесплатный лимит Gemini исчерпан: сотрудник всё равно получает формулировку по правилам
    (диалог не ломается). «🔁 Другой вариант» — AI снова доступен и отвечает."""
    h = app
    await team(h, second_manager=False)
    gemini.fail()
    gemini.answer("Проверить 100 договоров и представить отчёт с нарушениями", 100, "договоров")

    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, TITLE)
    await h.send_text(EMP, RAW_RESULT)
    assert "AI сейчас недоступен — проверено по правилам" in h.last_text(EMP)
    assert "Проверить 100 договоров и сделать отчёт" in h.last_text(EMP)

    await h.press_button(EMP, "Другой вариант")
    assert "🤖 Предлагаю измеримую формулировку" in h.last_text(EMP)
    await h.press_button(EMP, "Принять")
    await h.press_button(EMP, "Завтра")
    assert "Проверить 100 договоров и представить отчёт с нарушениями" in h.last_text(EMP)


async def test_retry_while_ai_still_down_does_not_blame_ai(app: BotHarness, gemini: FakeGemini) -> None:
    """Лимит Gemini исчерпан, сотрудник всё равно жмёт «🔁 Другой вариант» — AI снова не ответил.
    Бот не пишет «AI предложил тот же вариант» (AI ничего не предлагал), а честно говорит, что
    AI по-прежнему недоступен, и предлагает принять формулировку по правилам или написать свою."""
    h = app
    await team(h, second_manager=False)
    gemini.fail()
    gemini.fail()

    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, TITLE)
    await h.send_text(EMP, RAW_RESULT)
    await h.press_button(EMP, "Другой вариант")
    text = h.last_text(EMP)
    assert "AI сейчас недоступен — проверено по правилам" in text
    assert "AI предложил тот же вариант" not in text
    assert "AI по-прежнему недоступен" in text
    assert h.buttons(EMP)[0] == "✅ Принять"

async def test_without_ai_employee_writes_own_variant_and_plan(app: BotHarness) -> None:
    """AI выключен: «Другой вариант» объясняет, что его не получить. Сотрудник пишет свой вариант
    без числа — бот спрашивает плановое число; «много» не принимается, «15 встреч» — да."""
    h = app
    await team(h, second_manager=False)
    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, "Встречи с поставщиками")
    await h.send_text(EMP, "провести встречи с поставщиками")
    assert "Добавьте число или критерий приёмки" in h.last_text(EMP)

    log = await h.press_button(EMP, "Другой вариант")
    assert "AI сейчас недоступен" in log.alert
    await h.press_button(EMP, "Свой вариант")
    assert "Напишите свой вариант ожидаемого результата" in h.last_text(EMP)
    assert await h.get_state(EMP) == "ProposeTaskSG:result_manual"

    await h.send_text(EMP, "ок")
    assert "Слишком коротко" in h.last_text(EMP)
    await h.send_text(EMP, "Провести встречи с ключевыми поставщиками и сдать протоколы")
    assert "Плановое число" in h.last_text(EMP)
    assert h.buttons(EMP) == ["⏭ Пропустить", "✖️ Отмена"]

    await h.send_text(EMP, "много")
    assert "Не нашёл положительного числа" in h.last_text(EMP)
    await h.send_text(EMP, "0 встреч")
    assert "Не нашёл положительного числа" in h.last_text(EMP)
    await h.send_text(EMP, "15 встреч")
    assert "Шаг 3/3" in h.last_text(EMP)
    await h.press_button(EMP, "Завтра")
    summary = h.last_text(EMP)
    assert "Провести встречи с ключевыми поставщиками и сдать протоколы" in summary
    assert "План: 15 встреч" in summary


async def test_keep_as_written_without_number_and_skip_plan(app: BotHarness) -> None:
    """Сотрудник оставляет свою формулировку как есть (числа в ней нет) и пропускает план:
    поручение уходит без планового числа — сравнивать план и факт будут по тексту."""
    h = app
    await team(h, second_manager=False)
    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, "Регламент закупок")
    await h.send_text(EMP, "подготовить регламент закупок")
    await h.press_button(EMP, "Оставить как написал")
    assert "Плановое число" in h.last_text(EMP)
    await h.press_button(EMP, "Пропустить")
    await h.press_button(EMP, "Завтра")
    assert "План: — (без числа)" in h.last_text(EMP)
    await h.press_button(EMP, "Отправить руководителю")

    task = await h.get_task(1)
    assert task.expected_result == "подготовить регламент закупок"
    assert task.plan_value is None and task.plan_unit is None
    assert task.description is None, "исходный текст совпадает с результатом — не дублируется"


async def test_text_typed_over_suggestion_becomes_own_variant(app: BotHarness) -> None:
    """Вместо кнопок сотрудник просто пишет свой вариант поверх подсказки — бот принимает его,
    а кнопки старой подсказки после этого уже не срабатывают."""
    h = app
    await team(h, second_manager=False)
    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, TITLE)
    await h.send_text(EMP, RAW_RESULT)
    suggestion_id = h.last_message(EMP).message_id

    await h.send_text(EMP, "Проверить 120 договоров и сдать отчёт в Excel")
    assert "Шаг 3/3" in h.last_text(EMP), "число 120 взято из своего варианта — сразу срок"
    log = await h.press_button(EMP, "Принять", message_id=suggestion_id)
    assert STALE in log.alert
    assert await h.get_state(EMP) == "ProposeTaskSG:deadline"
    data = await h.get_data(EMP)
    assert (data["plan_value"], data["plan_unit"]) == (120, "договоров")


async def test_long_texts_with_html_characters_reach_manager_intact(app: BotHarness) -> None:
    """Сотрудник вставляет длинный текст из письма: название на пределе (255 символов) и результат
    почти на 2000 символов, со знаками «<», «>», «&». Бот не падает на разметке Telegram и лимите
    длины, а руководитель получает уведомление с кнопками решения."""
    h = app
    await team(h, second_manager=False)
    title = ("Сверка <актов> & счетов " * 20)[:255]
    result = ("проверить 100 договоров <ООО «Ромашка» & партнёры> и сдать отчёт; " * 40)[:1990]

    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, title)
    await h.send_text(EMP, result)
    assert await h.get_state(EMP) == "ProposeTaskSG:ai"
    await h.press_button(EMP, "Оставить как написал")
    await h.press_button(EMP, "Завтра")
    assert "Проверьте поручение" in h.last_text(EMP)
    await h.press_button(EMP, "Отправить руководителю")

    task = await h.get_task(1)
    assert task.title == title.strip() and task.expected_result == result.strip()
    assert (task.plan_value, task.plan_unit) == (100, "договоров")
    notice = h.find_message(MGR, "Сотрудник внёс поручение")
    assert "Сверка <актов> & счетов" in notice.content
    assert notice.button_texts == PROPOSAL_BUTTONS

    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "Ожидаемый результат")
    await h.send_text(MGR, "x" * 2001)
    assert "Результат — от 3 до 2000 символов" in h.last_text(MGR)
    await h.press_button(MGR, "Назад")
    await h.press_button(MGR, "Назад")
    await approve(h)
    assert "Сверка <актов> & счетов" in h.find_message(EMP, "Руководитель подтвердил").content

async def test_employee_input_mistakes_get_helpful_hints(app: BotHarness) -> None:
    """Сотрудник ошибается на каждом шаге — бот подсказывает и не теряет введённое:
    слишком короткое/длинное название, стикер вместо текста, слишком короткий результат,
    непонятный и прошедший срок, кнопка из устаревшего сообщения, текст вместо кнопки."""
    h = app
    await team(h, second_manager=False)
    await h.press_menu(EMP, BTN_PROPOSE)

    await h.send_text(EMP, "А")
    assert "Название слишком короткое" in h.last_text(EMP)
    await h.send_text(EMP, "Д" * 300)
    assert "Название длинновато (300 символов)" in h.last_text(EMP)
    await h.send_sticker(EMP)
    assert "отправьте ответ обычным текстом" in h.last_text(EMP)
    assert await h.get_state(EMP) == "ProposeTaskSG:title"

    await h.send_text(EMP, TITLE)
    await h.send_text(EMP, "ок")
    assert "Опишите результат чуть подробнее" in h.last_text(EMP)
    await h.send_text(EMP, RAW_RESULT)
    await h.press_button(EMP, "Принять")
    first_deadline_msg = h.last_message(EMP).message_id

    await h.send_text(EMP, "когда-нибудь")
    assert "Не понял срок «когда-нибудь»" in h.last_text(EMP)
    assert "завтра" in h.last_text(EMP), "с примерами"
    await h.send_text(EMP, "01.01.2020")
    assert "или он уже прошёл" in h.last_text(EMP)
    log = await h.press_button(EMP, "Завтра", message_id=first_deadline_msg)
    assert STALE in log.alert
    assert await h.get_state(EMP) == "ProposeTaskSG:deadline"

    await h.press_button(EMP, "Завтра")
    await h.send_text(EMP, "отправляй")
    assert "Выберите вариант кнопкой" in h.last_text(EMP)
    assert await h.get_state(EMP) == "ProposeTaskSG:confirm"
    assert await task_count(h) == 0


async def test_employee_corrects_draft_before_sending(app: BotHarness) -> None:
    """На сводке сотрудник замечает ошибку: «✏️ Изменить» → название (со спецсимволами
    «<», «&»), затем план и срок. Открыв правку срока, он передумывает — «◀ К сводке» возвращает
    черновик без изменений (раньше была только «✖️ Отмена», стиравшая весь черновик)."""
    h = app
    await team(h, second_manager=False)
    await fill_draft(h)

    await h.press_button(EMP, "Изменить")
    assert "Что изменить?" in h.last_text(EMP)
    assert h.buttons(EMP) == [
        "Название", "Ожидаемый результат", "План (число)", "Срок", "◀ К сводке", "✖️ Отмена",
    ]
    await h.press_button(EMP, "К сводке")
    assert "Проверьте поручение" in h.last_text(EMP)

    await h.press_button(EMP, "Изменить")
    await h.press_button(EMP, "Название")
    assert f"Сейчас: {TITLE}" in h.last_text(EMP)
    assert h.buttons(EMP) == ["◀ К сводке", "✖️ Отмена"]
    await h.send_text(EMP, "Д")
    assert "Название слишком короткое" in h.last_text(EMP)
    assert h.buttons(EMP) == ["◀ К сводке", "✖️ Отмена"], "и после ошибки можно вернуться"
    await h.send_text(EMP, "Анализ <договоров> & претензий")
    assert "Задача: Анализ <договоров> & претензий" in h.last_text(EMP), "сразу назад к сводке"

    await h.press_button(EMP, "Изменить")
    await h.press_button(EMP, "План (число)")
    assert "Сейчас: 100 договоров" in h.last_text(EMP)
    await h.send_text(EMP, "120")
    assert "План: 120 договоров" in h.last_text(EMP), "единица остаётся прежней"

    await h.press_button(EMP, "Изменить")
    await h.press_button(EMP, "Срок")
    assert "Сейчас:" in h.last_text(EMP)
    assert h.buttons(EMP)[-2:] == ["◀ К сводке", "✖️ Отмена"]
    await h.press_button(EMP, "К сводке")
    assert "Проверьте поручение" in h.last_text(EMP)
    assert await h.get_state(EMP) == "ProposeTaskSG:confirm"
    assert (await h.get_data(EMP))["deadline"] == tomorrow_deadline().isoformat()

    await h.press_button(EMP, "Изменить")
    await h.press_button(EMP, "Срок")
    await h.send_text(EMP, "через 2 недели")
    assert "Проверьте поручение" in h.last_text(EMP)

    await h.press_button(EMP, "Отправить руководителю")
    task = await h.get_task(1)
    assert task.title == "Анализ <договоров> & претензий"
    assert (task.plan_value, task.plan_unit) == (120, "договоров")
    assert to_local(task.deadline).date() == (to_local(utcnow()) + timedelta(days=14)).date()
    assert "Анализ <договоров> & претензий" in h.find_message(MGR, "Сотрудник внёс поручение").content


async def test_back_to_summary_is_not_offered_in_the_main_flow(app: BotHarness) -> None:
    """При первом заполнении (не правке) на шагах только «✖️ Отмена»: сводки ещё нет. Поддельная
    «◀ К сводке» на этом этапе не ломает диалог."""
    h = app
    await team(h, second_manager=False)
    await h.press_menu(EMP, BTN_PROPOSE)
    assert h.buttons(EMP) == ["✖️ Отмена"]
    await h.send_text(EMP, TITLE)
    assert h.buttons(EMP) == ["✖️ Отмена"]
    log = await h.press(EMP, "k:field:back")
    assert STALE in log.alert
    assert await h.get_state(EMP) == "ProposeTaskSG:result"


async def test_editing_result_from_summary_asks_ai_with_deadline_context(
    app: BotHarness, gemini: FakeGemini
) -> None:
    """Сотрудник уже выбрал срок и на сводке переписывает результат: AI получает срок как контекст,
    после принятия формулировки бот возвращается к сводке (срок не спрашивает заново)."""
    h = app
    await team(h, second_manager=False)
    gemini.answer("Проверить 100 договоров и сдать отчёт", 100, "договоров")
    gemini.answer("Проверить 150 договоров и сдать реестр нарушений", 150, "договоров")
    await fill_draft(h)

    await h.press_button(EMP, "Изменить")
    await h.press_button(EMP, "Ожидаемый результат")
    assert "Сейчас: Проверить 100 договоров и сдать отчёт" in h.last_text(EMP)
    await h.send_text(EMP, "проверить ещё 50 договоров, всего 150")
    assert "Срок (для контекста" in gemini.prompts[-1]
    await h.press_button(EMP, "Принять")
    summary = h.last_text(EMP)
    assert "Проверьте поручение" in summary
    assert "Проверить 150 договоров и сдать реестр нарушений" in summary and "План: 150 договоров" in summary


async def test_employee_cancels_or_leaves_dialog_via_menu(app: BotHarness) -> None:
    """Сотрудник передумал: «✖️ Отмена» на шаге срока — ничего не создано. Во второй раз он
    посреди диалога жмёт «📋 Мои задачи» — надпись кнопки не становится названием задачи."""
    h = app
    await team(h, second_manager=False)
    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, TITLE)
    await h.send_text(EMP, RAW_RESULT)
    await h.press_button(EMP, "Принять")
    log = await h.press_button(EMP, "Отмена")
    assert "Действие отменено" in log.text
    assert await h.get_state(EMP) is None

    await h.press_menu(EMP, BTN_PROPOSE)
    await h.press_menu(EMP, BTN_MY_TASKS)
    assert await h.get_state(EMP) is None
    assert await task_count(h) == 0
    assert all("Задача: 📋 Мои задачи" not in text for text in h.outputs(EMP))


async def test_deadline_in_another_year_shows_the_year(app: BotHarness) -> None:
    """Сотрудник опечатался в годе: «05.10.2030» вместо «05.10.2026». Бот принимает дату, но в
    сводке и у руководителя срок выглядит как «5 октября (сб), 18:00» — без года опечатку не
    заметить, а задача на 4 года выпадет из KPI. Срок не в текущем году должен показываться с годом."""
    h = app
    await team(h, second_manager=False)
    year = to_local(utcnow()).year + 4
    await fill_draft(h, deadline="Завтра")
    await h.press_button(EMP, "Изменить")
    await h.press_button(EMP, "Срок")
    await h.send_text(EMP, f"05.10.{year}")
    assert "Проверьте поручение" in h.last_text(EMP)
    assert str(year) in h.last_text(EMP)
    await h.press_button(EMP, "Отправить руководителю")
    assert str(year) in h.find_message(MGR, "Сотрудник внёс поручение").content


async def test_employee_blocked_mid_draft_cannot_finish_proposal(app: BotHarness) -> None:
    """Сотрудник начал вносить поручение, и в этот момент руководитель его заблокировал: дальше
    диалог не идёт (доступ закрыт), черновик сброшен, в систему ничего не попадает."""
    h = app
    await team(h, second_manager=False)
    await h.press_menu(EMP, BTN_PROPOSE)
    async with h.db() as session:
        await session.execute(update(User).where(User.tg_id == EMP).values(status=UserStatus.BLOCKED))
        await session.commit()

    log = await h.send_text(EMP, TITLE)
    assert "Шаг 2/3" not in log.text and "Доступ закрыт" in log.text
    assert await h.get_state(EMP) is None
    assert await task_count(h) == 0
    assert h.messages(MGR)[-1].content.startswith("🏠 Главное меню"), "руководителю ничего не пришло"


async def test_draft_deadline_expired_while_summary_was_open(app: BotHarness) -> None:
    """Сотрудник открыл сводку и отвлёкся — выбранный срок успел пройти. При отправке бот просит
    выбрать новый срок и только потом отправляет поручение."""
    h = app
    await team(h, second_manager=False)
    await fill_draft(h)
    context = h.dp.fsm.get_context(h.bot, chat_id=EMP, user_id=EMP)
    await context.update_data(deadline=(utcnow() - timedelta(hours=1)).isoformat())

    await h.press_button(EMP, "Отправить руководителю")
    assert "Указанный срок уже прошёл — выберите новый" in h.last_text(EMP)
    assert await task_count(h) == 0

    await h.press_button(EMP, "Завтра")
    assert "Проверьте поручение" in h.last_text(EMP)
    await h.press_button(EMP, "Отправить руководителю")
    assert (await h.get_task(1)).deadline == tomorrow_deadline()


# =================================================================================================
#  Руководитель подтверждает
# =================================================================================================


async def test_manager_approves_with_weight_and_priority(app: BotHarness) -> None:
    """Руководитель подтверждает поручение: видит загрузку сотрудника на неделе срока (70 %),
    ошибается с весом, вводит 25 % числом, выбирает высокий приоритет. Сотрудник получает
    уведомление с кнопкой «Сдать результат»; задача в работе и сразу принята (её внёс он сам)."""
    h = app
    await team(h)
    task_id = await propose(h)
    async with h.db() as session:
        manager = await users_svc.get_by_tg(session, MGR)
        employee = await users_svc.get_by_tg(session, EMP)
        await tasks_svc.create_task(
            session, creator=manager, assignee_id=employee.id, title="Квартальный отчёт",
            expected_result="Сдать отчёт", deadline=tomorrow_deadline(), weight=70,
        )
        await session.commit()

    await h.press_button(MGR, "Подтвердить")
    text = h.last_text(MGR)
    assert "Подтверждение поручения" in text and f"#{task_id}" in text and "Шаг 1/2" in text
    assert "на неделе срока: 70 %" in text
    assert "30 %" in h.buttons(MGR) and "40 % ⚠️" in h.buttons(MGR)
    assert await h.get_state(MGR) == "DecideProposalSG:weight"

    for wrong in ("много", "0", "150", "20,5"):
        await h.send_text(MGR, wrong)
        assert "Вес — целое число от 1 до 100" in h.last_text(MGR), wrong
    await h.send_text(MGR, "25%")
    assert "Вес: 25 %" in h.last_text(MGR) and "Шаг 2/2" in h.last_text(MGR)
    await h.send_text(MGR, "высокий")
    assert "Выберите вариант кнопкой" in h.last_text(MGR)

    log = await h.press_button(MGR, "Высокий")
    assert log.alert == "✅ Подтверждено"
    confirmed = h.find_message(MGR, "Подтверждено.")
    assert "в работе, сотрудник получил уведомление" in confirmed.content
    assert "Статус: 🔄 В работе" in confirmed.content
    assert "Ответственный руководитель: Петрова А. С." in confirmed.content
    assert "✏️ Изменить" in confirmed.button_texts and "📜 История" in confirmed.button_texts
    assert await h.get_state(MGR) is None

    note = log.to(EMP)
    assert "Руководитель подтвердил ваше поручение" in note.text
    assert "Вес: 25 %" in note.text and "🔴 Высокий" in note.text
    assert "Принята в работу" in note.text
    assert h.buttons(EMP) == ["📤 Сдать результат", "📋 Открыть"]

    task = await h.get_task(task_id)
    assert task.status == TaskStatus.ACTIVE
    assert (task.weight, task.priority) == (25, Priority.HIGH)
    assert task.manager.tg_id == MGR
    assert task.approved_at is not None and task.accepted_at is not None
    approved = [event for event in await events(h, task_id) if event.type == EventType.APPROVED]
    assert approved and approved[0].data == {"weight": 25, "priority": "high"}


async def test_cannot_approve_overdue_proposal_until_deadline_changed(app: BotHarness) -> None:
    """Руководитель начал подтверждать, но срок поручения успел пройти, пока он выбирал вес:
    на последнем шаге — alert и снова карточка с кнопками решения. «✅ Подтвердить» теперь сразу
    просит поменять срок. Руководитель меняет срок, после чего подтверждает."""
    h = app
    await team(h, second_manager=False)
    task_id = await propose(h)
    await h.press_button(MGR, "Подтвердить")
    await h.press_button(MGR, "20 %")
    await move_deadline_to_past(h, task_id)

    log = await h.press_button(MGR, "Средний")
    assert "Срок поручения уже прошёл" in log.alert
    assert "⚠️ Срок поручения уже прошёл" in h.last_text(MGR)
    assert all(button in h.buttons(MGR) for button in PROPOSAL_BUTTONS)
    assert await h.get_state(MGR) is None
    assert (await h.get_task(task_id)).status == TaskStatus.PROPOSED

    log = await h.press_button(MGR, "Подтвердить")
    assert "Срок этого поручения уже прошёл" in log.alert
    assert await h.get_state(MGR) is None

    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "Срок")
    await h.send_text(MGR, "через 3 дня")
    assert "Изменено: срок" in h.last_text(MGR)
    await approve(h)
    assert (await h.get_task(task_id)).status == TaskStatus.ACTIVE


async def test_cannot_approve_when_employee_was_blocked(app: BotHarness) -> None:
    """Пока поручение ждало решения, сотрудника заблокировали. Подтвердить его нельзя (об этом
    сразу говорит alert), а отклонить — можно; заблокированному уведомление не отправляется."""
    h = app
    await team(h, second_manager=False)
    task_id = await propose(h)
    async with h.db() as session:
        await session.execute(update(User).where(User.tg_id == EMP).values(status=UserStatus.BLOCKED))
        await session.commit()

    log = await h.press_button(MGR, "Подтвердить")
    assert "исполнитель больше не активный сотрудник" in log.alert
    assert await h.get_state(MGR) is None
    assert (await h.get_task(task_id)).status == TaskStatus.PROPOSED

    await h.press_button(MGR, "Отклонить")
    log = await h.press_button(MGR, "Пропустить")
    assert (await h.get_task(task_id)).status == TaskStatus.REJECTED
    assert not log.to(EMP).texts


# =================================================================================================
#  Руководитель корректирует
# =================================================================================================


async def test_manager_corrects_proposal_and_employee_sees_each_change(app: BotHarness) -> None:
    """Руководитель уточняет поручение перед подтверждением: название, результат, план, срок
    (кнопкой), затем убирает число из плана. После каждой правки сотрудник видит «было → стало»,
    руководитель — карточку с кнопками решения. Потом руководитель подтверждает — сотрудник
    получает уже исправленную задачу."""
    h = app
    await team(h)
    task_id = await propose(h)

    await h.press_button(MGR, "Изменить")
    assert "Что изменить?" in h.last_text(MGR)
    assert h.buttons(MGR) == [
        "Название", "Ожидаемый результат", "План (число)", "Срок", "◀ Назад", "✖️ Отмена",
    ]

    await h.press_button(MGR, "Название")
    assert f"Сейчас: {TITLE}" in h.last_text(MGR)
    await h.send_text(MGR, "А")
    assert "Название — от 2 до 255 символов" in h.last_text(MGR)
    log = await h.send_text(MGR, "Анализ договоров поставщиков за III квартал")
    assert "✅ Изменено: название" in h.last_text(MGR)
    assert "Подтвердить поручение?" in h.last_text(MGR)
    assert h.buttons(MGR) == PROPOSAL_BUTTONS
    change = log.to(EMP).text
    assert "Руководитель скорректировал ваше поручение" in change
    assert f"Название: «{TITLE}» → «Анализ договоров поставщиков за III квартал»" in change
    assert h.buttons(EMP) == ["📋 Открыть"], "поручение ещё не в работе — сдавать нечего"
    await h.press_button(EMP, "Открыть")
    card = h.last_text(EMP)
    assert "Анализ договоров поставщиков за III квартал" in card and "На подтверждении" in card
    assert "📜 История" in h.buttons(EMP)
    assert not set(PROPOSAL_BUTTONS) & set(h.buttons(EMP)), "решать по своему поручению сотрудник не может"
    assert "📤 Сдать результат" not in h.buttons(EMP)

    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "Ожидаемый результат")
    log = await h.send_text(MGR, "Проверить 100 договоров и сдать реестр нарушений в Excel")
    assert "Ожидаемый результат:" in log.to(EMP).text
    assert "«Проверить 100 договоров и сдать реестр нарушений в Excel»" in log.to(EMP).text

    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "План (число)")
    assert "🚫 Убрать число из плана" in h.buttons(MGR)
    await h.send_text(MGR, "ни одного")
    assert "Не нашёл положительного числа" in h.last_text(MGR)
    log = await h.send_text(MGR, "120")
    assert "План: 100 договоров → 120 договоров" in log.to(EMP).text  # «120» без единицы — единица прежняя

    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "Срок")
    await h.send_text(MGR, "вчера")
    assert "Не понял срок «вчера» или он уже прошёл" in h.last_text(MGR)
    new_label = next(label for label in h.buttons(MGR) if not label.startswith(("Завтра", "◀", "✖️")))
    log = await h.press_button(MGR, new_label)
    assert log.alert == "✅ Сохранено"
    assert "Срок:" in log.to(EMP).text

    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "План (число)")
    log = await h.press_button(MGR, "Убрать число из плана")
    assert "Изменено: плановое число, единица плана" in h.last_text(MGR)
    assert "План: 120 договоров → —" in log.to(EMP).text

    await approve(h)
    task = await h.get_task(task_id)
    assert task.status == TaskStatus.ACTIVE
    assert task.title == "Анализ договоров поставщиков за III квартал"
    assert task.expected_result == "Проверить 100 договоров и сдать реестр нарушений в Excel"
    assert task.plan_value is None and task.plan_unit is None
    assert task.deadline != tomorrow_deadline()
    approved_note = h.find_message(EMP, "Руководитель подтвердил ваше поручение").content
    assert "Анализ договоров поставщиков за III квартал" in approved_note
    edited = [event for event in await events(h, task_id) if event.type == EventType.EDITED]
    assert len(edited) == 5


async def test_manager_edit_without_changes_and_back_buttons(app: BotHarness) -> None:
    """Руководитель открывает правку и вводит то же название — «Ничего не изменилось», сотрудника
    не беспокоят. Затем выбирает «Срок», передумывает: «◀ Назад» — снова выбор поля, ещё раз
    «◀ Назад» — карточка с кнопками решения. Предложение не изменилось."""
    h = app
    await team(h, second_manager=False)
    task_id = await propose(h)

    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "Название")
    assert h.buttons(MGR) == ["◀ Назад", "✖️ Отмена"]
    log = await h.send_text(MGR, TITLE)
    assert "Ничего не изменилось" in h.last_text(MGR)
    assert not log.to(EMP).texts

    await h.press_button(MGR, "Изменить")
    await h.send_text(MGR, "название")
    assert "Выберите вариант кнопкой" in h.last_text(MGR)
    await h.press_button(MGR, "Срок")
    assert h.buttons(MGR)[-2:] == ["◀ Назад", "✖️ Отмена"]
    await h.press_button(MGR, "Назад")
    assert "Что изменить?" in h.last_text(MGR)
    assert await h.get_state(MGR) == "DecideProposalSG:edit_pick"
    await h.press_button(MGR, "Назад")
    assert h.buttons(MGR) == PROPOSAL_BUTTONS
    assert await h.get_state(MGR) is None
    assert not [event for event in await events(h, task_id) if event.type == EventType.EDITED]
    assert (await h.get_task(task_id)).deadline == tomorrow_deadline()


async def test_manager_cancels_approval_and_finds_proposal_in_queue(app: BotHarness) -> None:
    """Руководитель начал подтверждать и нажал «✖️ Отмена»: поручение остаётся на подтверждении,
    сотруднику ничего не приходит, а в «📥 Предложения» оно по-прежнему ждёт решения."""
    h = app
    await team(h, second_manager=False)
    task_id = await propose(h)

    await h.press_button(MGR, "Подтвердить")
    await h.press_button(MGR, "20 %")
    log = await h.press_button(MGR, "Отмена")
    assert "Действие отменено" in log.text
    assert not log.to(EMP).texts
    assert await h.get_state(MGR) is None
    assert (await h.get_task(task_id)).status == TaskStatus.PROPOSED

    await h.press_menu(MGR, BTN_PROPOSALS)
    assert "Предложения сотрудников (1)" in h.last_text(MGR)
    assert TITLE in h.last_text(MGR)


# =================================================================================================
#  Руководитель отклоняет
# =================================================================================================


async def test_manager_rejects_with_reason(app: BotHarness) -> None:
    """Руководитель отклоняет поручение и объясняет почему (слишком длинную причину бот просит
    сократить). Сотрудник видит решение и причину; в журнале — событие с причиной."""
    h = app
    await team(h)
    task_id = await propose(h)

    await h.press_button(MGR, "Отклонить")
    assert "Отклонение поручения" in h.last_text(MGR) and "Напишите причину" in h.last_text(MGR)
    assert h.buttons(MGR) == ["⏭ Пропустить", "✖️ Отмена"]
    await h.send_text(MGR, "x" * 1001)
    assert "Слишком длинно (1001 символов)" in h.last_text(MGR)
    assert await h.get_state(MGR) == "DecideProposalSG:reject_reason"

    log = await h.send_text(MGR, "Это уже входит в план отдела <на октябрь>")
    text = h.last_text(MGR)
    assert f"Предложение #{task_id} отклонено" in text
    assert "Причина: Это уже входит в план отдела <на октябрь>" in text
    assert "Статус: ❌ Отклонена" in text
    note = log.to(EMP).text
    assert "Руководитель отклонил ваше поручение" in note and TITLE in note
    assert "Причина: Это уже входит в план отдела <на октябрь>" in note

    task = await h.get_task(task_id)
    assert task.status == TaskStatus.REJECTED
    rejected = [event for event in await events(h, task_id) if event.type == EventType.REJECTED]
    assert rejected[0].data == {"reason": "Это уже входит в план отдела <на октябрь>"}
    assert await h.get_state(MGR) is None


async def test_manager_rejects_without_reason(app: BotHarness) -> None:
    """Руководитель отклоняет поручение, не указывая причину: сотрудник получает короткое
    уведомление без строки «Причина»."""
    h = app
    await team(h, second_manager=False)
    task_id = await propose(h)

    await h.press_button(MGR, "Отклонить")
    log = await h.press_button(MGR, "Пропустить")
    assert log.alert == "❌ Отклонено"
    assert f"Предложение #{task_id} отклонено" in h.last_text(MGR)
    assert "Причина" not in h.last_text(MGR)
    note = log.to(EMP).text
    assert "Руководитель отклонил ваше поручение" in note and "Причина" not in note
    task = await h.get_task(task_id)
    assert task.status == TaskStatus.REJECTED
    rejected = [event for event in await events(h, task_id) if event.type == EventType.REJECTED]
    assert rejected[0].data == {"reason": None}


# =================================================================================================
#  Два руководителя и чужие руки
# =================================================================================================


async def test_second_manager_gets_alert_on_already_processed_proposal(app: BotHarness) -> None:
    """Уведомление пришло обоим руководителям. Петрова подтвердила поручение; Соколов позже жмёт
    кнопку в своём уведомлении — alert «Предложение уже обработано», а уведомление превращается
    в актуальную карточку (в работе, без кнопок решения). Остальные кнопки решения (если бы
    кто-то нажал их из старого сообщения) тоже дают alert; задача не меняется."""
    h = app
    await team(h)
    task_id = await propose(h)
    notice_id = h.find_message(MGR2, "Сотрудник внёс поручение").message_id
    await approve(h, MGR, weight="30 %", priority="Низкий")

    log = await h.press_button(MGR2, "Подтвердить")
    assert log.alert == ALREADY
    notice = h.api.messages[(MGR2, notice_id)]
    assert "Предложение уже обработано. Актуальное состояние:" in notice.content
    assert "Статус: 🔄 В работе" in notice.content and "Вес: 30 %" in notice.content
    assert "✅ Подтвердить" not in notice.button_texts and "❌ Отклонить" not in notice.button_texts
    assert notice.button_texts == ["✏️ Изменить", "🚫 Отменить", "📜 История"], "действия с задачей в работе"

    for action in ("pedit", "reject"):
        log = await h.press(MGR2, TaskCB(action=action, task_id=task_id), notice_id)
        assert log.alert == ALREADY, action
        assert not log.to(EMP).texts
    assert await h.get_state(MGR2) is None
    task = await h.get_task(task_id)
    assert (task.status, task.weight, task.priority) == (TaskStatus.ACTIVE, 30, Priority.LOW)
    assert task.manager.tg_id == MGR


async def test_second_manager_mid_dialog_when_first_decides(app: BotHarness) -> None:
    """Соколов начал подтверждать (выбрал вес), а Петрова в это время отклонила поручение:
    на выборе приоритета Соколов получает alert, задача остаётся отклонённой. То же с правкой
    и с причиной отклонения, если Петрова успела подтвердить."""
    h = app
    await team(h)
    first = await propose(h)
    await h.press_button(MGR2, "Подтвердить")
    await h.press_button(MGR2, "20 %")
    await h.press_button(MGR, "Отклонить")
    await h.press_button(MGR, "Пропустить")
    log = await h.press_button(MGR2, "Средний")
    assert log.alert == ALREADY
    assert await h.get_state(MGR2) is None
    assert (await h.get_task(first)).status == TaskStatus.REJECTED

    second = await propose(h, title="Сверка остатков")
    await h.press_button(MGR2, "Изменить")
    await h.press_button(MGR2, "Название")
    await approve(h, MGR)
    log = await h.send_text(MGR2, "Сверка остатков на складе")
    assert ALREADY in log.text
    assert (await h.get_task(second)).title == "Сверка остатков"
    assert await h.get_state(MGR2) is None

    third = await propose(h, title="Отчёт по претензиям")
    await h.press_button(MGR2, "Отклонить")
    await approve(h, MGR)
    log = await h.send_text(MGR2, "Не актуально")
    assert ALREADY in log.text
    assert (await h.get_task(third)).status == TaskStatus.ACTIVE


async def test_employee_cannot_decide_proposals_and_manager_cannot_propose(app: BotHarness) -> None:
    """Сотрудник «подделывает» нажатия кнопок решения по своему поручению и листание очереди —
    получает отказ, поручение не меняется. Руководителю «/propose» недоступна, сотруднику —
    «/proposals». Несуществующее поручение — «Задача не найдена»."""
    h = app
    await team(h, second_manager=False)
    task_id = await propose(h)

    for action in ("approve", "reject", "pedit"):
        log = await h.press(EMP, TaskCB(action=action, task_id=task_id))
        assert NO_RIGHTS in log.alert, action
    log = await h.press(EMP, ListCB(scope="proposals", status="all", page=0))
    assert NO_RIGHTS in log.alert
    assert (await h.get_task(task_id)).status == TaskStatus.PROPOSED

    log = await h.send_command(EMP, "proposals")
    assert "Предложения сотрудников" not in log.text
    log = await h.send_command(MGR, "propose")
    assert "Новое поручение" not in log.text
    assert await h.get_state(MGR) is None

    log = await h.press(MGR, TaskCB(action="approve", task_id=999))
    assert log.alert == "Задача не найдена."


# =================================================================================================
#  «📥 Предложения»
# =================================================================================================


async def test_proposals_queue_with_pagination(app: BotHarness) -> None:
    """У руководителя накопилось 9 предложений от двух сотрудников. «📥 Предложения» показывает
    первые 8 (старые сверху) и «Вперёд ▶»; на второй странице — девятое и «◀ Назад». Из списка
    поручение открывается с кнопками решения; подтверждённое исчезает из очереди."""
    h = app
    await team(h, second_manager=False, second_employee=True)
    ids = [await seed_proposal(h, EMP if n % 2 else EMP2, f"Поручение {n}") for n in range(1, 10)]

    await h.press_menu(MGR, BTN_PROPOSALS)
    text = h.last_text(MGR)
    assert "Предложения сотрудников (9)" in text
    assert "1. 📥 #1 Поручение 1" in text and "8. 📥 #8 Поручение 8" in text
    assert "Поручение 9" not in text
    assert "👤 Иванов И. И." in text and "👤 Сидорова М. О." in text
    buttons = h.buttons(MGR)
    assert len(buttons) == 9 and buttons[-1] == "Вперёд ▶"
    assert buttons[0].startswith("📥 #1 Иванов")

    await h.press_button(MGR, "Вперёд")
    text = h.last_text(MGR)
    assert "9. 📥 #9 Поручение 9" in text and "1. " not in text
    assert h.buttons(MGR) == [h.buttons(MGR)[0], "◀ Назад"]
    await h.press_button(MGR, "Назад")
    assert "1. 📥 #1 Поручение 1" in h.last_text(MGR)

    await h.press(MGR, ListCB(scope="proposals", status="all", page=42))
    assert "9. 📥 #9 Поручение 9" in h.last_text(MGR), "несуществующая страница — последняя"

    await h.press_button(MGR, "#9")
    card = h.last_text(MGR)
    assert f"Задача #{ids[-1]}" in card and "Поручение 9" in card
    assert all(button in h.buttons(MGR) for button in PROPOSAL_BUTTONS)
    await approve(h)
    assert (await h.get_task(ids[-1])).status == TaskStatus.ACTIVE

    await h.press_menu(MGR, BTN_PROPOSALS)
    assert "Предложения сотрудников (8)" in h.last_text(MGR)
    assert h.buttons(MGR)[-1] != "Вперёд ▶", "8 предложений помещаются на одну страницу"


async def test_card_opened_from_queue_leads_back_to_queue(app: BotHarness) -> None:
    """Руководитель открыл поручение из «📥 Предложения» (вторая страница) и решил пока не
    решать: «◀ К списку задач» должна вернуть его в очередь предложений на ту же страницу,
    а не в «📋 Задачи — В работе», где этого поручения нет («Задач нет» сбивает с толку)."""
    h = app
    await team(h, second_manager=False)
    for n in range(1, 10):
        await seed_proposal(h, EMP, f"Поручение {n}")
    await h.press_menu(MGR, BTN_PROPOSALS)
    await h.press_button(MGR, "Вперёд")
    await h.press_button(MGR, "#9")
    await h.press_button(MGR, "К списку")
    text = h.last_text(MGR)
    assert "Предложения сотрудников (9)" in text and "9. 📥 #9 Поручение 9" in text


async def test_empty_proposals_queue(app: BotHarness) -> None:
    """Предложений нет — руководитель видит понятное сообщение без кнопок."""
    h = app
    await team(h, second_manager=False)
    log = await h.press_menu(MGR, BTN_PROPOSALS)
    assert "Новых предложений нет" in log.text
    assert h.buttons(MGR) == []


# =================================================================================================
#  Поручение в KPI
# =================================================================================================


async def test_completed_proposal_counts_as_self_initiated_in_kpi(app: BotHarness) -> None:
    """Полный цикл ТЗ для устного поручения: сотрудник внёс → руководитель подтвердил →
    сотрудник сдал результат → руководитель подтвердил оценку → в «📈 Моя эффективность»
    поручение вошло в KPI и посчитано как «внесено самостоятельно». Отклонённое и ещё не
    подтверждённое поручения в KPI не попадают."""
    h = app
    local_now = to_local(utcnow())
    if local_now.hour == 23 and local_now.minute >= 45:
        pytest.skip("до полуночи слишком мало времени для срока «сегодня 23:59»")
    await team(h, second_manager=False)

    # Срок — сегодня до конца дня: поручение гарантированно попадает в текущие неделю и месяц.
    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, TITLE)
    await h.send_text(EMP, RAW_RESULT)
    await h.press_button(EMP, "Принять")
    await h.send_text(EMP, "сегодня 23:59")
    await h.press_button(EMP, "Отправить руководителю")
    kpi_task = await h.scalar(select(func.max(Task.id)))
    deadline = (await h.get_task(kpi_task)).deadline
    rejected = await seed_proposal(h, EMP, "Лишнее поручение", deadline=deadline)
    pending = await seed_proposal(h, EMP, "Ждёт решения", deadline=deadline)

    await h.press(MGR, TaskCB(action="approve", task_id=kpi_task))
    await h.press_button(MGR, "20 %")
    await h.press_button(MGR, "Высокий")
    await h.press(MGR, TaskCB(action="reject", task_id=rejected))
    await h.press_button(MGR, "Пропустить")
    assert (await h.get_task(pending)).status == TaskStatus.PROPOSED

    approved_note = h.find_message(EMP, "Руководитель подтвердил ваше поручение")
    await h.press(EMP, TaskCB(action="submit", task_id=kpi_task), approved_note.message_id)
    await h.send_text(EMP, "Проверено 100 договоров, в 12 найдены нарушения")
    await h.press_button(EMP, "Пропустить")
    await h.send_text(EMP, "100")
    await h.press_button(EMP, "Без файлов")
    await h.press_button(EMP, "Отправить")
    assert (await h.get_task(kpi_task)).status == TaskStatus.SUBMITTED

    await h.press_button(MGR, "Подтвердить 100")
    task = await h.get_task(kpi_task)
    assert (task.status, task.final_score, task.source) == (TaskStatus.DONE, 100, TaskSource.EMPLOYEE)

    await h.press_menu(EMP, BTN_MY_KPI)
    card = h.last_text(EMP)
    assert "Иванов Иван Иванович — 100 %" in card
    assert "✋ Внесено самостоятельно: 1" in card
    assert "✅ Выполнено задач: 1 из 1" in card
    assert f"• {TITLE} — вес 20 % × 100 %" in card
    assert "Лишнее поручение" not in card and "Ждёт решения" not in card


# --- Особые случаи: своё поручение, нет руководителя, опечатка в годе ----------------------------


async def test_promoted_employee_is_told_why_own_proposal_cannot_be_approved(app: BotHarness) -> None:
    """Иванов внёс поручение, а потом его назначили руководителем. Он открывает своё поручение
    и жмёт «✅ Подтвердить» — бот прямо говорит, что это его собственное поручение и задачи
    ставятся только сотрудникам (а не загадочное «исполнитель больше не активный сотрудник»).
    Петрова видит обычное объяснение про исполнителя. Поручение остаётся на подтверждении."""
    h = app
    await team(h, second_manager=False)
    task_id = await seed_proposal(h, EMP, "Справка для юристов")
    async with h.db() as session:
        petrova = await users_svc.get_by_tg(session, MGR)
        ivanov = await users_svc.get_by_tg(session, EMP)
        await users_svc.set_role(session, ivanov.id, Role.MANAGER, petrova)
        await session.commit()

    log = await h.press(EMP, TaskCB(action="approve", task_id=task_id))
    assert "Это ваше поручение" in log.alert and "только сотрудникам" in log.alert
    assert await h.get_state(EMP) is None

    log = await h.press(MGR, TaskCB(action="approve", task_id=task_id))
    assert "исполнитель больше не активный сотрудник" in log.alert
    assert (await h.get_task(task_id)).status == TaskStatus.PROPOSED


async def test_proposal_when_no_manager_is_in_the_bot(app: BotHarness) -> None:
    """В боте пока нет ни одного активного руководителя. Иванов вносит поручение — оно сохранено,
    но бот не обещает «отправлено руководителю»: честно говорит, что уведомить некого, и советует
    сообщить руководителю лично. Поручение ждёт в «📥 Предложения»."""
    h = app
    await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    await h.send_command(EMP, "menu")
    log = await h.press_menu(EMP, BTN_PROPOSE)
    assert log.texts
    await h.send_text(EMP, TITLE)
    await h.send_text(EMP, RAW_RESULT)
    await h.press_button(EMP, "Принять")
    await h.press_button(EMP, "Завтра")
    log = await h.press_button(EMP, "Отправить руководителю")

    text = h.last_text(EMP)
    assert "сохранено" in text and "нет активного руководителя" in text
    assert "отправлено руководителю на подтверждение" not in text
    [task] = await h.scalars(select(Task))
    assert task.status == TaskStatus.PROPOSED


async def test_far_year_typo_in_deadline_is_explained(app: BotHarness) -> None:
    """И сотрудник в черновике, и руководитель при правке поручения опечатываются в годе
    («05.10.2099»). Бот не пишет загадочное «не понял срок», а просит проверить год; срок не меняется."""
    h = app
    await team(h, second_manager=False)
    await h.press_menu(EMP, BTN_PROPOSE)
    await h.send_text(EMP, TITLE)
    await h.send_text(EMP, RAW_RESULT)
    await h.press_button(EMP, "Принять")
    log = await h.send_text(EMP, "05.10.2099")
    assert "проверьте год" in log.text
    assert await h.get_state(EMP) == "ProposeTaskSG:deadline"

    task_id = await seed_proposal(h, EMP, "Справка для юристов")
    before = (await h.get_task(task_id)).deadline
    await h.press(MGR, TaskCB(action="pedit", task_id=task_id))
    await h.press_button(MGR, "Срок")
    log = await h.send_text(MGR, "05.10.2099")
    assert "проверьте год" in log.text
    assert (await h.get_task(task_id)).deadline == before
