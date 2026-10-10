"""Сценарии «начальник ставит задачу» и «задачи: списки, карточка, правка, отмена» (SPEC 7.3, 7.5).

Пример из ТЗ: начальник выбирает сотрудника и указывает
«Задача: провести анализ договоров → Ожидаемый результат: проверить 100 договоров и представить
отчёт → Срок: 5 октября → Вес: 20 %». Бот помогает сделать результат измеримым (AI или правила),
сотрудник получает уведомление и нажимает «✅ Принял в работу».

AI в тестах не ходит в сеть: ответы Gemini подменяются (``FakeGemini``), без подмены
работает путь «по правилам» (AI_PROVIDER=none в tests/e2e/conftest.py).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from bot.ai import formulate, provider
from bot.ai.provider import AIUnavailable
from bot.db.models import EventType, Priority, Task, TaskEvent, TaskSource, TaskStatus, User, UserStatus
from bot.handlers import task_create
from bot.services import tasks as tasks_svc
from bot.ui.callbacks import ListCB, PickCB, TaskCB
from bot.ui.texts import BTN_MY_TASKS, BTN_NEW_TASK, BTN_TASKS
from bot.utils.dateparse import iso_to_deadline, parse_deadline
from bot.utils.dates import MONTHS_GEN, fmt_deadline, to_local, utcnow

from .fakebot import MANAGER_TG_ID, BotHarness

pytestmark = pytest.mark.asyncio

MGR = MANAGER_TG_ID
EMP = 2001   # Иванов Иван Иванович
EMP2 = 2002  # Сидоров Пётр Ильич
MGR2 = 1002  # второй начальник (заведён в БД напрямую)

NO_RIGHTS = "⛔ Недостаточно прав для этого действия."
STALE = "Эта кнопка уже неактуальна"

TZ_TITLE = "Провести анализ договоров"
TZ_RESULT = "проверить 100 договоров и представить отчёт"
AI_RESULT = "Проверить 100 договоров поставщиков и представить отчёт в Excel с перечнем нарушений"


# --- Подготовка ------------------------------------------------------------------------------


class FakeGemini:
    """Подмена бесплатного Gemini: отдаёт заранее заданные ответы по очереди и запоминает запросы.

    formulate.py импортирует ``generate_json``/``ai_available`` из provider по имени, поэтому
    подменяем и в provider, и в formulate.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *answers: dict[str, Any] | BaseException) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        for module in (provider, formulate):
            monkeypatch.setattr(module, "ai_available", lambda: True)
            monkeypatch.setattr(module, "generate_json", self)

    async def __call__(
        self, *, system: str, parts: list[Any], schema: dict[str, Any], max_output_tokens: int = 2048
    ) -> tuple[dict[str, Any], str]:
        self.prompts.append("\n".join(str(part) for part in parts))
        if not self.answers:
            raise AIUnavailable("ответы закончились")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer, "gemini-test"


def ai_answer(expected: str, plan_value: float | None = None, plan_unit: str | None = None,
              note: str | None = None) -> dict[str, Any]:
    return {"expected_result": expected, "plan_value": plan_value, "plan_unit": plan_unit, "note": note}


async def team(h: BotHarness) -> tuple[User, User, User]:
    """Начальник (уже нажал /start, меню показано) и два активных сотрудника."""
    mgr = await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    emp = await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    emp2 = await h.seed_user(EMP2, "Сидоров Пётр Ильич")
    await h.send_command(MGR, "start")
    await h.send_command(EMP, "start")
    await h.send_command(EMP2, "start")
    return mgr, emp, emp2


def tz_deadline_text() -> str:
    """«5 октября» из ТЗ; если именно сейчас этот срок уже прошёл (вечер 5 октября) — «6 октября»."""
    return "5 октября" if parse_deadline("5 октября") is not None else "6 октября"


def tomorrow_deadline() -> datetime:
    tomorrow = to_local(utcnow()).date() + timedelta(days=1)
    return iso_to_deadline(tomorrow.isoformat())


async def reach_result_step(h: BotHarness, *, employee: str = "Иванов", title: str = TZ_TITLE) -> None:
    await h.press_menu(MGR, BTN_NEW_TASK)
    await h.press_button(MGR, employee)
    await h.send_text(MGR, title)


async def reach_summary(
    h: BotHarness,
    *,
    employee: str = "Иванов",
    title: str = TZ_TITLE,
    result: str = TZ_RESULT,
    deadline: str | None = None,
    priority: str = "Средний",
    weight: str = "20 %",
) -> None:
    """Пройти мастер по правилам (без AI) до сводки «📋 Проверьте задачу»."""
    await reach_result_step(h, employee=employee, title=title)
    await h.send_text(MGR, result)
    await h.press_button(MGR, "Принять")
    if "Плановое число" in (h.last_text(MGR) or ""):
        await h.press_button(MGR, "Пропустить")
    if deadline is None:
        await h.press_button(MGR, "Завтра")
    else:
        await h.send_text(MGR, deadline)
    await h.press_button(MGR, priority)
    await h.press_button(MGR, weight)
    assert "Проверьте задачу" in (h.last_text(MGR) or "")


async def all_tasks(h: BotHarness) -> list[Task]:
    return await h.scalars(select(Task).order_by(Task.id))


async def event_types(h: BotHarness, task_id: int) -> list[EventType]:
    events = await h.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id))
    return [event.type for event in events]


async def set_fsm(h: BotHarness, user_id: int, **data: Any) -> None:
    context = h.dp.fsm.get_context(h.bot, chat_id=user_id, user_id=user_id)
    await context.update_data(**data)


async def seed_task(
    h: BotHarness,
    assignee_tg: int,
    title: str,
    *,
    deadline: datetime | None = None,
    days: float = 3,
    weight: int = 10,
    status: TaskStatus = TaskStatus.ACTIVE,
    score: float | None = None,
    overdue: bool = False,
    plan_value: float | None = None,
    plan_unit: str | None = None,
) -> int:
    """Задача, поставленная начальником MGR, — через сервисы (как если бы её поставили раньше)."""
    async with h.db() as s:
        mgr = await s.scalar(select(User).where(User.tg_id == MGR))
        emp = await s.scalar(select(User).where(User.tg_id == assignee_tg))
        task = await tasks_svc.create_task(
            s,
            creator=mgr,
            assignee_id=emp.id,
            title=title,
            expected_result=f"{title}: результат",
            deadline=deadline or utcnow() + timedelta(days=days),
            weight=weight,
            plan_value=plan_value,
            plan_unit=plan_unit,
        )
        if status in (TaskStatus.SUBMITTED, TaskStatus.DONE, TaskStatus.REWORK):
            sub = await tasks_svc.submit_result(s, task.id, emp, fact_text="Сделано")
            await tasks_svc.record_evaluation(s, sub.id, score=score or 100, rationale="ok", source="rules")
            if status == TaskStatus.DONE:
                await tasks_svc.review_confirm(s, sub.id, mgr)
            elif status == TaskStatus.REWORK:
                await tasks_svc.review_rework(s, sub.id, mgr, "Добавьте выводы")
        elif status == TaskStatus.CANCELLED:
            await tasks_svc.cancel_task(s, task.id, mgr, "не актуально")
        if overdue:
            task.deadline = utcnow() - timedelta(days=1, hours=2)
        await s.commit()
        return task.id


# =============================================================================================
# 1. Постановка задачи
# =============================================================================================


