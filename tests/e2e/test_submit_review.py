"""Сценарии «сдача результата → AI-оценка → проверка руководителем» (ТЗ, шаги 4–6; SPEC 7.6, 7.7).

Бот целиком (bot.main.build_dispatcher) на фейковом Telegram API (tests/e2e/fakebot.py), БД в памяти.
AI по умолчанию выключен (оценка по правилам); фикстура ``gemini`` «включает» AI и подменяет
generate_json заранее заданным ответом — в сеть тесты не ходят.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from bot.ai import evaluate as ai_evaluate
from bot.ai import formulate as ai_formulate
from bot.ai import provider as ai_provider
from bot.ai.provider import AIUnavailable
from bot.db.models import (
    EventType,
    Priority,
    ReviewDecision,
    Submission,
    Task,
    TaskEvent,
    TaskSource,
    TaskStatus,
    User,
)
from bot.handlers import task_submit
from bot.scheduler import jobs
from bot.services import tasks as tasks_svc
from bot.ui.callbacks import PickCB, SubCB, TaskCB
from bot.ui.texts import BTN_MY_TASKS, BTN_REVIEW, BTN_SUBMIT
from bot.utils import dateparse
from bot.utils.dates import utcnow

from .fakebot import MANAGER_TG_ID, BotHarness, RequestLog

pytestmark = pytest.mark.asyncio

MGR = MANAGER_TG_ID   # Петрова — руководитель, поставила задачу
MGR2 = 1002           # Сидоров — второй руководитель
EMP = 2001            # Иванов — исполнитель
EMP2 = 2002           # Кузнецова — другой сотрудник

TITLE = "Анализ договоров"
FACT = "Проверено 110 договоров, в 12 выявлены нарушения"
RESULT = "Подготовлены рекомендации по выявленным нарушениям"
AI_RATIONALE = (
    "План: 100 договоров, факт: 110 — перевыполнение на 10 %. "
    "Дополнительно подготовлены рекомендации по нарушениям."
)


# --- Фикстуры и помощники -----------------------------------------------------------------------


@dataclass
class FakeGemini:
    """Подмена Gemini: запоминает запросы и отдаёт заранее заданный JSON-ответ (или ошибку)."""

    answer: dict[str, Any] = field(
        default_factory=lambda: {"score": 110, "rationale": AI_RATIONALE, "completeness": "exceeded"}
    )
    model: str = "gemini-test-flash"
    error: BaseException | None = None
    delay: float = 0.0
    # Что «успевает случиться», пока модель думает (действия других пользователей).
    meanwhile: Callable[[], Awaitable[None]] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def prompt(self) -> str:
        """Текстовые части последнего запроса одной строкой."""
        return "\n".join(part for part in self.calls[-1]["parts"] if isinstance(part, str))

    @property
    def binary_parts(self) -> list[Any]:
        """Файлы, переданные модели «как есть» (фото, PDF)."""
        return [part for part in self.calls[-1]["parts"] if not isinstance(part, str)]


@pytest.fixture
def gemini(monkeypatch: pytest.MonkeyPatch) -> FakeGemini:
    """«Включить» AI: ai_available() -> True, generate_json -> FakeGemini (без сети)."""
    fake = FakeGemini()

    async def generate_json(*, system: str, parts: list, schema: dict, max_output_tokens: int = 2048):
        fake.calls.append({"system": system, "parts": list(parts), "schema": schema})
        if fake.meanwhile is not None:
            await fake.meanwhile()
        if fake.delay:
            await asyncio.sleep(fake.delay)
        if fake.error is not None:
            raise fake.error
        return dict(fake.answer), fake.model

    async def no_formulate(**_: Any):
        raise AIUnavailable("в этих тестах подсказки формулировок не нужны")

    for module in (ai_provider, ai_evaluate):
        monkeypatch.setattr(module, "generate_json", generate_json)
    monkeypatch.setattr(ai_formulate, "generate_json", no_formulate)
    for module in (ai_provider, ai_evaluate, ai_formulate, task_submit):
        monkeypatch.setattr(module, "ai_available", lambda: True)
    return fake


@pytest.fixture(autouse=True)
def _fast_album(monkeypatch: pytest.MonkeyPatch) -> None:
    """Альбом в тестах приходит строго по очереди — ждать «хвост» альбома секунду незачем."""
    monkeypatch.setattr(task_submit, "ALBUM_DELAY_SEC", 0)


def analysis_xlsx() -> bytes:
    """Analysis.xlsx из ТЗ: таблица со 110 проверенными договорами."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Анализ"
    ws.append(["№", "Договор", "Нарушение"])
    for n in range(1, 111):
        ws.append([n, f"Договор {n:03d}", "да" if n % 9 == 0 else "нет"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _team(h: BotHarness) -> tuple[User, User]:
    """Руководитель Петрова и сотрудник Иванов открыли бота (/start -> главное меню)."""
    mgr = await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    emp = await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист")
    await h.send_command(MGR, "start")
    await h.send_command(EMP, "start")
    return mgr, emp


async def _task(
    h: BotHarness,
    mgr: User,
    emp: User,
    *,
    title: str = TITLE,
    plan_value: float | None = 100.0,
    plan_unit: str | None = "договоров",
    deadline: Any = None,
) -> int:
    """Задача прямо в БД (как после «➕ Поставить задачу»); по умолчанию — пример из ТЗ, срок через 3 дня."""
    async with h.db() as s:
        task = Task(
            title=title,
            expected_result="Проверить 100 договоров и представить отчёт",
            plan_value=plan_value,
            plan_unit=plan_unit,
            deadline=deadline or utcnow() + timedelta(days=3),
            priority=Priority.MEDIUM,
            weight=20,
            status=TaskStatus.ACTIVE,
            source=TaskSource.MANAGER,
            assignee_id=emp.id,
            created_by_id=mgr.id,
            manager_id=mgr.id,
        )
        s.add(task)
        await s.commit()
        return task.id


async def _submit(
    h: BotHarness,
    *,
    title: str = TITLE,
    fact: str = FACT,
    result: str | None = RESULT,
    value: str | None = "110",
    send_files: bool = False,
) -> RequestLog:
    """Сотрудник проходит «✅ Сдать результат» целиком и нажимает «📤 Отправить» (лог отправки)."""
    await h.press_menu(EMP, BTN_SUBMIT)
    await h.press_button(EMP, title)
    await h.send_text(EMP, fact)
    if result is None:
        await h.press_button(EMP, "Пропустить")
    else:
        await h.send_text(EMP, result)
    if "Фактическое значение" in (h.last_text(EMP) or ""):
        if value is None:
            await h.press_button(EMP, "Пропустить")
        else:
            await h.send_text(EMP, value)
    if send_files:
        await h.send_document(EMP, "Analysis.xlsx", content=analysis_xlsx())
        album = h.new_media_group_id()
        for n in range(1, 4):
            await h.send_photo(EMP, f"фото {n}".encode(), media_group_id=album)
        await h.press_button(EMP, "Готово")
    else:
        await h.press_button(EMP, "Без файлов")
    assert "Проверьте перед отправкой" in h.last_text(EMP)
    return await h.press_button(EMP, "Отправить")


async def _sub(h: BotHarness, task_id: int, attempt: int = 1) -> Submission:
    return await h.scalar(select(Submission).where(Submission.task_id == task_id, Submission.attempt == attempt))


async def _events(h: BotHarness, task_id: int) -> list[TaskEvent]:
    return await h.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id))