async def test_manager_sets_tz_example_task_with_ai_and_employee_accepts(app, monkeypatch):
    """Начальник ставит задачу из ТЗ: AI делает результат измеримым, сотрудник принимает задачу.

    «Провести анализ договоров» → «проверить 100 договоров и представить отчёт» → AI предлагает
    формулировку с планом «100 договоров» → «✅ Принять» → срок «5 октября» → 🔴 Высокий → вес 20 %
    → сводка → «✅ Создать». Сотруднику приходит карточка с «✅ Принял в работу».
    """
    h = app
    ai = FakeGemini(monkeypatch, ai_answer(AI_RESULT, 100, "договоров", "Уточните формат отчёта"))
    mgr, emp, _ = await team(h)

    log = await h.press_menu(MGR, BTN_NEW_TASK)
    assert "шаг 1 из 6" in log.text and "Выберите сотрудника" in log.text
    assert {"Иванов И. И.", "Сидоров П. И.", "✖️ Отмена"} <= set(h.buttons(MGR))

    await h.press_button(MGR, "Иванов")
    assert "👤 Сотрудник: Иванов Иван Иванович" in h.last_text(MGR)
    assert "шаг 2 из 6" in h.last_text(MGR)

    await h.send_text(MGR, TZ_TITLE)
    assert "шаг 3 из 6" in h.last_text(MGR) and "ожидаемый результат" in h.last_text(MGR)

    log = await h.send_text(MGR, TZ_RESULT)
    assert "⏳ Формулирую измеримый результат…" in log.texts
    shown = h.last_text(MGR)
    assert "🤖 Предлагаю измеримую формулировку:" in shown
    assert AI_RESULT in shown
    assert "📊 План: 100 договоров" in shown
    assert "💡 Уточните формат отчёта" in shown
    assert f"Вы написали: {TZ_RESULT}" in shown
    assert h.buttons(MGR) == ["✅ Принять", "🔁 Другой вариант", "✏️ Свой вариант",
                              "📝 Оставить как написал", "✖️ Отмена"]
    # В запросе к AI — название и слова начальника.
    assert len(ai.prompts) == 1 and TZ_TITLE in ai.prompts[0] and TZ_RESULT in ai.prompts[0]

    # План известен из подсказки — шаг «плановое число» пропускается.
    await h.press_button(MGR, "Принять")
    assert f"🎯 Ожидаемый результат: {AI_RESULT}" in h.last_text(MGR)
    assert "шаг 4 из 6" in h.last_text(MGR)
    assert h.has_button(MGR, "Завтра")

    deadline_text = tz_deadline_text()
    expected_deadline = parse_deadline(deadline_text)
    await h.send_text(MGR, deadline_text)
    assert f"📅 Срок: {fmt_deadline(expected_deadline)}" in h.last_text(MGR)
    assert "шаг 5 из 6" in h.last_text(MGR)

    await h.press_button(MGR, "Высокий")
    assert "шаг 6 из 6" in h.last_text(MGR)
    assert "Загрузка недели сотрудника: 0 %" in h.last_text(MGR)

    await h.press_button(MGR, "20 %")
    summary = h.last_text(MGR)
    for expected in ("📋 Проверьте задачу", "👤 Исполнитель: Иванов Иван Иванович", f"📌 Задача: {TZ_TITLE}",
                     AI_RESULT, "📊 План: 100 договоров", f"💬 Описание: {TZ_RESULT}",
                     f"📅 Срок: {fmt_deadline(expected_deadline)}", "⚡ Приоритет: 🔴 Высокий", "⚖️ Вес: 20 %"):
        assert expected in summary, expected
    assert "✅ Создать" in h.buttons(MGR) and "✏️ Изменить" in h.buttons(MGR)

    log = await h.press_button(MGR, "Создать")
    assert log.alert == "✅ Задача поставлена"
    card = h.last_text(MGR)
    assert card.startswith("✅ Задача #1 поставлена")
    assert "⏳ Исполнитель ещё не подтвердил получение" in card
    assert h.buttons(MGR) == ["✏️ Изменить", "🚫 Отменить", "📜 История", "◀ К списку задач"]
    assert "Уведомление не доставлено" not in card
    assert await h.get_state(MGR) is None
    # Выше в чате не осталось кнопок мастера («✖️ Отмена» после создания сбивала бы с толку).
    leftovers = [msg.button_texts for msg in h.messages(MGR)[:-1] if msg.buttons]
    assert leftovers == []

    # Сотруднику — уведомление с карточкой (без строки «Исполнитель») и кнопкой «Принял в работу».
    notice = log.to(EMP).text
    assert "🆕 Вам поставлена новая задача" in notice
    assert AI_RESULT in notice and "Вес: 20 %" in notice and "Исполнитель:" not in notice
    assert h.buttons(EMP) == ["✅ Принял в работу", "📋 Открыть"]

    [task] = await all_tasks(h)
    assert task.title == TZ_TITLE
    assert task.expected_result == AI_RESULT
    assert task.description == TZ_RESULT  # исходные слова начальника сохранены
    assert (task.plan_value, task.plan_unit) == (100, "договоров")
    assert task.deadline == expected_deadline
    assert (task.priority, task.weight) == (Priority.HIGH, 20)
    assert (task.status, task.source) == (TaskStatus.ACTIVE, TaskSource.MANAGER)
    assert (task.assignee_id, task.created_by_id, task.manager_id) == (emp.id, mgr.id, mgr.id)
    assert task.accepted_at is None
    assert await event_types(h, task.id) == [EventType.CREATED]

    # Сотрудник подтверждает получение.
    log = await h.press_button(EMP, "Принял в работу")
    assert log.alert == "Принято в работу ✅"
    assert "✔️ Принята в работу" in h.last_text(EMP)
    assert "✅ Принял в работу" not in h.buttons(EMP)
    assert "📤 Сдать результат" in h.buttons(EMP)
    task = await h.get_task(task.id)
    assert task.accepted_at is not None
    assert await event_types(h, task.id) == [EventType.CREATED, EventType.ACCEPTED]

    # Повторное нажатие (кнопка в старом сообщении) ничего не ломает.
    log = await h.press(EMP, TaskCB(action="accept", task_id=task.id))
    assert log.alert == "Задача уже принята в работу."
    assert await event_types(h, task.id) == [EventType.CREATED, EventType.ACCEPTED]


async def test_ai_other_variant_is_shown_escaped_and_accepted(app, monkeypatch):
    """«🔁 Другой вариант»: AI просят переформулировать; спецсимволы в ответе AI не ломают сообщение.

    Во втором варианте нет числа — после «Принять» бот спрашивает плановое число.
    """
    h = app
    second = "Подготовить отчёт <Анализ договоров> & реестр нарушений по каждому договору"
    ai = FakeGemini(monkeypatch, ai_answer(AI_RESULT, 100, "договоров"), ai_answer(second))
    await team(h)
    await reach_result_step(h)
    await h.send_text(MGR, TZ_RESULT)

    log = await h.press_button(MGR, "Другой вариант")
    assert "⏳ Формулирую другой вариант…" in log.text
    assert len(ai.prompts) == 2
    assert f"Предыдущий вариант: {AI_RESULT}" in ai.prompts[1]
    shown = h.last_text(MGR)
    assert second in shown and AI_RESULT not in shown
    assert "📊 План" not in shown

    await h.press_button(MGR, "Принять")
    assert "Плановое число" in h.last_text(MGR)
    await h.send_text(MGR, "100 договоров")
    assert "📊 План: 100 договоров" in h.last_text(MGR)
    assert "шаг 4 из 6" in h.last_text(MGR)

    await h.press_button(MGR, "Завтра")
    await h.press_button(MGR, "Средний")
    await h.press_button(MGR, "20 %")
    await h.press_button(MGR, "Создать")
    [task] = await all_tasks(h)
    assert task.expected_result == second
    assert (task.plan_value, task.plan_unit) == (100, "договоров")


async def test_ai_unavailable_on_retry_falls_back_to_manager_words(app, monkeypatch):
    """Бесплатный лимит Gemini кончился на «Другом варианте» — бот честно говорит, что AI недоступен,
    и показывает подсказку по правилам из слов начальника (без служебного «Предыдущий вариант»)."""
    h = app
    FakeGemini(monkeypatch, ai_answer(AI_RESULT, 100, "договоров"), AIUnavailable("429 quota"))
    await team(h)
    await reach_result_step(h)
    await h.send_text(MGR, TZ_RESULT)

    await h.press_button(MGR, "Другой вариант")
    shown = h.last_text(MGR)
    assert "⚠️ AI сейчас недоступен" in shown
    assert "📐 Подсказка (без AI):" in shown
    assert "Проверить 100 договоров и представить отчёт" in shown
    assert "Предыдущий вариант" not in shown
    assert "✏️ Свой вариант" in h.buttons(MGR)

    await h.press_button(MGR, "Принять")
    assert "шаг 4 из 6" in h.last_text(MGR)  # план 100 договоров взят правилами


async def test_manager_types_own_wording(app, monkeypatch):
    """«✏️ Свой вариант»: бот показывает подсказку для копирования, слишком длинный текст не принимает,
    своё число из формулировки становится планом."""
    h = app
    FakeGemini(monkeypatch, ai_answer(AI_RESULT, 100, "договоров"))
    await team(h)
    await reach_result_step(h)
    await h.send_text(MGR, TZ_RESULT)

    await h.press_button(MGR, "Свой вариант")
    prompt = h.last_text(MGR)
    assert "Введите свою формулировку" in prompt
    assert f"Вариант-подсказка (нажмите, чтобы скопировать, и поправьте):\n{AI_RESULT}" in prompt
    assert h.buttons(MGR) == ["✖️ Отмена"]

    await h.send_text(MGR, "x" * 1001)
    assert "⚠️ Слишком длинно (1001 симв.)" in h.last_text(MGR)

    own = "Проверить 50 договоров аренды и сдать реестр в Excel"
    await h.send_text(MGR, own)
    assert f"🎯 Ожидаемый результат: {own}" in h.last_text(MGR)
    assert "шаг 4 из 6" in h.last_text(MGR)

    await h.press_button(MGR, "Завтра")
    await h.press_button(MGR, "Средний")
    await h.press_button(MGR, "20 %")
    await h.press_button(MGR, "Создать")
    [task] = await all_tasks(h)
    assert task.expected_result == own
    assert (task.plan_value, task.plan_unit) == (50, "договоров")
    assert task.description == TZ_RESULT


async def test_keep_own_words_without_number_asks_plan_number(app, monkeypatch):
    """«📝 Оставить как написал» без числа → бот просит плановое число; мусор, ноль и огромные числа —
    переспрос; «12 отчётов» — план 12 и единица «отчётов»."""
    h = app
    FakeGemini(monkeypatch, ai_answer("Подготовить 5 отчётов по филиалам", 5, "отчётов"))
    await team(h)
    await reach_result_step(h, title="Отчёты по филиалам")
    raw = "подготовить отчёты по всем филиалам"
    await h.send_text(MGR, raw)

    await h.press_button(MGR, "Оставить как написал")
    assert f"🎯 Ожидаемый результат: {raw}" in h.last_text(MGR)
    assert "Плановое число" in h.last_text(MGR)
    assert h.buttons(MGR) == ["⏭ Пропустить", "✖️ Отмена"]

    await h.send_text(MGR, "много")
    assert "⚠️ Не понял число" in h.last_text(MGR)
    await h.send_text(MGR, "0")
    assert "⚠️ Плановое число должно быть больше нуля." in h.last_text(MGR)
    await h.send_text(MGR, "9" * 30)
    assert "⚠️ Слишком большое число" in h.last_text(MGR)
    await h.send_text(MGR, "12 отчётов")
    assert "📊 План: 12 отчётов" in h.last_text(MGR)
    assert "шаг 4 из 6" in h.last_text(MGR)

    await h.press_button(MGR, "Завтра")
    await h.press_button(MGR, "Низкий")
    await h.press_button(MGR, "10 %")
    await h.press_button(MGR, "Создать")
    [task] = await all_tasks(h)
    assert task.expected_result == raw
    assert task.description is None  # формулировка совпадает со словами начальника
    assert (task.plan_value, task.plan_unit) == (12, "отчётов")
    assert task.priority == Priority.LOW


async def test_rules_path_without_ai(app):
    """AI выключен (нет ключа): бот подсказывает по правилам и советует добавить число;
    «Другой вариант» объясняет, что AI недоступен; план можно пропустить."""
    h = app
    await team(h)
    await reach_result_step(h, title="Отчёт о нарушениях")
    await h.send_text(MGR, "подготовить отчёт о нарушениях")
    shown = h.last_text(MGR)
    assert "📐 Подсказка (без AI):" in shown
    assert "Подготовить отчёт о нарушениях" in shown
    assert "💡 Добавьте число или критерий приёмки" in shown

    await h.press_button(MGR, "Другой вариант")
    assert "⚠️ AI сейчас недоступен" in h.last_text(MGR)

    await h.press_button(MGR, "Принять")
    assert "Плановое число" in h.last_text(MGR)
    await h.press_button(MGR, "Пропустить")
    assert "📊 Без числового плана." in h.last_text(MGR)
    await h.press_button(MGR, "Завтра")
    await h.press_button(MGR, "Средний")
    await h.press_button(MGR, "15 %")
    assert "📊 План" not in h.last_text(MGR)
    await h.press_button(MGR, "Создать")
    [task] = await all_tasks(h)
    assert task.expected_result == "Подготовить отчёт о нарушениях"
    assert task.plan_value is None and task.plan_unit is None
    assert task.description is None


# --- Срок ----------------------------------------------------------------------------------


# Дата для форматов «05.10 18:00» и «5 октября» — через 10 дней от сегодняшнего (по Ташкенту): тест не
# зависит от того, в какой день и час его запускают (фиксированная дата в какой-то момент станет прошлой).
_SOON = to_local(utcnow()).date() + timedelta(days=10)


@pytest.mark.parametrize(
    "text", ["завтра", "через 3 дня", f"{_SOON:%d.%m} 18:00", f"{_SOON.day} {MONTHS_GEN[_SOON.month - 1]}"]
)
async def test_deadline_typed_in_words_is_understood(app, text):
    """Срок можно написать словами: «завтра», «через 3 дня», «15.10 18:00», «15 октября»."""
    h = app
    expected = parse_deadline(text)
    if expected is None:
        pytest.skip(f"«{text}» сейчас уже в прошлом")
    await team(h)
    await reach_summary(h, deadline=text)
    assert f"📅 Срок: {fmt_deadline(expected)}" in h.last_text(MGR)
    await h.press_button(MGR, "Создать")
    [task] = await all_tasks(h)
    assert task.deadline == expected


async def test_unclear_or_past_deadline_is_asked_again(app):
    """Непонятный или прошедший срок не принимается: бот переспрашивает с примерами."""
    h = app
    await team(h)
    await reach_result_step(h)
    await h.send_text(MGR, TZ_RESULT)
    await h.press_button(MGR, "Принять")

    for wrong in ("когда-нибудь потом", "01.01.2020", "32.13"):
        await h.send_text(MGR, wrong)
        text = h.last_text(MGR)
        assert "⚠️ Не удалось распознать срок или он уже прошёл" in text, wrong
        assert "«05.10», «5 октября 18:00», «завтра», «через 3 дня»" in text
        assert await h.get_state(MGR) == "CreateTaskSG:deadline"
        assert h.has_button(MGR, "Завтра")

    # Опечатка в годе: срок через тысячи лет не принимается (и не ломает расчёт загрузки недели).
    for far in ("31.12.9999", "5 октября 2045"):
        await h.send_text(MGR, far)
        assert "⚠️ Срок слишком далеко. Укажите дату не дальше чем на 5 лет вперёд" in h.last_text(MGR), far
        assert await h.get_state(MGR) == "CreateTaskSG:deadline"
    log = await h.press(MGR, PickCB(field="deadline", value="9999-12-31"))
    assert log.answers and "⚠️ Срок слишком далеко" in h.last_text(MGR)

    # Подделанная/устаревшая кнопка с прошедшей датой.
    log = await h.press(MGR, PickCB(field="deadline", value="2020-01-01"))
    assert log.answers
    assert "⚠️ Этот срок уже прошёл — выберите другой." in h.last_text(MGR)

    await h.press_button(MGR, "Завтра")
    assert f"📅 Срок: {fmt_deadline(tomorrow_deadline())}" in h.last_text(MGR)
    assert await h.get_state(MGR) == "CreateTaskSG:priority"


async def test_deadline_quick_buttons_match_their_dates(app):
    """Быстрые кнопки срока («Завтра, 03.10», «Пятница, 09.10», «Конец месяца, 31.10» …): дата на кнопке
    совпадает с той, что попадёт в задачу; время — по умолчанию 18:00."""
    h = app
    await team(h)
    await reach_result_step(h)
    await h.send_text(MGR, TZ_RESULT)
    await h.press_button(MGR, "Принять")

    quick = [b for b in h.last_message(MGR).buttons if PickCB.unpack(b.callback_data).field == "deadline"]
    labels = [b.text.split(",")[0] for b in quick]
    assert "Завтра" in labels and "Сегодня" not in labels[1:]
    for button in quick:
        day = iso_to_deadline(PickCB.unpack(button.callback_data).value)
        assert button.text.endswith(to_local(day).strftime("%d.%m")), button.text
    assert len({b.callback_data for b in quick}) == len(quick)  # без повторов дат

    furthest = quick[-1]  # обычно «Конец месяца»
    expected = iso_to_deadline(PickCB.unpack(furthest.callback_data).value)
    await h.press(MGR, furthest.callback_data)
    assert f"📅 Срок: {fmt_deadline(expected)}" in h.last_text(MGR)
    await h.press_button(MGR, "Средний")
    await h.press_button(MGR, "10 %")
    await h.press_button(MGR, "Создать")
    [task] = await all_tasks(h)
    assert task.deadline == expected and to_local(task.deadline).strftime("%H:%M") == "18:00"


async def test_deadline_expired_while_summary_was_open(app):
    """Сводка провисела, срок за это время прошёл: «Создать» не создаёт задачу в прошлом,
    а просит новый срок и возвращает к сводке."""
    h = app
    await team(h)
    await reach_summary(h)
    await set_fsm(h, MGR, deadline=(utcnow() - timedelta(minutes=5)).isoformat())

    log = await h.press_button(MGR, "Создать")
    assert log.alert == "Срок уже прошёл — укажите новый."
    assert "⚠️ Указанный срок уже прошёл — выберите новый." in h.last_text(MGR)
    assert await all_tasks(h) == []

    await h.press_button(MGR, "Завтра")
    assert "✔️ Черновик обновлён." in h.last_text(MGR)
    await h.press_button(MGR, "Создать")
    [task] = await all_tasks(h)
    assert task.deadline == tomorrow_deadline()


# --- Приоритет и вес -----------------------------------------------------------------------


async def test_priority_can_be_typed(app):
    """Приоритет можно написать словом; непонятное слово — просьба выбрать кнопкой."""
    h = app
    await team(h)
    await reach_result_step(h)
    await h.send_text(MGR, TZ_RESULT)
    await h.press_button(MGR, "Принять")
    await h.press_button(MGR, "Завтра")

    await h.send_text(MGR, "срочно!!!")
    assert "👇 Выберите приоритет кнопкой." in h.last_text(MGR)
    await h.send_text(MGR, "низкий")
    assert "🚦 Приоритет: 🟢 Низкий" in h.last_text(MGR)
    assert await h.get_state(MGR) == "CreateTaskSG:weight"