async def _cancel_in_db(h: BotHarness, task_id: int, mgr: User) -> None:
    """Руководитель отменил задачу (то же, что «🚫 Отменить» в карточке)."""
    async with h.db() as s:
        manager = await s.get(User, mgr.id)
        await tasks_svc.cancel_task(s, task_id, manager, "Договоры передали в другой отдел")
        await s.commit()


async def _submitted_in_db(h: BotHarness, emp: User, task_id: int, ai_score: float = 100) -> int:
    """Результат уже сдан и оценён AI (быстрая подготовка очереди без диалога)."""
    async with h.db() as s:
        employee = await s.get(User, emp.id)
        sub = await tasks_svc.submit_result(s, task_id, employee, fact_text="Сделано всё по плану")
        await tasks_svc.record_evaluation(s, sub.id, score=ai_score, rationale="План выполнен.", source="ai")
        await s.commit()
        return sub.id


# --- ТЗ: план 100 → факт 110, AI предлагает 110 %, руководитель подтверждает ------------------------


async def test_tz_example_ai_suggests_110_and_manager_confirms(app: BotHarness, gemini: FakeGemini) -> None:
    """Иванов сдаёт «Анализ договоров» как в ТЗ: план 100 договоров, факт 110, рекомендации,
    файл Analysis.xlsx и альбом из трёх фото. AI сравнивает план и факт и предлагает 110 %.
    Петрова видит «AI предлагает: 110 %», кнопки решения и файлы, подтверждает — задача выполнена,
    Иванов получает итоговую оценку.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)

    # Список «✅ Сдать результат» -> выбор задачи -> диалог начинается с плана.
    await h.press_menu(EMP, BTN_SUBMIT)
    assert "Выберите задачу" in h.last_text(EMP)
    await h.press_button(EMP, TITLE)
    intro = h.last_text(EMP)
    assert "Проверить 100 договоров и представить отчёт" in intro
    assert "Шаг 1 из 4. Что фактически сделано?" in intro

    await h.send_text(EMP, FACT)
    assert "Какой получен результат?" in h.last_text(EMP)
    await h.send_text(EMP, RESULT)
    assert "Фактическое значение?" in h.last_text(EMP)
    assert "План: 100 договоров" in h.last_text(EMP)
    await h.send_text(EMP, "110 договоров")
    assert "Какие документы или материалы подтверждают выполнение?" in h.last_text(EMP)

    # Файл с подписью и альбом из трёх фото: бот считает файлы, а не отвечает на каждое фото отдельно.
    caption = "Сводная таблица по 110 договорам"
    await h.send_document(EMP, "Analysis.xlsx", content=analysis_xlsx(), caption=caption)
    assert "Добавлено файлов: 1" in h.last_text(EMP)
    prompts_before = len(h.messages(EMP))
    album = h.new_media_group_id()
    for n in range(1, 4):
        await h.send_photo(EMP, f"фото {n}".encode(), media_group_id=album)
    assert "Добавлено файлов: 4" in h.last_text(EMP)
    assert len(h.messages(EMP)) == prompts_before + 1
    assert "✅ Готово (4)" in h.buttons(EMP)

    await h.press_button(EMP, "Готово")
    summary = h.last_text(EMP)
    assert "Проверьте перед отправкой" in summary
    assert "100 договоров → 110 договоров" in summary
    assert "Analysis.xlsx" in summary and "Файлы (4)" in summary
    assert f"Материалы: Analysis.xlsx: {caption}" in summary  # подпись к файлу не потерялась
    assert h.buttons(EMP) == ["📤 Отправить", "✖️ Отмена"]

    log = await h.press_button(EMP, "Отправить")

    # AI получил план, факт, содержимое таблицы и три фото.
    assert len(gemini.calls) == 1
    assert "Проверить 100 договоров" in gemini.prompt
    assert FACT in gemini.prompt and "110 договоров" in gemini.prompt
    assert "Договор 110" in gemini.prompt  # текст из Analysis.xlsx
    assert caption in gemini.prompt
    assert len(gemini.binary_parts) == 3

    # Сотрудник: «отправлено на проверку», без оценки AI.
    employee_view = log.to(EMP).text
    assert "Результат отправлен руководителю на проверку" in employee_view
    assert "Файлов: 4" in employee_view
    for leak in ("110 %", "AI", "🤖", "перевыполнение"):
        assert leak not in employee_view

    # Руководитель: план ↔ факт, «AI предлагает: 110 %», обоснование, кнопки решения и сами файлы.
    to_mgr = log.to(MGR)
    review = h.find_message(MGR, f"Результат по задаче #{task_id}")
    assert "🤖 AI предлагает: 110 %" in review.content
    assert "перевыполнение на 10 %" in review.content
    assert "План: 100 договоров → Факт: 110 договоров" in review.content
    assert "в срок" in review.content
    assert caption in review.content
    assert review.button_texts == ["✅ Подтвердить 110 %", "✏️ Изменить оценку", "↩ На доработку", "📎 Файлы (4)"]
    assert [f.file_name for f in to_mgr.documents] == ["Analysis.xlsx"]
    assert [f.kind for f in to_mgr.files].count("photo") == 3

    task = await h.get_task(task_id)
    sub = await _sub(h, task_id)
    assert task.status == TaskStatus.SUBMITTED
    assert (sub.fact_value, sub.ai_score, sub.ai_source, sub.ai_model) == (110, 110, "ai", "gemini-test-flash")
    assert len(sub.attachments) == 4
    assert await h.get_state(EMP) is None

    # Петрова подтверждает 110 %.
    log = await h.press_button(MGR, "Подтвердить 110 %", review.message_id)
    assert log.alert == "✅ Оценка подтверждена"
    closed = h.api.messages[(MGR, review.message_id)]
    assert "✅ Подтверждено: 110 %" in closed.content
    assert closed.button_texts == ["📎 Файлы (4)"]  # кнопки решения исчезли, доступ к файлам остался
    notice = log.to(EMP).text
    assert "Итоговая оценка: 110 %" in notice
    assert "подтвердил предварительную оценку" in notice
    for leak in ("AI", "🤖", "перевыполнение"):
        assert leak not in notice  # сотруднику — итог руководителя, без «кухни» AI

    # Через неделю Петрова снова открывает подтверждения из этого же сообщения.
    log = await h.press_button(MGR, "Файлы (4)", review.message_id)
    assert [f.file_name for f in log.to(MGR).documents] == ["Analysis.xlsx"]
    assert len(log.to(MGR).files) == 4

    task = await h.get_task(task_id)
    sub = await _sub(h, task_id)
    assert task.status == TaskStatus.DONE and task.final_score == 110 and task.completed_at is not None
    assert sub.decision == ReviewDecision.APPROVED and sub.final_score == 110
    assert sub.reviewer.tg_id == MGR
    types = [e.type for e in await _events(h, task_id)]
    assert types[-3:] == [EventType.SUBMITTED, EventType.AI_EVALUATED, EventType.SCORE_CONFIRMED]


async def test_without_ai_rules_compare_plan_and_fact(app: BotHarness) -> None:
    """AI выключен (нет ключа Gemini): бот всё равно сравнивает план и факт по правилам —
    руководитель видит «Расчёт по правилам (AI недоступен): 110 %» и может подтвердить.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)

    log = await _submit(h)

    review = log.to(MGR).text
    assert "📐 Расчёт по правилам (AI недоступен): 110 %" in review
    assert "План: 100, факт: 110" in review
    assert "AI предлагает" not in review
    assert "✅ Подтвердить 110 %" in h.buttons(MGR)
    assert "Файлы" not in " ".join(h.buttons(MGR))  # без вложений кнопки файлов нет
    sub = await _sub(h, task_id)
    assert (sub.ai_score, sub.ai_source, sub.ai_model) == (110, "rules", None)

    await h.press_button(MGR, "Подтвердить 110 %")
    assert (await h.get_task(task_id)).final_score == 110