async def test_weight_step_shows_week_load_and_validates_input(app):
    """На шаге веса видна загрузка недели сотрудника (без отменённых задач и задач коллег),
    варианты, после которых сумма превысит 100 %, помечены ⚠️; вес вне 1–100 не принимается."""
    h = app
    await team(h)
    deadline = tomorrow_deadline()
    await seed_task(h, EMP, "Подготовка ТЗ", deadline=deadline, weight=30)
    await seed_task(h, EMP, "Отчёт", deadline=deadline + timedelta(minutes=1), weight=40)
    await seed_task(h, EMP, "Отменённая", deadline=deadline, weight=50, status=TaskStatus.CANCELLED)
    await seed_task(h, EMP2, "Чужая", deadline=deadline, weight=50)

    await reach_result_step(h)
    await h.send_text(MGR, TZ_RESULT)
    await h.press_button(MGR, "Принять")
    await h.press_button(MGR, "Завтра")
    await h.press_button(MGR, "Средний")
    text = h.last_text(MGR)
    assert "Загрузка недели сотрудника: 70 %" in text
    assert "Рекомендуется, чтобы сумма весов за неделю была ≈100 %" in text
    buttons = h.buttons(MGR)
    assert "30 %" in buttons and "40 % ⚠️" in buttons and "50 % ⚠️" in buttons

    for wrong in ("150", "0", "12,5", "двадцать", "1e309", "-5"):
        await h.send_text(MGR, wrong)
        assert "⚠️ Вес — целое число от 1 до 100" in h.last_text(MGR), wrong
        assert await h.get_state(MGR) == "CreateTaskSG:weight"

    await h.send_text(MGR, "25 %")
    assert "⚖️ Вес: 25 %" in h.last_text(MGR)
    await h.press_button(MGR, "Создать")
    task = (await all_tasks(h))[-1]
    assert task.weight == 25


# --- Сводка и правка черновика ---------------------------------------------------------------


async def test_summary_lets_manager_change_every_field(app):
    """В сводке «✏️ Изменить» позволяет поправить каждое поле черновика; после каждого ввода —
    снова сводка с новым значением. Задача создаётся с исправленными данными и уходит новому исполнителю."""
    h = app
    _, _, emp2 = await team(h)
    await reach_summary(h)

    await h.press_button(MGR, "Изменить")
    assert "✏️ Что изменить?" in h.last_text(MGR)
    assert h.buttons(MGR) == ["Сотрудник", "Название", "Ожидаемый результат", "План (число)", "Срок",
                              "Приоритет", "Вес", "◀ К сводке", "✖️ Отмена"]

    async def change(field: str) -> str:
        await h.press_button(MGR, "Изменить")
        await h.press_button(MGR, field)
        text = h.last_text(MGR)
        assert "✏️ Изменение черновика задачи" in text
        return text

    # Сотрудник
    await h.press_button(MGR, "◀ К сводке")
    text = await change("Сотрудник")
    assert "Сейчас: Иванов Иван Иванович" in text
    await h.press_button(MGR, "Сидоров")
    assert "✔️ Черновик обновлён." in h.last_text(MGR)
    assert "👤 Исполнитель: Сидоров Пётр Ильич" in h.last_text(MGR)

    # Название
    text = await change("Название")
    assert f"Сейчас: {TZ_TITLE}" in text
    await h.send_text(MGR, "Анализ договоров аренды")
    assert "📌 Задача: Анализ договоров аренды" in h.last_text(MGR)

    # Ожидаемый результат — снова через подсказку
    await change("Ожидаемый результат")
    await h.send_text(MGR, "проверить 40 договоров аренды")
    assert "📐 Подсказка (без AI):" in h.last_text(MGR)
    assert "Изменение черновика" in h.last_text(MGR)
    await h.press_button(MGR, "Принять")
    summary = h.last_text(MGR)
    assert "Проверить 40 договоров аренды" in summary and "📊 План: 40 договоров" in summary

    # План
    text = await change("План (число)")
    assert "Сейчас: 40 договоров («⏭ Пропустить» — убрать план)" in text
    await h.send_text(MGR, "45 договоров")
    assert "📊 План: 45 договоров" in h.last_text(MGR)

    # Срок
    text = await change("Срок")
    assert f"Сейчас: {fmt_deadline(tomorrow_deadline())}" in text
    new_deadline = parse_deadline("через 3 дня")
    await h.send_text(MGR, "через 3 дня")
    assert f"📅 Срок: {fmt_deadline(new_deadline)}" in h.last_text(MGR)

    # Приоритет
    text = await change("Приоритет")
    assert "Сейчас: 🟡 Средний" in text
    await h.press_button(MGR, "Высокий")
    assert "⚡ Приоритет: 🔴 Высокий" in h.last_text(MGR)

    # Вес
    text = await change("Вес")
    assert "Сейчас: 20 %" in text
    await h.send_text(MGR, "35")
    assert "⚖️ Вес: 35 %" in h.last_text(MGR)

    log = await h.press_button(MGR, "Создать")
    [task] = await all_tasks(h)
    assert task.assignee_id == emp2.id
    assert task.title == "Анализ договоров аренды"
    assert task.expected_result == "Проверить 40 договоров аренды"
    assert (task.plan_value, task.plan_unit) == (45, "договоров")
    assert task.deadline == new_deadline
    assert (task.priority, task.weight) == (Priority.HIGH, 35)
    # Уведомление — новому исполнителю, первому ничего не пришло.
    assert "🆕 Вам поставлена новая задача" in log.to(EMP2).text
    assert not log.to(EMP).texts


async def test_plan_can_be_removed_on_summary(app):
    """В режиме правки «⏭ Пропустить» на шаге плана убирает число из черновика."""
    h = app
    await team(h)
    await reach_summary(h)
    assert "📊 План: 100 договоров" in h.last_text(MGR)
    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "План")
    await h.press_button(MGR, "Пропустить")
    assert "📊 План" not in h.last_text(MGR)
    await h.press_button(MGR, "Создать")
    [task] = await all_tasks(h)
    assert task.plan_value is None and task.plan_unit is None


# --- Неудачные пути --------------------------------------------------------------------------


async def test_no_active_employees(app):
    """Нет ни одного подтверждённого сотрудника — бот объясняет, что сначала нужно их подтвердить."""
    h = app
    await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    await h.seed_user(EMP, "Иванов Иван Иванович", status="pending")
    await h.send_command(MGR, "start")
    await h.press_menu(MGR, BTN_NEW_TASK)
    assert "Пока нет активных сотрудников" in h.last_text(MGR)
    assert "Сначала подтвердите сотрудников (👥 Сотрудники)" in h.last_text(MGR)
    assert await h.get_state(MGR) is None


async def test_employee_cannot_set_tasks(app):
    """Сотрудник не может ставить задачи: /new не открывает мастер, кнопок мастера ему не показывают."""
    h = app
    await team(h)
    log = await h.send_command(EMP, "new")
    assert "Новая задача" not in log.text
    assert await h.get_state(EMP) is None
    assert BTN_NEW_TASK not in h.reply_keyboard(EMP)


async def test_manager_demoted_mid_dialog_loses_access(app):
    """Начальника понизили до сотрудника посреди диалога — следующий ввод отклоняется, диалог закрыт."""
    h = app
    await team(h)
    await reach_result_step(h)
    async with h.db() as s:
        mgr = await s.scalar(select(User).where(User.tg_id == MGR))
        mgr.role = "employee"
        await s.commit()
    log = await h.send_text(MGR, TZ_RESULT)
    assert log.text == NO_RIGHTS
    assert await h.get_state(MGR) is None
    assert await all_tasks(h) == []


async def test_text_sticker_and_long_title_mid_dialog(app):
    """Текст вместо кнопки, стикер вместо текста, слишком длинное название — бот подсказывает и
    повторяет вопрос, диалог не теряется."""
    h = app
    await team(h)
    await h.press_menu(MGR, BTN_NEW_TASK)

    await h.send_text(MGR, "Иванову")
    assert "👇 Выберите вариант кнопкой" in h.last_text(MGR)
    assert h.has_button(MGR, "Иванов И. И.")
    await h.press_button(MGR, "Иванов")

    await h.send_sticker(MGR)
    assert "⚠️ Пожалуйста, ответьте текстом или кнопкой." in h.last_text(MGR)
    assert await h.get_state(MGR) == "CreateTaskSG:title"

    await h.send_text(MGR, "Анализ " * 40)
    assert "⚠️ Слишком длинное название" in h.last_text(MGR)
    assert await h.get_state(MGR) == "CreateTaskSG:title"

    await h.send_text(MGR, "Отчёт <ООО «Ромашка»> & Co")
    await h.send_text(MGR, "сверить 10 актов <б/н> & подписать")
    assert "Вы написали: сверить 10 актов <б/н> & подписать" in h.last_text(MGR)
    await h.press_button(MGR, "Принять")
    await h.press_button(MGR, "Завтра")
    await h.press_button(MGR, "Средний")
    await h.press_button(MGR, "10 %")
    assert "📌 Задача: Отчёт <ООО «Ромашка»> & Co" in h.last_text(MGR)
    await h.press_button(MGR, "Создать")
    [task] = await all_tasks(h)
    assert task.title == "Отчёт <ООО «Ромашка»> & Co"
    assert "Отчёт <ООО «Ромашка»> & Co" in h.last_text(EMP)


async def test_cancel_mid_dialog_and_dead_buttons(app):
    """«✖️ Отмена» на сводке закрывает диалог; старая кнопка «Создать» после этого задачу не создаёт."""
    h = app
    await team(h)
    await reach_summary(h)
    summary_id = h.last_message(MGR).message_id
    create_data = h.find_button(MGR, "Создать")

    log = await h.press_button(MGR, "Отмена")
    assert "Действие отменено." in log.text
    assert await h.get_state(MGR) is None
    assert h.buttons(MGR, summary_id) == []  # кнопки сводки убраны

    log = await h.press(MGR, create_data, summary_id)
    assert log.answers and log.alert
    assert await all_tasks(h) == []


async def test_buttons_of_old_steps_do_not_change_draft(app):
    """Кнопки из старых сообщений (предыдущий шаг, брошенный черновик) не меняют текущий черновик."""
    h = app
    await team(h)
    await reach_result_step(h)
    await h.send_text(MGR, TZ_RESULT)
    await h.press_button(MGR, "Принять")
    deadline_prompt = h.last_message(MGR).message_id
    tomorrow = h.find_button(MGR, "Завтра")
    await h.send_text(MGR, "через 3 дня")
    # Ответили текстом — у вопроса о сроке кнопки убраны, чтобы по ним не нажимали.
    assert h.buttons(MGR, deadline_prompt) == []

    # Но у клиента Telegram кнопки могли остаться (не обновился экран) — нажатие ничего не меняет.
    log = await h.press(MGR, tomorrow, deadline_prompt)
    assert STALE in log.alert
    assert await h.get_state(MGR) == "CreateTaskSG:priority"

    await h.press_button(MGR, "Средний")
    await h.press_button(MGR, "20 %")
    summary = h.last_message(MGR).message_id
    create = h.find_button(MGR, "Создать")
    # В режиме правки снова шаг срока — старый вопрос о сроке всё равно неактуален.
    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "Срок")
    log = await h.press(MGR, tomorrow, deadline_prompt)
    assert STALE in log.alert
    assert await h.get_state(MGR) == "CreateTaskSG:deadline"
    assert f"Сейчас: {fmt_deadline(parse_deadline('через 3 дня'))}" in h.last_text(MGR)

    # Начали задачу заново — кнопки брошенного черновика убраны, «Создать» из него не срабатывает.
    await h.press_menu(MGR, BTN_NEW_TASK)
    assert h.buttons(MGR, summary) == []
    log = await h.press(MGR, create, summary)
    assert STALE in log.alert
    assert await all_tasks(h) == []


async def test_assignee_blocked_before_create(app):
    """Сотрудника заблокировали, пока начальник заполнял черновик: создать нельзя, бот объясняет;
    после смены исполнителя задача создаётся. Недоступного сотрудника нельзя выбрать и кнопкой."""
    h = app
    _, emp, emp2 = await team(h)
    await reach_summary(h)
    async with h.db() as s:
        (await s.get(User, emp.id)).status = UserStatus.BLOCKED
        await s.commit()

    log = await h.press_button(MGR, "Создать")
    assert log.alert == "Исполнителем может быть только активный сотрудник"
    assert await all_tasks(h) == []
    assert await h.get_state(MGR) == "CreateTaskSG:confirm"

    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "Сотрудник")
    assert not h.has_button(MGR, "Иванов И. И.")
    # Подделанные кнопки: заблокированный сотрудник и несуществующий огромный id.
    for value in (str(emp.id), "9" * 20):
        await h.press(MGR, PickCB(field="assignee", value=value))
        assert "⚠️ Этот сотрудник сейчас недоступен — выберите другого." in h.last_text(MGR), value
    await h.press_button(MGR, "Сидоров")
    log = await h.press_button(MGR, "Создать")
    [task] = await all_tasks(h)
    assert task.assignee_id == emp2.id
    assert "🆕 Вам поставлена новая задача" in log.to(EMP2).text


async def test_double_press_create_makes_one_task(app):
    """Двойное нажатие «✅ Создать» — одна задача и одно уведомление сотруднику."""
    h = app
    await team(h)
    await reach_summary(h)
    summary_id = h.last_message(MGR).message_id
    data = h.find_button(MGR, "Создать")
    await h.press(MGR, data, summary_id)
    log = await h.press(MGR, data, summary_id)
    assert log.answers
    assert len(await all_tasks(h)) == 1
    assert sum("Вам поставлена новая задача" in text for text in h.sent_to(EMP)) == 1


async def test_employee_who_blocked_bot_still_gets_task(app):
    """Сотрудник заблокировал бота — задача всё равно создаётся, начальник видит подтверждение."""
    h = app
    await team(h)
    h.api.blocked_chats.add(EMP)
    await reach_summary(h)
    await h.press_button(MGR, "Создать")
    assert h.last_text(MGR).startswith("✅ Задача #1 поставлена")
    assert len(await all_tasks(h)) == 1


# =============================================================================================
# 2. Списки задач и карточка
# =============================================================================================


def task_buttons(h: BotHarness, chat_id: int) -> list[str]:
    return [text for text in h.buttons(chat_id) if "#" in text]


async def test_employee_sees_only_own_tasks_by_tabs(app):
    """«📋 Мои задачи»: вкладки В работе / Просрочены / На проверке / Выполнены / Все;
    только свои задачи, отменённые в «Все» не попадают."""
    h = app
    await team(h)
    active = await seed_task(h, EMP, "Подготовка ТЗ", days=3)
    overdue = await seed_task(h, EMP, "Работа с поставщиками", overdue=True)
    review = await seed_task(h, EMP, "Отчёт за месяц", status=TaskStatus.SUBMITTED)
    done = await seed_task(h, EMP, "Анализ договоров", status=TaskStatus.DONE, score=110)
    rework = await seed_task(h, EMP, "Отчёт о нарушениях", days=5, status=TaskStatus.REWORK)
    await seed_task(h, EMP, "Отменённая задача", status=TaskStatus.CANCELLED)
    await seed_task(h, EMP2, "Задача коллеги")

    await h.press_menu(EMP, BTN_MY_TASKS)
    text = h.last_text(EMP)
    assert text.startswith("📋 Мои задачи — В работе (3)")
    assert f"#{active} Подготовка ТЗ" in text and f"#{overdue} Работа с поставщиками" in text
    assert f"↩️ #{rework} Отчёт о нарушениях" in text  # возвращённая на доработку — тоже «в работе»
    assert "просрочено на" in text
    assert "Задача коллеги" not in text and "👤" not in text
    assert len(task_buttons(h, EMP)) == 3
    assert {"• В работе", "Просрочены", "На проверке", "Выполнены", "Все"} <= set(h.buttons(EMP))

    await h.press_button(EMP, "Просрочены")
    text = h.last_text(EMP)
    assert text.startswith("📋 Мои задачи — Просрочены (1)") and f"#{overdue}" in text
    assert "• Просрочены" in h.buttons(EMP)

    await h.press_button(EMP, "На проверке")
    text = h.last_text(EMP)
    assert text.startswith("📋 Мои задачи — На проверке (1)") and f"#{review}" in text and "сдано" in text

    await h.press_button(EMP, "Выполнены")
    text = h.last_text(EMP)
    assert text.startswith("📋 Мои задачи — Выполнены (1)") and f"#{done}" in text and "110 %" in text

    await h.press_button(EMP, "Все")
    text = h.last_text(EMP)
    assert text.startswith("📋 Мои задачи — Все (5)")
    assert "Отменённая задача" not in text and "Задача коллеги" not in text
    # Повторное нажатие на открытую вкладку ничего не ломает.
    log = await h.press_button(EMP, "• Все")
    assert log.answers and h.last_text(EMP) == text

    # Коллега видит только свою задачу.
    await h.press_menu(EMP2, BTN_MY_TASKS)
    assert h.last_text(EMP2).startswith("📋 Мои задачи — В работе (1)")
    assert "Задача коллеги" in h.last_text(EMP2)


async def test_manager_sees_all_tasks_with_assignee_and_pages(app):
    """«📋 Задачи» у начальника: все задачи с исполнителем, по 8 на страницу с листанием;
    во вкладке «Все» видны и отменённые."""
    h = app
    await team(h)
    ids = [await seed_task(h, EMP if i % 2 else EMP2, f"Задача {i}", days=i + 1) for i in range(10)]
    cancelled = await seed_task(h, EMP, "Отменённая задача", days=30, status=TaskStatus.CANCELLED)

    await h.press_menu(MGR, BTN_TASKS)
    text = h.last_text(MGR)
    assert text.startswith("📋 Задачи — В работе (10)")
    assert "Страница 1 из 2" in text
    assert "👤 Иванов И. И." in text and "👤 Сидоров П. И." in text
    first_page = task_buttons(h, MGR)
    assert len(first_page) == 8
    assert first_page[0].startswith(f"🔄 #{ids[0]} Сидоров")  # ближайший срок — сверху
    assert "Вперёд ▶" in h.buttons(MGR) and "◀ Назад" not in h.buttons(MGR)

    await h.press_button(MGR, "Вперёд")
    text = h.last_text(MGR)
    assert "Страница 2 из 2" in text
    second_page = task_buttons(h, MGR)
    assert len(second_page) == 2 and not set(first_page) & set(second_page)
    assert "◀ Назад" in h.buttons(MGR) and "Вперёд ▶" not in h.buttons(MGR)

    # Страница за пределами списка (старая кнопка после удаления задач) — показывается последняя.
    await h.press(MGR, ListCB(scope="all", status="open", page=99))
    assert "Страница 2 из 2" in h.last_text(MGR)

    await h.press_button(MGR, "Все")
    text = h.last_text(MGR)
    assert text.startswith("📋 Задачи — Все (11)")
    await h.press_button(MGR, "Вперёд")
    assert f"#{cancelled}" in h.last_text(MGR) and "отменена" in h.last_text(MGR)