@pytest.mark.parametrize(
    "failure",
    ["limit", "timeout"],
    ids=["исчерпан-лимит-Gemini", "Gemini-думает-слишком-долго"],
)
async def test_ai_failure_falls_back_to_rules(
    app: BotHarness, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Gemini включён, но исчерпан бесплатный лимит или зависла модель: сдача всё равно доходит
    до руководителя с оценкой по правилам, сотрудник видит «отправлено», ничего не «ломается».
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    if failure == "limit":
        gemini.error = AIUnavailable("429 RESOURCE_EXHAUSTED")
    else:
        gemini.delay = 5
        monkeypatch.setattr(task_submit, "_ai_budget_sec", lambda: 0.05)

    log = await _submit(h)

    assert len(gemini.calls) == 1
    assert "Результат отправлен руководителю на проверку" in log.to(EMP).text
    assert "Расчёт по правилам (AI недоступен): 110 %" in log.to(MGR).text
    sub = await _sub(h, task_id)
    assert (sub.ai_score, sub.ai_source) == (110, "rules")


async def test_database_error_saving_ai_score_falls_back_to_rules(
    app: BotHarness, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI ответил, но запись его оценки упала с ошибкой базы внутри транзакции хендлера (на PostgreSQL
    после такой ошибки транзакция «сломана» до ROLLBACK). Бот откатывает её, считает по правилам
    и всё равно отправляет результат руководителю — сдача не теряется."""
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    original = tasks_svc.record_evaluation
    failed: list[str] = []

    async def broken_once(session: Any, sub_id: int, **kwargs: Any) -> Any:
        if kwargs.get("source") == "ai" and not failed:
            failed.append("ai")
            from sqlalchemy import text

            await session.execute(text("SELECT * FROM kpi_no_such_table"))  # настоящая ошибка БД
        return await original(session, sub_id, **kwargs)

    monkeypatch.setattr(tasks_svc, "record_evaluation", broken_once)
    log = await _submit(h)

    assert failed == ["ai"] and len(gemini.calls) == 1
    assert "Результат отправлен руководителю на проверку" in log.to(EMP).text
    assert "Расчёт по правилам" in log.to(MGR).text and "✅ Подтвердить 110 %" in h.buttons(MGR)
    sub = await _sub(h, task_id)
    assert (sub.ai_score, sub.ai_source) == (110, "rules")
    assert (await h.get_task(task_id)).status == TaskStatus.SUBMITTED


# --- Руководитель меняет оценку ---------------------------------------------------------------------


async def test_manager_changes_score_to_100_with_comment(app: BotHarness, gemini: FakeGemini) -> None:
    """Петрова не согласна с AI (110 %): «✏️ Изменить оценку» -> сначала ошибается (200),
    потом выбирает 100 % и пишет комментарий. Иванов получает 100 % с комментарием,
    в журнале — изменение оценки AI 110 → 100.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await _submit(h)
    review = h.find_message(MGR, "AI предлагает: 110 %")

    await h.press_button(MGR, "Изменить оценку", review.message_id)
    prompt = h.last_text(MGR)
    assert "Изменение оценки" in prompt and "AI предлагает: 110 %" in prompt
    assert "🤖 110 %" in h.buttons(MGR) and "100 %" in h.buttons(MGR)

    await h.send_text(MGR, "200")
    assert "от 0 до 150" in h.last_text(MGR)
    await h.send_text(MGR, "отлично")
    assert "Не понял оценку" in h.last_text(MGR)

    await h.press_button(MGR, "100 %")
    assert "Итоговая оценка: 100 %" in h.last_text(MGR)
    assert "Комментарий к оценке?" in h.last_text(MGR)

    comment = "Рекомендации без расчёта экономии — <b>засчитываю</b> как план"
    log = await h.send_text(MGR, comment)

    done = log.to(MGR).text
    assert "✏️ Оценка изменена: 100 % (AI предлагал 110 %)" in done
    assert comment in done  # HTML пользователя показан как текст, не как разметка
    closed = h.api.messages[(MGR, review.message_id)]
    assert "Оценка изменена: 100 %" in closed.content and closed.buttons == []
    notice = log.to(EMP).text
    assert "Итоговая оценка: 100 %" in notice
    assert "Оценку выставил руководитель" in notice
    assert comment in notice
    assert await h.get_state(MGR) is None
    # В чате не осталось «живых» кнопок пройденных шагов (оценки, «Пропустить», решения).
    assert [m.button_texts for m in h.messages(MGR) if m.buttons] == []

    task = await h.get_task(task_id)
    sub = await _sub(h, task_id)
    assert task.status == TaskStatus.DONE and task.final_score == 100 and task.ai_score == 110
    assert sub.decision == ReviewDecision.CHANGED and sub.review_comment == comment
    changed = [e for e in await _events(h, task_id) if e.type == EventType.SCORE_CHANGED]
    assert len(changed) == 1
    assert (changed[0].data["ai_score"], changed[0].data["score"]) == (110, 100)


@pytest.mark.parametrize(
    ("typed", "shown", "stored"),
    [("95 %", "95 %", 95), ("95,5", "96 %", 96)],
    ids=["целое-с-процентом", "дробное-округляется"],
)
async def test_manager_types_score_and_skips_comment(app: BotHarness, typed: str, shown: str, stored: int) -> None:
    """Оценку можно написать текстом («95 %»), комментарий — пропустить. Дробную оценку бот
    округляет до целого: сохраняется ровно то, что руководитель и сотрудник видят на экране.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await _submit(h)

    await h.press_button(MGR, "Изменить оценку")
    await h.send_text(MGR, typed)
    assert f"Итоговая оценка: {shown}" in h.last_text(MGR)
    log = await h.press_button(MGR, "Пропустить")

    assert log.alert == "✏️ Оценка сохранена"
    assert f"Оценка изменена: {shown}" in log.to(MGR).text
    assert f"Итоговая оценка: {shown}" in log.to(EMP).text
    assert "Комментарий" not in log.to(EMP).text
    task = await h.get_task(task_id)
    assert task.final_score == stored and (await _sub(h, task_id)).review_comment is None


# --- Доработка и повторная сдача --------------------------------------------------------------------


async def test_rework_with_comment_and_new_deadline_then_second_attempt(
    app: BotHarness, gemini: FakeGemini
) -> None:
    """Петрова возвращает результат на доработку с комментарием и новым сроком «завтра».
    Иванов видит, что доработать, сдаёт снова (попытка 2) — AI знает, что просили доработать,
    Петрова подтверждает. У задачи две сдачи: первая «на доработку», вторая принята.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await _submit(h, send_files=True)
    review = h.find_message(MGR, "AI предлагает: 110 %")

    await h.press_button(MGR, "На доработку", review.message_id)
    assert "Что нужно доработать?" in h.last_text(MGR)
    await h.send_sticker(MGR)
    assert "Опишите текстом, что нужно доработать" in h.last_text(MGR)
    comment = "Приложите акты проверки по каждому договору"
    await h.send_text(MGR, comment)
    assert "Срок доработки?" in h.last_text(MGR)
    assert "📌 Оставить текущий срок" in h.buttons(MGR)

    log = await h.press_button(MGR, "Завтра")

    tomorrow_iso = next(iso for label, iso in dateparse.quick_deadline_options() if label.startswith("Завтра"))
    assert log.alert == "↩ Возвращено на доработку"
    assert "Возвращено на доработку" in log.to(MGR).text and "(новый)" in log.to(MGR).text
    assert h.api.messages[(MGR, review.message_id)].button_texts == ["📎 Файлы (4)"]
    notice = log.to(EMP).text
    assert f"Задача #{task_id} возвращена на доработку" in notice
    assert comment in notice
    assert h.buttons(EMP) == ["📤 Сдать результат", "📋 Открыть"]

    task = await h.get_task(task_id)
    first = await _sub(h, task_id, 1)
    assert task.status == TaskStatus.REWORK and task.rework_count == 1
    assert task.deadline == dateparse.iso_to_deadline(tomorrow_iso)
    assert first.decision == ReviewDecision.REWORK and first.review_comment == comment

    # Иванов дорабатывает прямо из уведомления.
    await h.press_button(EMP, "Сдать результат")
    intro = h.last_text(EMP)
    assert "возвращена на доработку" in intro
    assert comment in intro
    assert "Попытка сдачи №2" in intro
    await h.send_text(EMP, "Проверено 110 договоров, акты приложены")
    await h.press_button(EMP, "Пропустить")
    await h.send_text(EMP, "110")
    await h.send_document(EMP, "Акты.pdf", content=b"%PDF-1.4 acts")
    await h.press_button(EMP, "Готово")
    log = await h.press_button(EMP, "Отправить")

    assert f"Что руководитель просил доработать в прошлый раз: «{comment}»" in gemini.prompt
    assert "попытка 2" in log.to(MGR).text
    assert [f.file_name for f in log.to(MGR).documents] == ["Акты.pdf"]  # только файлы новой попытки

    log = await h.press_button(MGR, "Подтвердить 110 %")
    assert "Итоговая оценка: 110 %" in log.to(EMP).text
    task = await h.get_task(task_id)
    second = await _sub(h, task_id, 2)
    assert task.status == TaskStatus.DONE and task.final_score == 110
    assert [s.attempt for s in task.submissions] == [1, 2]
    assert second.decision == ReviewDecision.APPROVED
    assert (await _sub(h, task_id, 1)).decision == ReviewDecision.REWORK


async def test_rework_deadline_in_past_is_rejected_and_keep_current(app: BotHarness) -> None:
    """Возврат на доработку: срок «вчера» бот не принимает и переспрашивает;
    «📌 Оставить текущий срок» сохраняет прежний срок.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    old_deadline = (await h.get_task(task_id)).deadline
    await _submit(h)

    await h.press_button(MGR, "На доработку")
    await h.send_text(MGR, "Добавьте выводы по каждому нарушению")
    await h.send_text(MGR, "вчера")
    assert "Не понял срок или он уже в прошлом" in h.last_text(MGR)
    assert (await h.get_task(task_id)).status == TaskStatus.SUBMITTED

    log = await h.press_button(MGR, "Оставить текущий срок")
    assert "(без изменений)" in log.to(MGR).text
    task = await h.get_task(task_id)
    assert task.status == TaskStatus.REWORK and task.deadline == old_deadline


# --- Просрочка --------------------------------------------------------------------------------------


async def test_late_submission_gets_late_days_and_rules_penalty(app: BotHarness) -> None:
    """Срок истёк 2,5 дня назад. Иванов выполнил план (100 из 100), но с опозданием:
    в сводке — предупреждение, у руководителя — «с опозданием 2,5 дн.» и оценка по правилам
    100 − 2,5 × 2 = 95 % (как во втором примере ТЗ).
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp, deadline=utcnow() - timedelta(days=2, hours=12))

    await h.press_menu(EMP, BTN_SUBMIT)
    assert any(b.startswith("⏰") for b in h.buttons(EMP))
    await h.press_button(EMP, TITLE)
    assert "просрочено" in h.last_text(EMP)
    await h.send_text(EMP, "Проверено 100 договоров")
    await h.press_button(EMP, "Пропустить")
    await h.send_text(EMP, "100")
    await h.press_button(EMP, "Без файлов")
    assert "Срок уже прошёл — результат будет отмечен как сданный с опозданием" in h.last_text(EMP)
    log = await h.press_button(EMP, "Отправить")

    review = log.to(MGR).text
    assert "с опозданием 2,5 дн." in review
    assert "📐 Расчёт по правилам (AI недоступен): 95 %" in review
    assert "штраф 5 п.п." in review
    assert "✅ Подтвердить 95 %" in h.buttons(MGR)
    sub = await _sub(h, task_id)
    assert sub.is_late is True and sub.late_days == 2.5 and sub.ai_score == 95

    await h.press_menu(MGR, BTN_REVIEW)
    assert "опоздание 2,5 дн." in h.last_text(MGR)


# --- Уже обработано / чужие кнопки ------------------------------------------------------------------


async def test_second_manager_gets_already_processed_alert(app: BotHarness) -> None:
    """Сидоров открыл тот же результат из «📝 На проверке», но Петрова уже подтвердила оценку:
    его кнопки отвечают «Результат уже обработан» и ничего не меняют. После первого нажатия мёртвые
    кнопки решения исчезают из его сообщения (остаются файлы и очередь проверки); если клиент
    ещё показывает старые кнопки, они по-прежнему только объясняют, что результат обработан.
    """
    h = app
    mgr, emp = await _team(h)
    await h.seed_user(MGR2, "Сидоров Олег Петрович", role="manager")
    await h.send_command(MGR2, "start")
    task_id = await _task(h, mgr, emp)
    log = await _submit(h)
    assert MGR2 not in log.chats  # результат ушёл только ответственному руководителю

    await h.press_menu(MGR2, BTN_REVIEW)
    assert "На проверке" in h.last_text(MGR2) and "Иванов И. И." in h.last_text(MGR2)
    await h.press_button(MGR2, f"#{task_id}")
    assert "Расчёт по правилам (AI недоступен): 110 %" in h.last_text(MGR2)

    await h.press_button(MGR, "Подтвердить 110 %")

    stale = h.last_message(MGR2).message_id
    decision = ("Подтвердить", "Изменить оценку", "На доработку")
    stale_buttons = [h.find_button(MGR2, button, stale) for button in decision]
    for data in stale_buttons:
        log = await h.press(MGR2, data, stale)
        assert log.alert == "Результат уже обработан"
        assert not log.to(EMP).texts
    assert not any(button in label for label in h.buttons(MGR2, stale) for button in decision)
    log = await h.press(MGR2, TaskCB(action="review", task_id=task_id))
    assert log.alert == "Результат уже обработан"

    task = await h.get_task(task_id)
    sub = await _sub(h, task_id)
    assert task.status == TaskStatus.DONE and task.final_score == 110
    assert sub.reviewer.tg_id == MGR


async def test_change_dialog_after_other_manager_decided(app: BotHarness) -> None:
    """Сидоров начал «✏️ Изменить оценку», пока писал число — Петрова уже вернула результат на доработку.
    Ввод Сидорова не перетирает решение: бот сообщает, что результат уже обработан, диалог закрыт.
    """
    h = app
    mgr, emp = await _team(h)
    await h.seed_user(MGR2, "Сидоров Олег Петрович", role="manager")
    task_id = await _task(h, mgr, emp)
    await _submit(h)
    await h.send_command(MGR2, "start")
    await h.press_menu(MGR2, BTN_REVIEW)
    await h.press_button(MGR2, f"#{task_id}")
    await h.press_button(MGR2, "Изменить оценку")

    await h.press_button(MGR, "На доработку")
    await h.send_text(MGR, "Нужны акты")
    await h.press_button(MGR, "Оставить текущий срок")

    log = await h.send_text(MGR2, "100")
    assert "Результат уже обработан" in log.to(MGR2).text
    assert await h.get_state(MGR2) is None
    assert not h.has_button(MGR2, "120 %")  # у вопроса закрытого диалога кнопок не осталось
    task = await h.get_task(task_id)
    assert task.status == TaskStatus.REWORK and task.final_score is None


async def test_employee_cannot_review_and_stranger_cannot_submit(app: BotHarness) -> None:
    """Сотрудник не может «подделать» кнопку проверки, а чужой сотрудник — сдать чужую задачу."""
    h = app
    mgr, emp = await _team(h)
    await h.seed_user(EMP2, "Кузнецова Мария Олеговна")
    task_id = await _task(h, mgr, emp)

    log = await h.press(EMP2, TaskCB(action="submit", task_id=task_id))
    assert "только исполнитель" in log.alert
    assert await h.get_state(EMP2) is None

    await _submit(h)
    sub = await _sub(h, task_id)
    log = await h.press(EMP, SubCB(action="ok", sub_id=sub.id))
    assert "Недостаточно прав" in log.alert
    assert (await h.get_task(task_id)).status == TaskStatus.SUBMITTED


# --- Файлы ------------------------------------------------------------------------------------------


async def test_files_button_resends_attachments(app: BotHarness) -> None:
    """«📎 Файлы (4)» присылает руководителю все подтверждения ещё раз: Analysis.xlsx и альбом фото."""
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await _submit(h, send_files=True)
    uploaded = {a.file_id for a in (await _sub(h, task_id)).attachments}

    log = await h.press_button(MGR, "Файлы (4)")

    assert log.alert == "📎 Отправляю файлы…"
    assert [f.file_name for f in log.to(MGR).documents] == ["Analysis.xlsx"]
    assert log.to(MGR).documents[0].content.startswith(b"PK")  # тот самый Excel-файл, не заглушка
    assert [f.kind for f in log.to(MGR).files].count("photo") == 3
    assert {f.file_id for f in log.to(MGR).files} == uploaded
    assert (await h.get_task(task_id)).status == TaskStatus.SUBMITTED  # просмотр файлов ничего не решает


async def test_files_button_without_attachments(app: BotHarness) -> None:
    """Сдача без файлов: кнопки «📎 Файлы» нет, а старая/поддельная кнопка честно отвечает."""
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await _submit(h)
    sub = await _sub(h, task_id)
    assert not any("Файлы" in b for b in h.buttons(MGR))

    log = await h.press(MGR, SubCB(action="files", sub_id=sub.id))
    assert log.alert == "К этому результату файлы не приложены."
    assert not log.files


# --- Сотрудник не видит оценку AI до решения -------------------------------------------------------


async def test_employee_never_sees_ai_score_before_decision(app: BotHarness, gemini: FakeGemini) -> None:
    """Пока руководитель не решил, Иванов нигде не видит предложение AI: ни после отправки,
    ни в карточке задачи, ни в её истории. Повторно сдать нельзя — результат уже на проверке.
    После решения — видит итоговую оценку.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    seen_before = len(h.outputs(EMP))

    await _submit(h, send_files=True)

    await h.press_menu(EMP, BTN_SUBMIT)
    assert "Нет задач для сдачи" in h.last_text(EMP)
    log = await h.press(EMP, TaskCB(action="submit", task_id=task_id))
    assert "уже отправлен" in log.alert

    await h.press_menu(EMP, BTN_MY_TASKS)
    await h.press_button(EMP, "На проверке")
    await h.press_button(EMP, TITLE)
    card = h.last_text(EMP)
    assert f"Задача #{task_id}" in card and "На проверке" in card
    await h.press_button(EMP, "История")
    assert "результат сдан (попытка 1" in h.last_text(EMP)

    everything = "\n".join(h.outputs(EMP)[seen_before:])
    for leak in ("AI", "🤖", "Предварительн", "перевыполнение", "Подтвердить 110"):
        assert leak not in everything, leak

    log = await h.press_button(MGR, "Подтвердить 110 %")
    assert "Итоговая оценка: 110 %" in log.to(EMP).text


# --- Отменённая задача ------------------------------------------------------------------------------


async def test_submit_button_on_cancelled_task(app: BotHarness) -> None:
    """Иванов открыл «✅ Сдать результат» (две задачи), а Петрова тем временем отменила одну.
    Кнопка отменённой задачи объясняет «Задача отменена руководителем — сдавать результат не нужно»,
    диалог не начинается, а список обновляется — в нём остаётся только задача, которую ещё можно сдать.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await _task(h, mgr, emp, title="Отчёт по закупкам", plan_value=None, plan_unit=None)
    await h.press_menu(EMP, BTN_SUBMIT)
    assert h.has_button(EMP, TITLE) and h.has_button(EMP, "Отчёт по закупкам")
    await _cancel_in_db(h, task_id, mgr)

    log = await h.press_button(EMP, TITLE)

    assert log.alert == "🚫 Задача отменена руководителем — сдавать результат не нужно."
    assert await h.get_state(EMP) is None
    assert not h.has_button(EMP, TITLE)
    assert h.has_button(EMP, "Отчёт по закупкам")
    assert await h.scalar(select(Submission).where(Submission.task_id == task_id)) is None

    # Отменили и вторую — список честно говорит, что сдавать нечего.
    other_id = task_id + 1
    await _cancel_in_db(h, other_id, mgr)
    log = await h.press_button(EMP, "Отчёт по закупкам")
    assert log.alert == "🚫 Задача отменена руководителем — сдавать результат не нужно."
    assert "Нет задач для сдачи" in h.last_text(EMP)
    assert h.buttons(EMP) == []


async def test_review_of_task_cancelled_while_on_review(app: BotHarness) -> None:
    """Результат ждал проверки, но Петрова отменила задачу через карточку. Кнопки решения
    в уведомлении больше не действуют, объясняют почему и после нажатия исчезают; оценка
    не выставляется.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await _submit(h)
    review = h.find_message(MGR, "Результат по задаче")
    await h.press_button(MGR, "Изменить оценку", review.message_id)
    await _cancel_in_db(h, task_id, mgr)

    # Петрова дописывает оценку в уже начатом диалоге — бот объясняет, что задача отменена.
    log = await h.send_text(MGR, "100")
    assert "Результат уже обработан: задача отменена" in log.to(MGR).text
    assert await h.get_state(MGR) is None
    assert h.has_button(MGR, "Подтвердить") and not h.has_button(MGR, "120 %")

    decision = ("Подтвердить", "Изменить оценку", "На доработку")
    stale_buttons = [h.find_button(MGR, button, review.message_id) for button in decision]
    for data in stale_buttons:
        log = await h.press(MGR, data, review.message_id)
        assert log.alert == "Результат уже обработан: задача отменена"
        assert not log.to(EMP).texts
    # Кнопки решения в уведомлении убраны — нажимать там больше нечего.
    assert not any(button in label for label in h.buttons(MGR, review.message_id) for button in decision)
    log = await h.press(MGR, TaskCB(action="review", task_id=task_id))
    assert log.alert == "Результат уже обработан: задача отменена"
    task = await h.get_task(task_id)
    assert task.status == TaskStatus.CANCELLED and task.final_score is None


async def test_task_cancelled_while_employee_fills_answers(app: BotHarness) -> None:
    """Задачу отменили, пока Иванов заполнял ответы: «📤 Отправить» результат не создаёт,
    сотрудник видит, что задача отменена, диалог закрыт.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await h.press_menu(EMP, BTN_SUBMIT)
    await h.press_button(EMP, TITLE)
    await h.send_text(EMP, FACT)
    await h.press_button(EMP, "Пропустить")
    await h.send_text(EMP, "110")
    await h.press_button(EMP, "Без файлов")
    await _cancel_in_db(h, task_id, mgr)

    log = await h.press_button(EMP, "Отправить")

    assert "отменена" in (log.alert or "").lower()
    assert "Результат не отправлен" in h.last_text(EMP)
    assert "отменена" in h.last_text(EMP).lower()
    assert h.buttons(EMP) == []
    assert MGR not in log.chats
    assert await h.get_state(EMP) is None
    assert await h.scalar(select(Submission).where(Submission.task_id == task_id)) is None


async def test_task_cancelled_before_files_step_done(app: BotHarness) -> None:
    """Задачу отменили, пока Иванов присылал файлы: «✅ Готово» закрывает диалог с объяснением."""
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await h.press_menu(EMP, BTN_SUBMIT)
    await h.press_button(EMP, TITLE)
    await h.send_text(EMP, FACT)
    await h.press_button(EMP, "Пропустить")
    await h.press_button(EMP, "Пропустить")
    await h.send_document(EMP, "Analysis.xlsx", content=analysis_xlsx())
    await _cancel_in_db(h, task_id, mgr)

    await h.press_button(EMP, "Готово")

    assert "отменена" in h.last_text(EMP).lower()
    assert h.buttons(EMP) == []
    assert await h.get_state(EMP) is None


# --- Ошибки ввода в диалоге сдачи ------------------------------------------------------------------


async def test_submit_dialog_unhappy_inputs(app: BotHarness) -> None:
    """Иванов отвечает невпопад: «да» вместо описания, стикер, «много» вместо числа,
    документ раньше времени. Бот переспрашивает и ничего не теряет; файл приложен к сдаче.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await h.press_menu(EMP, BTN_SUBMIT)
    await h.press_button(EMP, TITLE)

    await h.send_text(EMP, "да")
    assert "Опишите чуть подробнее" in h.last_text(EMP)
    await h.send_sticker(EMP)
    assert "Ответьте на вопрос выше текстом" in h.last_text(EMP)
    await h.send_document(EMP, "Analysis.xlsx", content=analysis_xlsx())
    assert "Сохранено файлов: 1" in h.last_text(EMP) and "Что фактически сделано?" in h.last_text(EMP)
    await h.send_text(EMP, FACT)
    await h.send_text(EMP, RESULT)
    await h.send_text(EMP, "много")
    assert "Не понял число" in h.last_text(EMP)
    await h.send_text(EMP, "110")
    assert "Уже приложено: 1 файл" in h.last_text(EMP)
    await h.press_button(EMP, "Готово")
    await h.send_text(EMP, "ну что там?")
    assert "нажмите «📤 Отправить»" in h.last_text(EMP)
    await h.press_button(EMP, "Отправить")

    sub = await _sub(h, task_id)
    assert sub.fact_text == FACT and sub.fact_value == 110
    assert [a.file_name for a in sub.attachments] == ["Analysis.xlsx"]


async def test_cancel_submission_dialog(app: BotHarness) -> None:
    """«✖️ Отмена» посреди сдачи: ничего не отправлено, задача по-прежнему в работе."""
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await h.press_menu(EMP, BTN_SUBMIT)
    await h.press_button(EMP, TITLE)
    await h.send_text(EMP, FACT)

    await h.press_button(EMP, "Отмена")

    assert await h.get_state(EMP) is None
    assert (await h.get_task(task_id)).status == TaskStatus.ACTIVE
    assert await h.scalar(select(Submission).where(Submission.task_id == task_id)) is None
    assert not h.sent_to(MGR)[2:]  # руководителю ничего не пришло (после приветствия)


async def test_double_press_send_creates_one_submission(app: BotHarness) -> None:
    """Иванов нажал «📤 Отправить» дважды: сдача одна, руководитель получил одно уведомление."""
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    log = await _submit(h)
    send_data = PickCB(field="confirm", value="yes").pack()
    summary_id = next(c.message_ids[0] for c in log.calls if c.message_ids and c.chat_id == EMP)

    again = await h.press(EMP, send_data, summary_id)

    assert again.answers
    assert MGR not in again.chats
    subs = await h.scalars(select(Submission).where(Submission.task_id == task_id))
    assert len(subs) == 1




# --- Просроченная задача на доработке, большие сдачи, недоступные чаты -----------------------------


async def test_overdue_task_rework_needs_new_deadline_and_resubmission_is_on_time(app: BotHarness) -> None:
    """Иванов сдал с опозданием (оценка по правилам 95 %). Петрова возвращает на доработку:
    оставить истёкший срок нельзя — бот просит новый. Повторная сдача до нового срока —
    «в срок», без штрафа: 100 %.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp, deadline=utcnow() - timedelta(days=2, hours=12))
    await _submit(h, value="100", result=None)
    assert (await _sub(h, task_id)).ai_score == 95

    await h.press_button(MGR, "На доработку")
    await h.send_text(MGR, "Нужны акты по каждому договору")
    prompt = h.last_text(MGR)
    assert "уже прошёл — укажите новый" in prompt
    assert not h.has_button(MGR, "Оставить текущий срок")
    await h.send_text(MGR, "через неделю")
    expected = dateparse.parse_deadline("через неделю")

    task = await h.get_task(task_id)
    assert task.status == TaskStatus.REWORK and task.deadline == expected
    assert "просрочено" not in h.last_text(EMP)

    log = await _submit(h, value="100", result=None)
    review = log.to(MGR).text
    assert "попытка 2" in review and "— в срок" in review
    assert "Расчёт по правилам (AI недоступен): 100 %" in review
    second = await _sub(h, task_id, 2)
    assert (second.is_late, second.late_days, second.ai_score) == (False, 0.0, 100)