async def test_manager_opens_one_employee_tasks(app):
    """Список задач одного сотрудника (из его карточки): заголовок с фамилией и «◀ К карточке сотрудника»;
    сотрудник такой список открыть не может."""
    h = app
    _, emp, _ = await team(h)
    mine = await seed_task(h, EMP, "Подготовка ТЗ")
    await seed_task(h, EMP2, "Задача коллеги")

    await h.press(MGR, ListCB(scope="emp", status="all", user_id=emp.id))
    text = h.last_text(MGR)
    assert text.startswith("📋 Задачи: Иванов И. И. — Все (1)")
    assert f"#{mine}" in text and "Задача коллеги" not in text
    assert "◀ К карточке сотрудника" in h.buttons(MGR)

    log = await h.press(MGR, ListCB(scope="emp", status="all", user_id=9999))
    assert log.alert == "Сотрудник не найден."

    for scope, user_id in (("emp", emp.id), ("all", 0)):
        log = await h.press(EMP2, ListCB(scope=scope, status="all", user_id=user_id))
        assert log.alert == NO_RIGHTS
        assert "Подготовка ТЗ" not in log.text


async def test_task_card_differs_by_role_and_back_returns_to_tab(app):
    """Карточка задачи: начальник видит исполнителя и кнопки «Изменить/Отменить», исполнитель —
    «Принял в работу/Сдать результат». «◀ К списку задач» возвращает на ту же вкладку."""
    h = app
    await team(h)
    task_id = await seed_task(h, EMP, "Работа с поставщиками", overdue=True, plan_value=5, plan_unit="договоров")

    await h.press_menu(MGR, BTN_TASKS)
    await h.press_button(MGR, "Просрочены")
    await h.press_button(MGR, f"#{task_id}")
    card = h.last_text(MGR)
    assert card.startswith(f"📌 Задача #{task_id}: Работа с поставщиками")
    assert "👤 Исполнитель: Иванов И. И." in card
    assert "📊 План: 5 договоров" in card
    assert "📍 Статус: ⏰ Просрочена" in card
    assert h.buttons(MGR) == ["✏️ Изменить", "🚫 Отменить", "📜 История", "◀ К списку задач"]
    await h.press_button(MGR, "К списку задач")
    assert h.last_text(MGR).startswith("📋 Задачи — Просрочены (1)")

    await h.press_menu(EMP, BTN_MY_TASKS)
    await h.press_button(EMP, f"#{task_id}")
    card = h.last_text(EMP)
    assert "Исполнитель:" not in card and "🧑‍💼 Постановщик: Петрова А. С." in card
    assert h.buttons(EMP) == ["✅ Принял в работу", "📤 Сдать результат", "📜 История", "◀ К списку задач"]
    await h.press_button(EMP, "К списку задач")
    assert h.last_text(EMP).startswith("📋 Мои задачи — В работе (1)")


async def test_employee_cannot_open_or_change_foreign_task(app):
    """Сотрудник не может открыть, принять, посмотреть историю, изменить или отменить чужую задачу
    (даже подделав нажатие кнопки)."""
    h = app
    await team(h)
    task_id = await seed_task(h, EMP, "Анализ договоров")

    for action in ("open", "history", "accept", "edit", "cancel"):
        log = await h.press(EMP2, TaskCB(action=action, task_id=task_id))
        assert log.alert == NO_RIGHTS, action
        assert "Анализ договоров" not in log.text, action
    # Свою задачу сотрудник тоже не может изменить или отменить.
    for action in ("edit", "cancel"):
        log = await h.press(EMP, TaskCB(action=action, task_id=task_id))
        assert log.alert == NO_RIGHTS, action

    task = await h.get_task(task_id)
    assert task.accepted_at is None and task.status == TaskStatus.ACTIVE
    assert await event_types(h, task_id) == [EventType.CREATED]

    log = await h.press(EMP, TaskCB(action="open", task_id=999))
    assert log.alert == "Задача не найдена."
    # id за пределами БД (подделанная кнопка) — понятный ответ, а не «Произошла ошибка».
    for data in [f"t:{action}:{2**63}" for action in ("open", "history", "accept", "edit", "cancel")] + [
        f"l:emp:all:0:{2**63}",
    ]:
        log = await h.press(MGR, data)
        assert log.alert and "ошибка" not in log.alert.lower(), (data, log.alert)

    # Заблокированный сотрудник не открывает даже свою задачу (кнопка из старого уведомления).
    async with h.db() as s:
        (await s.scalar(select(User).where(User.tg_id == EMP))).status = UserStatus.BLOCKED
        await s.commit()
    log = await h.press(EMP, TaskCB(action="open", task_id=task_id))
    assert log.alert == NO_RIGHTS


async def test_task_history_shows_lifecycle(app):
    """«📜 История»: постановка, принятие, изменения — с датой и автором; «◀ К задаче» возвращает к карточке."""
    h = app
    await team(h)
    task_id = await seed_task(h, EMP, "Анализ договоров", weight=10)
    await h.press(EMP, TaskCB(action="accept", task_id=task_id))
    await h.press(MGR, TaskCB(action="open", task_id=task_id))
    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, "Вес")
    await h.press_button(MGR, "30 %")

    await h.press_button(MGR, "История")
    text = h.last_text(MGR)
    assert text.startswith(f"📜 История задачи #{task_id}")
    lines = text.splitlines()
    assert any("Петрова А. С.: задача поставлена" in line and "вес 10 %" in line for line in lines)
    assert any("Иванов И. И.: задача принята в работу" in line for line in lines)
    assert any("Петрова А. С.: изменено — вес: 10 % → 30 %" in line for line in lines)
    assert h.buttons(MGR) == ["◀ К задаче"]
    await h.press_button(MGR, "К задаче")
    assert h.last_text(MGR).startswith(f"📌 Задача #{task_id}")

    # Исполнитель тоже видит историю своей задачи.
    await h.press(EMP, TaskCB(action="history", task_id=task_id))
    assert "изменено — вес: 10 % → 30 %" in h.last_text(EMP)


# =============================================================================================
# 3. Правка и отмена задачи начальником
# =============================================================================================


async def open_edit(h: BotHarness, task_id: int, field: str) -> str:
    await h.press(MGR, TaskCB(action="open", task_id=task_id))
    await h.press_button(MGR, "Изменить")
    await h.press_button(MGR, field)
    return h.last_text(MGR)


async def test_manager_moves_deadline_and_employee_is_notified(app):
    """Начальник переносит срок (текстом, затем кнопкой): непонятный срок — переспрос,
    исполнитель получает «было → стало», изменение записано в журнал."""
    h = app
    await team(h)
    old = utcnow().replace(second=0, microsecond=0) + timedelta(days=2)
    task_id = await seed_task(h, EMP, "Анализ договоров", deadline=old)

    await h.press(MGR, TaskCB(action="open", task_id=task_id))
    await h.press_button(MGR, "Изменить")
    menu = h.last_text(MGR)
    assert f"✏️ Изменение задачи #{task_id}" in menu and "Исполнитель получит уведомление" in menu
    assert h.buttons(MGR) == ["Название", "Ожидаемый результат", "План (число)", "Срок", "Приоритет", "Вес",
                              "✖️ Отмена"]
    await h.press_button(MGR, "Срок")
    assert f"Сейчас: {fmt_deadline(old)}" in h.last_text(MGR)
    assert h.has_button(MGR, "Завтра")

    await h.send_text(MGR, "вчера вечером")
    assert "⚠️ Не понял срок или он уже прошёл." in h.last_text(MGR)
    new = parse_deadline("через 3 дня")
    log = await h.send_text(MGR, "через 3 дня")
    card = h.last_text(MGR)
    assert card.startswith("✅ Изменено: срок. Исполнитель получил уведомление.")
    assert f"📅 Срок: {fmt_deadline(new)}" in card
    changed = log.to(EMP).text
    assert "✏️ Начальник изменил задачу" in changed
    assert f"• Срок: {fmt_deadline(old)} → {fmt_deadline(new)}" in changed
    assert h.buttons(EMP) == ["📤 Сдать результат", "📋 Открыть"]
    assert (await h.get_task(task_id)).deadline == new
    assert await h.get_state(MGR) is None

    # Быстрая кнопка срока.
    await open_edit(h, task_id, "Срок")
    log = await h.press_button(MGR, "Завтра")
    assert log.answers
    assert (await h.get_task(task_id)).deadline == tomorrow_deadline()
    assert "• Срок:" in log.to(EMP).text
    assert await event_types(h, task_id) == [EventType.CREATED, EventType.EDITED, EventType.EDITED]

    # Опечатка в годе — переспрос, срок прежний.
    await open_edit(h, task_id, "Срок")
    await h.send_text(MGR, "31.12.9999")
    assert "⚠️ Срок слишком далеко" in h.last_text(MGR)
    assert (await h.get_task(task_id)).deadline == tomorrow_deadline()
    await h.press_button(MGR, "Отмена")

    # Прошедшая дата на кнопке (сообщение провисело) — срок не меняется, вопрос повторяется.
    await open_edit(h, task_id, "Срок")
    log = await h.press(MGR, PickCB(field="deadline", value="2020-01-01"))
    assert "⚠️ Срок должен быть в будущем" in h.last_text(MGR)
    assert not log.to(EMP).texts
    assert (await h.get_task(task_id)).deadline == tomorrow_deadline()