async def test_huge_submission_fits_telegram_limits(app: BotHarness) -> None:
    """Иванов пишет очень длинные ответы с символами «<», «&», прикладывает 20 файлов разных типов
    (21-й бот не берёт) и длинное описание материалов. Сводка, уведомление руководителю, подписи файлов,
    повторная отправка файлов и длинный комментарий руководителя укладываются в лимиты Telegram
    (это проверяет harness), ничего не теряется.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    fact = ("Проверено <10% & >5 договоров. " * 200)[:3000]
    result = ("Рекомендации <по> нарушениям & выводы. " * 200)[:3000]

    await h.press_menu(EMP, BTN_SUBMIT)
    await h.press_button(EMP, TITLE)
    await h.send_text(EMP, fact + "!")  # 3001 символ — слишком длинно
    assert "Слишком длинно" in h.last_text(EMP)
    await h.send_text(EMP, fact)
    await h.send_text(EMP, result)
    await h.send_text(EMP, "110")
    await h.send_photo(EMP, b"photo")
    await h.send_video(EMP, file_name="обход_склада.mp4")
    for n in range(1, 19):
        await h.send_document(EMP, f"{n:02d}_" + "Акт_проверки_договора_" * 9 + ".pdf", mime_type="application/pdf")
    assert "Добавлено файлов: 20" in h.last_text(EMP) and "Нажмите «✅ Готово»" in h.last_text(EMP)
    await h.send_document(EMP, "лишний.pdf")
    assert "не более 20 файлов" in h.last_text(EMP)
    await h.send_text(EMP, "Оригиналы актов в папке \\srv\audit\2026 <архив> & копии у юристов. " * 20)
    await h.press_button(EMP, "Готово")
    assert "Файлы (20)" in h.last_text(EMP)
    log = await h.press_button(EMP, "Отправить")

    sub = await _sub(h, task_id)
    assert sub.fact_text == fact
    assert len(sub.attachments) == 20 and "лишний.pdf" not in {a.file_name for a in sub.attachments}
    assert "Подтверждающие материалы: Оригиналы актов" in sub.result_text
    assert len(log.to(MGR).files) == 20
    assert "Проверено <10% & >5 договоров" in h.find_message(MGR, "Результат по задаче").content

    log = await h.press_button(MGR, "Файлы (20)")
    assert len(log.to(MGR).files) == 20
    assert {f.kind for f in log.to(MGR).files} == {"photo", "video", "document"}

    await h.press_button(MGR, "Изменить оценку")
    await h.send_text(MGR, "105")
    await h.send_text(MGR, "Длинный комментарий " * 200)  # 4000 символов — слишком длинно
    assert "Комментарий слишком длинный" in h.last_text(MGR)
    comment = ("Хорошо <b>но</b> & без расчёта экономии. " * 60)[:2000]
    log = await h.send_text(MGR, comment)
    assert "Итоговая оценка: 105 %" in log.to(EMP).text
    assert (await _sub(h, task_id)).review_comment == comment


async def test_blocked_chats_do_not_break_submission_or_review(app: BotHarness) -> None:
    """Руководитель временно заблокировал бота — сдача Иванова всё равно сохраняется и ждёт в очереди.
    Потом Иванов заблокировал бота — решение руководителя всё равно сохраняется."""
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)

    h.api.blocked_chats.add(MGR)
    log = await _submit(h)
    assert "Результат отправлен руководителю на проверку" in log.to(EMP).text
    assert (await h.get_task(task_id)).status == TaskStatus.SUBMITTED

    h.api.blocked_chats.discard(MGR)
    h.api.blocked_chats.add(EMP)
    await h.press_menu(MGR, BTN_REVIEW)
    await h.press_button(MGR, f"#{task_id}")
    log = await h.press_button(MGR, "Подтвердить 110 %")
    assert log.alert == "✅ Оценка подтверждена"
    assert "✅ Подтверждено: 110 %" in h.last_text(MGR)
    task = await h.get_task(task_id)
    assert task.status == TaskStatus.DONE and task.final_score == 110



# --- Пока AI думает ----------------------------------------------------------------------------------


async def test_manager_decides_from_queue_while_ai_is_thinking(app: BotHarness, gemini: FakeGemini) -> None:
    """AI думает над сдачей, а Петрова уже открыла её из «📝 На проверке» (без оценки AI)
    и поставила 90 %. Когда AI ответил, бот не присылает ей «новый» результат с кнопкой
    «Подтвердить 110 %» — решение уже принято и не меняется. Иванову — честный итог.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)

    async def manager_reviews() -> None:
        await h.press_menu(MGR, BTN_REVIEW)
        await h.press_button(MGR, f"#{task_id}")
        # Пока AI думает, руководителю обещано, что оценка придёт отдельным сообщением.
        assert "Предварительная оценка ещё не рассчитана — пришлю её отдельным сообщением" in h.last_text(MGR)
        await h.press_button(MGR, "Изменить оценку")
        await h.send_text(MGR, "90")
        await h.press_button(MGR, "Пропустить")

    gemini.meanwhile = manager_reviews
    log = await _submit(h)

    assert not any("Подтвердить" in text for m in h.messages(MGR) for text in m.button_texts)
    assert sum("AI предлагает" in text for text in h.sent_to(MGR)) == 0
    assert "Руководитель уже принял решение" in log.to(EMP).text
    assert "Итоговая оценка: 90 %" in "\n".join(h.sent_to(EMP))
    task = await h.get_task(task_id)
    sub = await _sub(h, task_id)
    assert task.status == TaskStatus.DONE and task.final_score == 90
    assert sub.ai_score == 110 and sub.decision == ReviewDecision.CHANGED  # оценка AI сохранена для истории


async def test_task_cancelled_while_ai_is_thinking(app: BotHarness, gemini: FakeGemini) -> None:
    """Пока AI оценивал сдачу, Петрова отменила задачу. Результат сохранён, но проверять его
    некому и незачем: руководителю кнопки проверки не приходят, Иванов видит, что задачу отменили.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    gemini.meanwhile = lambda: _cancel_in_db(h, task_id, mgr)

    log = await _submit(h)

    assert MGR not in log.chats
    assert "руководитель тем временем отменил задачу" in log.to(EMP).text
    assert "отправлен руководителю на проверку" not in h.last_text(EMP)
    task = await h.get_task(task_id)
    assert task.status == TaskStatus.CANCELLED
    assert (await _sub(h, task_id)).ai_score == 110


# --- Задача без плана-числа, очередь проверки ------------------------------------------------------


async def test_task_without_plan_number_has_three_steps_and_rules_give_100(app: BotHarness) -> None:
    """У задачи нет планового числа («Подготовить отчёт по закупкам»): вопроса про фактическое значение
    нет (3 шага), правила берут за основу полное выполнение — 100 %, качество оценивает руководитель.
    """
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp, title="Отчёт по закупкам", plan_value=None, plan_unit=None)

    await h.press_menu(EMP, BTN_SUBMIT)
    await h.press_button(EMP, "Отчёт по закупкам")
    assert "Шаг 1 из 3" in h.last_text(EMP)
    await h.send_text(EMP, "Подготовлен отчёт по закупкам за сентябрь")
    await h.press_button(EMP, "Пропустить")
    assert "Шаг 3 из 3. Какие документы" in h.last_text(EMP)
    await h.press_button(EMP, "Без файлов")
    assert "План → факт" not in h.last_text(EMP)
    log = await h.press_button(EMP, "Отправить")

    review = log.to(MGR).text
    assert "Расчёт по правилам (AI недоступен): 100 %" in review
    assert "Числовой план не задан" in review
    sub = await _sub(h, task_id)
    assert sub.fact_value is None and sub.result_text is None and sub.ai_score == 100


async def test_review_queue_pages_and_shrinks_after_decisions(app: BotHarness) -> None:
    """На проверке 9 результатов: «📝 На проверке» показывает по 8 на странице с листанием.
    Петрова открывает результат с второй страницы, подтверждает — в очереди остаётся 8, кнопка
    «📝 Ещё на проверке: 8» ведёт обратно в очередь. Пустая очередь — «Нет результатов на проверке».
    """
    h = app
    mgr, emp = await _team(h)
    ids = []
    for n in range(1, 10):
        task_id = await _task(h, mgr, emp, title=f"Задача {n:02d}", plan_value=None, plan_unit=None)
        await _submitted_in_db(h, emp, task_id, ai_score=90 + n)
        ids.append(task_id)

    await h.press_menu(MGR, BTN_REVIEW)
    assert "На проверке — 9 результатов · стр. 1/2" in h.last_text(MGR)
    assert len([b for b in h.buttons(MGR) if b.startswith("🔍")]) == 8
    await h.press_button(MGR, "Вперёд")
    assert "стр. 2/2" in h.last_text(MGR)
    assert "🤖 99 %" in h.last_text(MGR)
    await h.press_button(MGR, f"#{ids[-1]}")
    assert "AI предлагает: 99 %" in h.last_text(MGR)
    log = await h.press_button(MGR, "Подтвердить 99 %")
    assert "Ещё на проверке: 8" in " ".join(h.buttons(MGR))
    assert "Итоговая оценка: 99 %" in log.to(EMP).text

    await h.press_button(MGR, "Ещё на проверке")
    assert "На проверке — 8 результатов" in h.last_text(MGR)
    assert "Вперёд" not in " ".join(h.buttons(MGR))

    async with h.db() as s:
        manager = await s.get(User, mgr.id)
        for task_id in ids[:-1]:
            task = await tasks_svc.get_task(s, task_id)
            await tasks_svc.review_confirm(s, task.last_submission.id, manager)
        await s.commit()
    await h.press_menu(MGR, BTN_REVIEW)
    assert "Нет результатов на проверке" in h.last_text(MGR)

    log = await h.send_command(EMP, "review")
    assert "На проверке —" not in log.text  # сотруднику очередь проверки не показывается


# --- Регрессия ядра: GET-SUB-LAZY-TASK (найдено этими тестами, исправлено в services/tasks.py) -------


async def test_bulk_confirm_by_submission_from_task_list(app: BotHarness) -> None:
    """Руководитель (или служебный скрипт) подтверждает результаты пачкой: берёт последнюю сдачу
    из задачи и подтверждает её в той же сессии БД. Сервис должен сам загрузить задачу сдачи,
    а не падать с MissingGreenlet, если объект задачи уже не держится в памяти.
    """
    import gc

    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp, plan_value=None, plan_unit=None)
    await _submitted_in_db(h, emp, task_id)
    async with h.db() as s:
        manager = await s.get(User, mgr.id)
        sub = (await tasks_svc.get_task(s, task_id)).last_submission
        gc.collect()  # объект Task больше никто не держит — sub.task не загружен
        await tasks_svc.review_confirm(s, sub.id, manager)
        await s.commit()
    assert (await h.get_task(task_id)).status == TaskStatus.DONE


# --- Срок доработки с опечаткой в годе, файлы выполненной задачи -----------------------------------


async def test_rework_deadline_with_far_year_typo_is_explained(app: BotHarness) -> None:
    """Петрова возвращает результат на доработку и опечатывается в годе нового срока («05.10.2099»).
    Бот просит проверить год (а не пишет «не понял срок»), решение не принимается, диалог ждёт срок."""
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    await _submitted_in_db(h, emp, task_id)
    await h.press(MGR, TaskCB(action="review", task_id=task_id))
    await h.press_button(MGR, "На доработку")
    await h.send_text(MGR, "Добавьте реестр нарушений")

    log = await h.send_text(MGR, "05.10.2099")
    assert "проверьте год" in log.text
    assert await h.get_state(MGR) == "ReviewSG:rework_deadline"
    assert (await h.get_task(task_id)).status == TaskStatus.SUBMITTED
    assert not log.to(EMP).texts


async def test_manager_gets_evidence_files_from_card_of_done_task(app: BotHarness) -> None:
    """Задача проверена и закрыта. Через месяц Петрова открывает её карточку из «📋 Задачи»,
    чтобы ответить на вопрос «что фактически получено?»: на карточке есть «📎 Файлы (2)», и по
    кнопке приходят файлы, которыми Иванов подтверждал результат. У Иванова в его карточке такой
    кнопки нет — файлы у него и так в чате."""
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    doc = h.api.register_file("document", b"xlsx", file_name="Analysis.xlsx")
    photo = h.api.register_file("photo", b"jpg", mime_type="image/jpeg")
    async with h.db() as s:
        employee = await s.get(User, emp.id)
        manager = await s.get(User, mgr.id)
        sub = await tasks_svc.submit_result(
            s, task_id, employee, fact_text=FACT, fact_value=110,
            attachments=[
                tasks_svc.AttachmentIn(kind="document", file_id=doc.file_id, file_name="Analysis.xlsx"),
                tasks_svc.AttachmentIn(kind="photo", file_id=photo.file_id),
            ],
        )
        await tasks_svc.record_evaluation(s, sub.id, score=110, rationale="План перевыполнен.", source="rules")
        await tasks_svc.review_confirm(s, sub.id, manager)
        await s.commit()

    await h.press(MGR, TaskCB(action="open", task_id=task_id))
    assert "📎 Файлы (2)" in h.buttons(MGR)
    log = await h.press_button(MGR, "Файлы (2)")
    assert {sent.kind for sent in log.files} == {"document", "photo"}
    assert any(sent.file_name == "Analysis.xlsx" or "Analysis.xlsx" in (sent.caption or "") for sent in log.files)

    await h.press(EMP, TaskCB(action="open", task_id=task_id))
    assert not any("Файлы" in button for button in h.buttons(EMP))


# --- Бота остановили посреди оценки ------------------------------------------------------------------


async def test_submission_reaches_manager_after_bot_stopped_mid_evaluation(app: BotHarness, gemini: FakeGemini) -> None:
    """Бота остановили, пока AI оценивал сдачу (обновление на Render прерывает незаконченное, сбой,
    нехватка памяти): сдача сохранена, но без оценки, руководитель о ней не знает, у сотрудника висит
    «⏳ Анализирую…». Задания по расписанию (фоновый цикл — каждые 5 мин) через бюджет AI + 5 мин
    находят такую сдачу: оценка по правилам, руководителю — сдача с кнопками проверки, сотруднику —
    «передан руководителю». Раньше времени (оценка ещё может идти) и повторно — ничего."""
    h = app
    mgr, emp = await _team(h)
    task_id = await _task(h, mgr, emp)
    started = asyncio.Event()

    async def ai_started() -> None:
        started.set()

    gemini.meanwhile = ai_started
    gemini.delay = 3600  # модель «думает»
    pending = asyncio.create_task(_submit(h))
    await asyncio.wait_for(started.wait(), timeout=10)
    pending.cancel()  # остановка бота: web._drain прерывает апдейты, не закончившиеся за SHUTDOWN_GRACE_SEC
    with contextlib.suppress(asyncio.CancelledError):
        await pending

    sub = await _sub(h, task_id)
    assert sub.ai_source is None and sub.decision is None
    assert "Анализирую результат" in h.last_text(EMP)
    assert not h.has_button(MGR, "Подтвердить")

    # Новый экземпляр бота, задания по расписанию.
    created = sub.created_at
    assert await jobs.recover_stalled_evaluations(h.bot, h.sessionmaker, now=created + timedelta(minutes=2)) == 0
    assert not h.has_button(MGR, "Подтвердить")
    assert await jobs.recover_stalled_evaluations(h.bot, h.sessionmaker, now=created + timedelta(minutes=10)) == 1
    assert "Расчёт по правилам" in h.last_text(MGR) and h.has_button(MGR, "Подтвердить 110 %")
    assert "передан руководителю на проверку" in h.last_text(EMP)
    assert await jobs.recover_stalled_evaluations(h.bot, h.sessionmaker, now=created + timedelta(minutes=15)) == 0
    sub = await _sub(h, task_id)
    assert (sub.ai_score, sub.ai_source) == (110, "rules")

    await h.press_button(MGR, "Подтвердить 110 %")
    assert (await h.get_task(task_id)).final_score == 110