async def test_manager_changes_weight_with_week_load_hint(app):
    """Правка веса: видно, сколько уже набрано на неделе срока; вес вне 1–100 — переспрос."""
    h = app
    await team(h)
    deadline = utcnow() + timedelta(days=2)
    task_id = await seed_task(h, EMP, "Анализ договоров", deadline=deadline, weight=10)
    await seed_task(h, EMP, "Подготовка ТЗ", deadline=deadline, weight=50)

    text = await open_edit(h, task_id, "Вес")
    assert "Сейчас: 10 %" in text
    assert "Другие задачи сотрудника на неделе срока: 50 % (вместе с этой — 60 %)" in text
    assert "50 % ⚠️" not in h.buttons(MGR) and "50 %" in h.buttons(MGR)  # 50 + 50 = 100 — ещё можно

    for wrong in ("200", "1e309", "12,5"):
        await h.send_text(MGR, wrong)
        assert "⚠️ Вес — целое число от 1 до 100, например: 20." in h.last_text(MGR), wrong
    assert (await h.get_task(task_id)).weight == 10
    log = await h.press_button(MGR, "40 %")
    assert h.last_text(MGR).startswith("✅ Изменено: вес.")
    assert "• Вес: 10 % → 40 %" in log.to(EMP).text
    assert (await h.get_task(task_id)).weight == 40


async def test_manager_rewrites_result_and_plan(app):
    """Правка ожидаемого результата и плана: исполнитель видит было → стало; «🗑 Убрать план»;
    то же самое значение — «Ничего не изменилось», исполнителя не беспокоим."""
    h = app
    await team(h)
    task_id = await seed_task(h, EMP, "Анализ договоров", plan_value=100, plan_unit="договоров")

    text = await open_edit(h, task_id, "Ожидаемый результат")
    assert "Сейчас: Анализ договоров: результат" in text
    new_result = "Проверить 120 договоров и представить отчёт в Excel"
    log = await h.send_text(MGR, new_result)
    assert h.last_text(MGR).startswith("✅ Изменено: ожидаемый результат.")
    assert f"• Ожидаемый результат: «Анализ договоров: результат» → «{new_result}»" in log.to(EMP).text

    text = await open_edit(h, task_id, "План")
    assert "Сейчас: 100 договоров" in text
    await h.send_text(MGR, "сто двадцать")
    assert "⚠️ Не нашёл подходящего положительного числа" in h.last_text(MGR)
    log = await h.send_text(MGR, "120 договоров")
    assert h.last_text(MGR).startswith("✅ Изменено: план.")
    assert "📊 План: 120 договоров" in h.last_text(MGR)
    assert "• План: 100 договоров → 120 договоров" in log.to(EMP).text  # с единицей, не голые числа
    task = await h.get_task(task_id)
    assert (task.expected_result, task.plan_value, task.plan_unit) == (new_result, 120, "договоров")

    await open_edit(h, task_id, "План")
    log = await h.press_button(MGR, "Убрать план")
    assert h.last_text(MGR).startswith("✅ Изменено: план, единица плана.")
    assert "• План: 120 договоров → —" in log.to(EMP).text
    assert "Единица плана" not in log.to(EMP).text  # единица уже показана в строке плана
    task = await h.get_task(task_id)
    assert task.plan_value is None and task.plan_unit is None

    events_before = await event_types(h, task_id)
    await open_edit(h, task_id, "Приоритет")
    assert "Сейчас: 🟡 Средний" in h.last_text(MGR)
    log = await h.press_button(MGR, "Средний")
    assert h.last_text(MGR).startswith("Ничего не изменилось")
    assert not log.to(EMP).texts
    assert await event_types(h, task_id) == events_before


async def test_finished_task_cannot_be_edited(app):
    """Задачу на проверке изменить нельзя: в карточке нет «Изменить», подделанная кнопка — отказ;
    если исполнитель сдал результат, пока начальник правил, правка не применяется."""
    h = app
    _, emp, _ = await team(h)
    submitted = await seed_task(h, EMP, "Отчёт за месяц", status=TaskStatus.SUBMITTED)
    await h.press(MGR, TaskCB(action="open", task_id=submitted))
    assert "✏️ Изменить" not in h.buttons(MGR) and "🔍 Проверить" in h.buttons(MGR)
    log = await h.press(MGR, TaskCB(action="edit", task_id=submitted))
    assert log.alert == "Изменить можно только задачу в работе или на доработке."

    task_id = await seed_task(h, EMP, "Анализ договоров")
    await open_edit(h, task_id, "Название")
    async with h.db() as s:
        await tasks_svc.submit_result(s, task_id, await s.get(User, emp.id), fact_text="Готово")
        await s.commit()
    log = await h.send_text(MGR, "Новое название")
    assert log.text == "Задачу уже нельзя изменить — её статус изменился."
    assert (await h.get_task(task_id)).title == "Анализ договоров"
    assert await h.get_state(MGR) is None


async def test_manager_cancels_task_with_reason(app):
    """Отмена задачи с причиной: подтверждение, причина, карточка «Отменена»; исполнитель получает
    уведомление с причиной, задача пропадает из его «В работе», старая кнопка «Принял» не работает."""
    h = app
    await team(h)
    task_id = await seed_task(h, EMP, "Анализ договоров")
    await h.press_menu(EMP, BTN_MY_TASKS)
    assert "Анализ договоров" in h.last_text(EMP)

    await h.press(MGR, TaskCB(action="open", task_id=task_id))
    await h.press_button(MGR, "Отменить")
    text = h.last_text(MGR)
    assert f"🚫 Отменить задачу #{task_id}?" in text and "Исполнитель: Иванов И. И." in text
    assert h.buttons(MGR) == ["🚫 Да, отменить задачу", "◀ Нет, не отменять"]

    await h.send_text(MGR, "да")
    assert "Выберите вариант кнопкой" in h.last_text(MGR)

    await h.press_button(MGR, "Да, отменить")
    assert "Напишите причину отмены" in h.last_text(MGR)
    assert h.buttons(MGR) == ["⏭ Без причины", "✖️ Отмена"]
    await h.send_text(MGR, "x" * 1001)
    assert "⚠️ Слишком длинно — до 1000 символов." in h.last_text(MGR)

    reason = "Договоры <передали> в другой отдел"
    log = await h.send_text(MGR, reason)
    card = h.last_text(MGR)
    assert card.startswith(f"🚫 Задача #{task_id} отменена. Исполнитель получил уведомление.")
    assert "📍 Статус: 🚫 Отменена" in card
    assert h.buttons(MGR) == ["📜 История", "◀ К списку задач"]
    assert log.alert is None  # ответ текстом, не нажатием
    emp_text = log.to(EMP).text
    assert "🚫 Задача отменена начальником" in emp_text
    assert f"💬 Причина: {reason}" in emp_text
    assert "Сдавать результат по ней не нужно." in emp_text

    task = await h.get_task(task_id)
    assert task.status == TaskStatus.CANCELLED
    events = await h.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id))
    assert events[-1].type == EventType.CANCELLED and events[-1].data["reason"] == reason

    await h.press_button(EMP, "В работе")
    assert h.last_text(EMP).startswith("📋 Мои задачи — В работе (0)")
    assert "Задач нет." in h.last_text(EMP)
    log = await h.press(EMP, TaskCB(action="accept", task_id=task_id))
    assert log.alert == "Задача уже не в работе."


async def test_cancel_can_be_declined_or_done_without_reason(app):
    """«◀ Нет, не отменять» возвращает карточку; «⏭ Без причины» отменяет без причины;
    повторная отмена и старые кнопки диалога ничего не делают."""
    h = app
    await team(h)
    task_id = await seed_task(h, EMP, "Анализ договоров")

    await h.press(MGR, TaskCB(action="open", task_id=task_id))
    await h.press_button(MGR, "Отменить")
    log = await h.press_button(MGR, "Нет, не отменять")
    assert log.alert == "Задача не отменена."
    assert h.last_text(MGR).startswith(f"📌 Задача #{task_id}")
    assert (await h.get_task(task_id)).status == TaskStatus.ACTIVE

    await h.press_button(MGR, "Отменить")
    confirm_id = h.last_message(MGR).message_id
    yes = h.find_button(MGR, "Да, отменить")
    await h.press(MGR, yes, confirm_id)
    log = await h.press_button(MGR, "Без причины")
    assert log.alert == "Задача отменена 🚫"
    assert "Причина" not in log.to(EMP).text and "🚫 Задача отменена начальником" in log.to(EMP).text
    assert (await h.get_task(task_id)).status == TaskStatus.CANCELLED

    log = await h.press(MGR, yes, confirm_id)
    assert log.answers and log.alert
    log = await h.press(MGR, TaskCB(action="cancel", task_id=task_id))
    assert log.alert == "Задачу уже нельзя отменить — она завершена или отменена."
    assert sum("отменена начальником" in text for text in h.sent_to(EMP)) == 1


async def test_employee_history_hides_ai_score_until_review(app):
    """Пока начальник не проверил результат, исполнитель не видит предварительную оценку AI —
    ни в карточке, ни в истории задачи. Начальник видит её в истории сразу."""
    h = app
    await team(h)
    task_id = await seed_task(h, EMP, "Анализ договоров", status=TaskStatus.SUBMITTED, score=87)

    await h.press(EMP, TaskCB(action="open", task_id=task_id))
    assert "87 %" not in h.last_text(EMP)
    await h.press(EMP, TaskCB(action="history", task_id=task_id))
    history = h.last_text(EMP)
    assert "результат сдан" in history
    assert "87 %" not in history and "оценка" not in history

    await h.press(MGR, TaskCB(action="history", task_id=task_id))
    assert "87 %" in h.last_text(MGR)


async def test_edit_by_text_priority_percent_plan_and_long_title(app):
    """Правка текстом: «невысокий» не превращается в «высокий» (бот переспрашивает), «низкий» принимается;
    план «95 %» сохраняет единицу «%»; слишком длинное название — отказ с объяснением."""
    h = app
    await team(h)
    task_id = await seed_task(h, EMP, "Доля проверенных договоров", plan_value=100, plan_unit="договоров")

    await open_edit(h, task_id, "Приоритет")
    await h.send_text(MGR, "невысокий")
    assert "⚠️ Выберите приоритет кнопкой." in h.last_text(MGR)
    assert (await h.get_task(task_id)).priority == Priority.MEDIUM
    await h.send_text(MGR, "низкий")
    assert h.last_text(MGR).startswith("✅ Изменено: приоритет.")
    assert (await h.get_task(task_id)).priority == Priority.LOW

    await open_edit(h, task_id, "План")
    await h.send_text(MGR, "95 %")
    assert "📊 План: 95 %" in h.last_text(MGR)
    task = await h.get_task(task_id)
    assert (task.plan_value, task.plan_unit) == (95, "%")

    await open_edit(h, task_id, "Название")
    await h.send_text(MGR, "Очень длинное название " * 20)
    assert "⚠️ Поле «Задача» слишком длинное (до 255 символов)" in h.last_text(MGR)
    assert await h.get_state(MGR) == "EditTaskSG:text"
    assert (await h.get_task(task_id)).title == "Доля проверенных договоров"


async def test_second_manager_cancelled_task_meanwhile(app):
    """Два начальника: второй начал отменять задачу, а первый уже отменил её — второй получает
    понятный отказ, исполнитель получает одно уведомление, изменить отменённую задачу нельзя."""
    h = app
    await team(h)
    await h.seed_user(MGR2, "Кузнецов Олег Петрович", role="manager")
    task_id = await seed_task(h, EMP, "Анализ договоров")

    await h.press(MGR2, TaskCB(action="open", task_id=task_id))
    await h.press_button(MGR2, "Отменить")
    assert "Отменить задачу" in h.last_text(MGR2)

    await h.press(MGR, TaskCB(action="open", task_id=task_id))
    await h.press_button(MGR, "Отменить")
    await h.press_button(MGR, "Да, отменить")
    await h.send_text(MGR, "Дубль задачи")

    log = await h.press_button(MGR2, "Да, отменить")
    assert log.alert == "Задачу уже нельзя отменить — она завершена или отменена."
    assert h.buttons(MGR2) == []
    assert await h.get_state(MGR2) is None
    assert sum("отменена начальником" in text for text in h.sent_to(EMP)) == 1

    await h.press(MGR2, TaskCB(action="open", task_id=task_id))
    assert "🚫 Отменена" in h.last_text(MGR2)
    assert "✏️ Изменить" not in h.buttons(MGR2)
    log = await h.press(MGR2, TaskCB(action="edit", task_id=task_id))
    assert log.alert == "Изменить можно только задачу в работе или на доработке."


async def test_weight_edit_of_task_with_absurd_deadline_does_not_crash(app):
    """Задача со сроком у границы календаря (31.12.9999, например из старых данных): правка веса
    открывается и сохраняется, а не падает с «Произошла ошибка» на расчёте загрузки недели."""
    h = app
    await team(h)
    task_id = await seed_task(h, EMP, "Вечная задача", weight=10)
    async with h.db() as s:
        (await s.get(Task, task_id)).deadline = datetime(9999, 12, 31, 13, 0)
        await s.commit()

    text = await open_edit(h, task_id, "Вес")
    assert "Сейчас: 10 %" in text
    await h.press_button(MGR, "20 %")
    assert (await h.get_task(task_id)).weight == 20


async def test_list_shows_short_title_with_special_characters_in_full(app):
    """Название короче 70 символов, но с «<», «>» и «&» (обычное дело: «ООО <…>», «№12 & №13»)
    видно в списке «📋 Мои задачи» целиком, без «…» (обрезка — по видимому тексту, а не по HTML)."""
    h = app
    await team(h)
    title = "Отчёт <ООО «Ромашка»> & Co по договорам №12 & №13 <срочно>"
    assert len(title) < 70
    task_id = await seed_task(h, EMP, title)
    await h.press_menu(EMP, BTN_MY_TASKS)
    assert f"#{task_id} {title} —" in h.last_text(EMP)


# --- Ограничение длины ожидаемого результата при правке ---------------------------------------------


async def test_edited_expected_result_has_the_same_length_limit_as_proposals(app):
    """Петрова правит ожидаемый результат задачи и по ошибке вставляет 2500 символов. Бот просит
    сократить до 2000 (как у поручений), результат не меняется, исполнителя не беспокоят;
    нормальный текст после этого принимается."""
    h = app
    await team(h)
    task_id = await seed_task(h, EMP, "Анализ договоров")
    before = (await h.get_task(task_id)).expected_result

    await open_edit(h, task_id, "Ожидаемый результат")
    log = await h.send_text(MGR, "Проверить договоры. " * 125)
    assert "Сократите до 2000 символов" in log.to(MGR).text
    assert not log.to(EMP).texts
    assert (await h.get_task(task_id)).expected_result == before

    log = await h.send_text(MGR, "Проверить 120 договоров и представить отчёт")
    assert h.last_text(MGR).startswith("✅ Изменено: ожидаемый результат.")
    assert (await h.get_task(task_id)).expected_result == "Проверить 120 договоров и представить отчёт"


# --- Бота остановили посреди запроса к AI -----------------------------------------------------------


class HangingGemini:
    """Gemini «думает» бесконечно: бота остановят посреди запроса."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.started = asyncio.Event()
        for module in (provider, formulate):
            monkeypatch.setattr(module, "ai_available", lambda: True)
            monkeypatch.setattr(module, "generate_json", self)

    async def __call__(self, **_: Any) -> tuple[dict[str, Any], str]:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("недостижимо")


async def stop_bot_mid_ai(h: BotHarness, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Начальник отправил свои слова, AI думает — и бот останавливается: обработка апдейта
    прерывается (так делает web._drain при остановке на хостинге). Состояние диалога — в БД."""
    ai = HangingGemini(monkeypatch)
    pending = asyncio.create_task(h.send_text(MGR, text))
    await asyncio.wait_for(ai.started.wait(), timeout=10)
    pending.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pending
    monkeypatch.setattr(formulate, "ai_available", lambda: False)  # новый экземпляр: AI не нужен


async def test_bot_stopped_while_ai_formulates_result_draft_is_not_frozen(app, monkeypatch):
    """Мягкая остановка посреди запроса к AI: черновик не «замерзает» на «⏳ Подождите…» —
    следующее сообщение начальника показывает вариант по его словам с кнопками, мастер продолжается."""
    h = app
    await team(h)
    await reach_result_step(h)
    await stop_bot_mid_ai(h, monkeypatch, TZ_RESULT)

    await h.send_text(MGR, "алло?")
    shown = h.last_text(MGR)
    assert "Вариант от AI не пришёл" in shown and "Проверить 100 договоров" in shown
    assert "✅ Принять" in h.buttons(MGR)
    await h.press_button(MGR, "Принять")
    assert "шаг 4 из 6" in h.last_text(MGR)


async def test_bot_killed_while_ai_formulates_result_flag_expires(app, monkeypatch):
    """Жёсткая остановка (SIGKILL, нехватка памяти): убрать за собой бот не успел, в БД остался ai_busy.
    Пока AI мог бы ещё ответить — «⏳ Подождите…»; позже флаг считается брошенным — вариант по правилам,
    и черновик можно продолжить (раньше он отвечал «Подождите» на всё, пока не нажмут /cancel)."""
    h = app
    await team(h)
    await reach_result_step(h)

    async def killed(*args: Any) -> None:  # «убитый» процесс ничего не успевает записать
        return None

    monkeypatch.setattr(task_create, "_settle_cancelled", killed)
    await stop_bot_mid_ai(h, monkeypatch, TZ_RESULT)

    await h.send_text(MGR, "алло?")
    assert h.last_text(MGR) == task_create.AI_BUSY_TEXT
    later = utcnow() + timedelta(seconds=task_create._ai_busy_stale_sec() + 1)
    monkeypatch.setattr(task_create, "utcnow", lambda: later)
    await h.send_text(MGR, "Принять")
    assert "Вариант от AI не пришёл" in h.last_text(MGR)
    await h.press_button(MGR, "Принять")
    assert "шаг 4 из 6" in h.last_text(MGR)
