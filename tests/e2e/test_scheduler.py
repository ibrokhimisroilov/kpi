"""Планировщик и уведомления глазами пользователей (SPEC 3.5, 6, 8; ТЗ п. 3 «Бот контролирует сроки»).

Напоминания о сроке, просрочки, тихие часы, непроверенные результаты, еженедельная сводка
руководителю и все функции bot.notify — через фейковый Telegram API (tests/e2e/fakebot.py)
и настоящий Dispatcher (кнопки из напоминаний нажимаются и должны работать).

Путешествие во времени: фикстура ``travel`` (поверх ``clock`` из tests/conftest.py) замораживает
utcnow в сервисах, рендере и хендлерах, а ``run_reminders`` / ``weekly_digest`` получают ``now=``
явно — будто планировщик сработал в этот момент (``Office.tick``).

Календарь (Asia/Tashkent, UTC+5): задачу ставят в пятницу 02.10.2026 в 12:00, срок —
среда 07.10 в 18:00 (через 5 дней). Тихие часы 21:00–08:00, ежедневное напоминание
о просрочке — после 10:00, руководителю о непроверенном результате — через 2 дня.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from aiogram import methods as m
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InputMediaPhoto
from sqlalchemy import select

from bot import notify
from bot.db.models import (
    AttachmentKind,
    DigestLog,
    EventType,
    Priority,
    ReminderLog,
    TaskEvent,
    TaskStatus,
    UserStatus,
)
from bot.scheduler.jobs import run_reminders, weekly_digest
from bot.services import tasks as tasks_svc
from bot.services import users as users_svc
from bot.ui.callbacks import TaskCB
from bot.ui.texts import BTN_MY_TASKS, BTN_SUBMIT
from bot.utils.dates import to_utc

from .fakebot import MANAGER_TG_ID, BotHarness, RequestLog, StoredMessage

MGR = MANAGER_TG_ID  # Петрова Анна Сергеевна — руководитель (ADMIN_IDS)
MGR2 = 1002          # Смирнов Олег Петрович — второй руководитель
EMP = 2001           # Иванов Иван Иванович — исполнитель
EMP2 = 2002          # Сидорова Мария Олеговна — сотрудница

TITLE = "Анализ договоров"
EXPECTED = "Проверить 100 договоров и представить отчёт"
SUBMIT_BUTTONS = ["📤 Сдать результат", "📋 Открыть"]
QUESTIONS = (
    "Что фактически сделано?",
    "Какой получен результат?",
    "Какие документы или материалы подтверждают выполнение?",
)


def local(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Местное время (Ташкент) 2026 года -> naive UTC, как хранится в БД."""
    return to_utc(datetime(2026, month, day, hour, minute))


def created_at() -> datetime:
    return local(10, 2, 12)  # пятница 02.10, 12:00 — задачу ставят


def deadline() -> datetime:
    return local(10, 7, 18)  # среда 07.10, 18:00 — срок через 5 дней


# --- Помощники -------------------------------------------------------------------------------------


async def make_task(
    h: BotHarness,
    *,
    assignee_tg: int = EMP,
    manager_tg: int = MGR,
    title: str = TITLE,
    expected: str = EXPECTED,
    due: datetime | None = None,
    plan_value: float | None = 100,
    plan_unit: str | None = "договоров",
    weight: int = 20,
) -> int:
    """Руководитель ставит задачу (через сервис, как это делает диалог «➕ Поставить задачу»)."""
    async with h.db() as s:
        creator = await users_svc.get_by_tg(s, manager_tg)
        assignee = await users_svc.get_by_tg(s, assignee_tg)
        task = await tasks_svc.create_task(
            s,
            creator=creator,
            assignee_id=assignee.id,
            title=title,
            expected_result=expected,
            deadline=due or deadline(),
            weight=weight,
            plan_value=plan_value,
            plan_unit=plan_unit,
        )
        await s.commit()
        return task.id


async def submit(
    h: BotHarness,
    task_id: int,
    *,
    emp_tg: int = EMP,
    score: float = 110,
    fact_value: float | None = 110,
    attachments: list[tasks_svc.AttachmentIn] | None = None,
) -> int:
    """Сотрудник сдал результат, бот посчитал предварительную оценку (как в диалоге сдачи)."""
    async with h.db() as s:
        emp = await users_svc.get_by_tg(s, emp_tg)
        sub = await tasks_svc.submit_result(
            s,
            task_id,
            emp,
            fact_text="Проверено 110 договоров, в 12 выявлены нарушения",
            result_text="Подготовлен отчёт и рекомендации",
            fact_value=fact_value,
            attachments=attachments or [],
        )
        await tasks_svc.record_evaluation(
            s, sub.id, score=score, rationale="Расчёт по правилам (AI недоступен): план перевыполнен.", source="rules"
        )
        await s.commit()
        return sub.id


async def as_manager(h: BotHarness, action: Any, tg_id: int = MGR) -> Any:
    """Выполнить сервисное действие руководителя: ``await as_manager(h, lambda s, mgr: ...)``."""
    async with h.db() as s:
        manager = await users_svc.get_by_tg(s, tg_id)
        result = await action(s, manager)
        await s.commit()
        return result


async def set_status(h: BotHarness, tg_id: int, status: UserStatus) -> None:
    async with h.db() as s:
        user = await users_svc.get_by_tg(s, tg_id)
        user.status = status
        await s.commit()


async def logged(h: BotHarness, task_id: int) -> set[str]:
    """Какие напоминания по задаче помечены отправленными (ReminderLog)."""
    return set(await h.scalars(select(ReminderLog.kind).where(ReminderLog.task_id == task_id)))


async def reminder_events(h: BotHarness, task_id: int) -> list[str]:
    """Напоминания, записанные в журнал задачи («📜 История»)."""
    events = await h.scalars(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id, TaskEvent.type == EventType.REMINDER)
        .order_by(TaskEvent.id)
    )
    return [event.data.get("kind") for event in events]


_EDITS = (m.EditMessageText, m.EditMessageReplyMarkup, m.EditMessageCaption)


def sent(h: BotHarness, log: RequestLog, chat_id: int) -> list[StoredMessage]:
    """Новые сообщения, которые бот успешно отправил в чат за этот прогон (в текущем виде)."""
    return [
        h.api.messages[(chat_id, message_id)]
        for call in log.calls
        if call.ok and call.chat_id == chat_id and not isinstance(call.method, _EDITS)
        for message_id in call.message_ids
    ]


def methods_to(log: RequestLog, chat_id: int) -> list[str]:
    return [type(call.method).__name__ for call in log.calls if call.chat_id == chat_id]


@dataclass
class Office:
    """Отдел: руководитель Петрова, сотрудник Иванов и задача «Анализ договоров» со сроком через 5 дней."""

    h: BotHarness
    clock: Any
    task_id: int

    async def tick(self, when: datetime) -> RequestLog:
        """Планировщик сработал в момент ``when`` (naive UTC): что бот отправил и сколько."""
        self.clock.set(when)
        return await self.h.capture(run_reminders(self.h.bot, self.h.sessionmaker, now=when))

    async def task(self) -> Any:
        return await self.h.get_task(self.task_id)


async def seed_office(h: BotHarness) -> None:
    await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    await h.seed_user(EMP, "Иванов Иван Иванович", position="Юрист", username="ivanov")


# Хендлеры и планировщик берут utcnow напрямую — подменяем и там, чтобы карточки, кнопки и проверки
# сроков жили в том же «сейчас», что и сервисы (фикстура clock из tests/conftest.py).
_HANDLER_CLOCK_MODULES = (
    "bot.handlers.dashboard",
    "bot.handlers.task_create",
    "bot.handlers.task_propose",
    "bot.handlers.task_review",
    "bot.handlers.task_view",
    "bot.scheduler.jobs",
    "bot.ai.evaluate",
)


@pytest.fixture
def travel(clock: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Машина времени: ``travel.set(local(10, 7, 19))`` — «сейчас» для сервисов, рендера и хендлеров."""
    for name in _HANDLER_CLOCK_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "utcnow", lambda: clock.now)
    clock.set(created_at())
    return clock


@pytest_asyncio.fixture
async def office(app: BotHarness, travel: Any) -> Office:
    clock = travel
    await seed_office(app)
    task_id = await make_task(app)
    return Office(app, clock, task_id)


# =====================================================================================================
# 1. Напоминания до срока
# =====================================================================================================


async def test_reminders_before_deadline_come_once_at_each_threshold(office: Office) -> None:
    """Задача со сроком через 5 дней. Сотрудник получает «осталось 3 дня», «осталось 1 день»
    и в день срока «осталось 2 часа» — каждое ровно один раз, с ожидаемым результатом и кнопками
    «📤 Сдать результат» / «📋 Открыть». Руководителя до срока не беспокоят."""
    h = office.h
    for when in (local(10, 2, 12, 15), local(10, 3, 18), local(10, 4, 17, 45)):
        log = await office.tick(when)
        assert log.result == 0 and not log.calls, f"рано для напоминаний: {log.texts}"

    log = await office.tick(local(10, 4, 18))  # T-3d
    assert log.result == 1 and log.chats == {EMP}
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith("⏰ До срока задачи «Анализ договоров» осталось 3 дня (7 октября (ср), 18:00).")
    assert "🎯 Ожидаемый результат: Проверить 100 договоров и представить отчёт" in msg.text
    assert "📊 План: 100 договоров" in msg.text  # плановое число — как в карточке задачи
    assert msg.button_texts == SUBMIT_BUTTONS

    for when in (local(10, 4, 18, 15), local(10, 4, 20, 45), local(10, 5, 12), local(10, 6, 17, 45)):
        assert not (await office.tick(when)).calls, "повторный запуск не должен слать то же напоминание"

    log = await office.tick(local(10, 6, 18))  # T-1d
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith("⏰ До срока задачи «Анализ договоров» осталось 1 день (7 октября (ср), 18:00).")
    assert msg.button_texts == SUBMIT_BUTTONS

    log = await office.tick(local(10, 7, 16))  # T-2h
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith("⏰ До срока задачи «Анализ договоров» осталось 2 часа (7 октября (ср), 18:00).")
    assert not (await office.tick(local(10, 7, 17, 45))).calls
    assert not (await office.tick(local(10, 7, 18))).calls  # ровно в срок — ещё не просрочено

    assert h.sent_to(MGR) == []
    assert await logged(h, office.task_id) == {"before_3d", "before_1d", "before_hours"}
    assert await reminder_events(h, office.task_id) == ["before_3d", "before_1d", "before_hours"]


async def test_urgent_task_gets_single_nearest_reminder(app: BotHarness, travel: Any) -> None:
    """Задачу поставили в пятницу в 12:00 со сроком на завтра 10:00: сразу приходит одно
    «осталось 22 часа», а не пачка «3 дня» + «1 день». Порог «за 3 часа» (07:00) попадает
    в тихие часы — напоминание приходит в 08:00: «осталось 2 часа»."""
    h = app
    clock = travel
    await seed_office(h)
    task_id = await make_task(h, due=local(10, 3, 10))
    office = Office(h, clock, task_id)

    log = await office.tick(local(10, 2, 12, 15))
    [msg] = sent(h, log, EMP)
    assert "осталось 22 часа (3 октября (сб), 10:00)" in msg.text
    assert await logged(h, task_id) == {"before_3d", "before_1d"}
    assert not (await office.tick(local(10, 2, 18))).calls

    assert not (await office.tick(local(10, 3, 7, 30))).calls  # тихие часы
    log = await office.tick(local(10, 3, 8))
    [msg] = sent(h, log, EMP)
    assert "осталось 2 часа (3 октября (сб), 10:00)" in msg.text
    assert await logged(h, task_id) == {"before_3d", "before_1d", "before_hours"}


async def test_employee_opens_task_from_reminder(office: Office) -> None:
    """Сотрудник нажимает «📋 Открыть» в напоминании — видит карточку задачи со сроком и кнопкой сдачи."""
    h = office.h
    office.clock.set(local(10, 6, 18))
    await office.tick(local(10, 6, 18))
    log = await h.press_button(EMP, "Открыть")
    assert log.answers and log.alert is None
    card = h.last_text(EMP)
    assert "осталось 1 дн." in card
    assert f"Задача #{office.task_id}" in card and "Анализ договоров" in card
    assert "7 октября (ср), 18:00" in card
    assert "📤 Сдать результат" in h.buttons(EMP)


# =====================================================================================================
# 2. Срок истёк: вопросы ТЗ сотруднику, уведомление руководителю, ежедневные напоминания
# =====================================================================================================


async def test_deadline_passed_employee_asked_questions_and_manager_warned(office: Office) -> None:
    """Через час после срока сотрудник получает «⌛ Срок задачи … истёк» с тремя вопросами ТЗ
    и кнопкой «📤 Сдать результат», а руководитель — «⚠️ Просрочена задача #N … (Иванов И. И.)»
    с кнопкой «📋 Открыть»."""
    h = office.h
    log = await office.tick(local(10, 7, 19))
    assert log.result == 2

    [emp_msg] = sent(h, log, EMP)
    assert emp_msg.text.startswith("⌛ Срок задачи «Анализ договоров» истёк (7 октября (ср), 18:00).")
    for question in QUESTIONS:
        assert question in emp_msg.text
    assert emp_msg.button_texts == SUBMIT_BUTTONS

    [mgr_msg] = sent(h, log, MGR)
    assert mgr_msg.text.startswith(f"⚠️ Просрочена задача #{office.task_id} «Анализ договоров» (Иванов И. И.)")
    assert "📅 Срок: 7 октября (ср), 18:00 · исполнитель не подтвердил получение" in mgr_msg.text
    assert "Результат пока не сдан." in mgr_msg.text
    assert mgr_msg.button_texts == ["📋 Открыть"]

    assert {"deadline_passed", "overdue_manager"} <= await logged(h, office.task_id)
    assert not (await office.tick(local(10, 7, 19, 15))).calls


async def test_manager_overdue_notice_mentions_accepted_task(office: Office) -> None:
    """Если сотрудник принял задачу в работу, руководитель видит «принята в работу», а не
    «исполнитель не подтвердил получение»."""
    h = office.h
    await h.capture(notify.notify_new_task(h.bot, await office.task()))
    log = await h.press(EMP, TaskCB(action="accept", task_id=office.task_id))
    assert log.alert and "Принято в работу" in log.alert
    log = await office.tick(local(10, 7, 19))
    [mgr_msg] = sent(h, log, MGR)
    assert "· принята в работу" in mgr_msg.text
    assert "не подтвердил" not in mgr_msg.text


async def test_manager_opens_overdue_task_from_notice(office: Office) -> None:
    """Руководитель нажимает «📋 Открыть» в уведомлении о просрочке — карточка со статусом
    «⏰ Просрочена» и кнопками управления задачей."""
    h = office.h
    await office.tick(local(10, 7, 19))
    log = await h.press_button(MGR, "Открыть")
    assert log.answers
    card = h.last_text(MGR)
    assert f"Задача #{office.task_id}" in card
    assert "⏰ Просрочена" in card
    assert "Иванов" in card
    assert any("История" in text for text in h.buttons(MGR))


async def test_submit_button_in_overdue_reminder_starts_submission(office: Office) -> None:
    """Сотрудник жмёт «📤 Сдать результат» прямо в напоминании «⌛ Срок истёк» и проходит весь диалог:
    что сделано → результат → факт числом → файл → «📤 Отправить». Руководитель получает
    план↔факт с пометкой «с опозданием», кнопки проверки и сам файл; после сдачи напоминания
    о просрочке прекращаются, а напоминание в чате сотрудника осталось как было."""
    h = office.h
    log = await office.tick(local(10, 7, 19))
    [reminder] = sent(h, log, EMP)

    log = await h.press_button(EMP, "Сдать результат", reminder.message_id)
    assert log.answers and log.alert is None
    intro = h.last_text(EMP)
    assert intro.startswith("📤 Сдача результата")
    assert f"Задача #{office.task_id}: Анализ договоров" in intro
    assert "Шаг 1 из 4. Что фактически сделано?" in intro
    assert await h.get_state(EMP) == "SubmitSG:fact"
    # Диалог начинается новым сообщением — само напоминание остаётся в чате как было.
    assert reminder.edits == 0 and not reminder.deleted
    assert reminder.button_texts == SUBMIT_BUTTONS

    await h.send_text(EMP, "Проверено 110 договоров, в 12 выявлены нарушения")
    assert "Какой получен результат?" in h.last_text(EMP)
    await h.send_text(EMP, "Подготовлен отчёт и рекомендации")
    assert "Фактическое значение?" in h.last_text(EMP) and "100 договоров" in h.last_text(EMP)
    await h.send_text(EMP, "110")
    assert "Какие документы или материалы подтверждают выполнение?" in h.last_text(EMP)
    await h.send_document(EMP, "Анализ.xlsx")
    await h.press_button(EMP, "Готово")
    summary = h.last_text(EMP)
    assert "⚠️ Срок уже прошёл" in summary and "Анализ.xlsx" in summary

    log = await h.press_button(EMP, "Отправить")
    assert "Результат отправлен руководителю" in h.last_text(EMP)
    to_manager = log.to(MGR)
    assert "📝 Результат по задаче #%d" % office.task_id in to_manager.text
    assert "с опозданием" in to_manager.text
    assert [doc.file_name for doc in to_manager.documents] == ["Анализ.xlsx"]
    review_msg = h.find_message(MGR, "Результат по задаче")
    assert "↩ На доработку" in review_msg.button_texts

    task = await office.task()
    assert task.status == TaskStatus.SUBMITTED
    assert task.last_submission.is_late is True
    assert task.last_submission.fact_value == 110
    assert [att.file_name for att in task.last_submission.attachments] == ["Анализ.xlsx"]

    # Результат сдан — ежедневных напоминаний о просрочке больше нет.
    for when in (local(10, 8, 10), local(10, 8, 15)):
        assert not (await office.tick(when)).to(EMP).calls


async def test_submit_from_early_reminder_stops_further_reminders(office: Office) -> None:
    """Сотрудник получил «осталось 3 дня» и сразу сдал результат из напоминания (без файлов,
    шаги «результат» и «факт числом» пропустил). Сдача — «в срок», и больше никаких «осталось
    1 день» / «срок истёк»; руководитель получает результат с кнопками проверки."""
    h = office.h
    await office.tick(local(10, 4, 18))
    office.clock.set(local(10, 4, 18, 5))
    await h.press_button(EMP, "Сдать результат")
    assert "Шаг 1 из 4" in h.last_text(EMP)
    await h.send_text(EMP, "Проверено 100 договоров")
    await h.press_button(EMP, "Пропустить")
    await h.press_button(EMP, "Пропустить")
    await h.press_button(EMP, "Без файлов")
    summary = h.last_text(EMP)
    assert "Срок уже прошёл" not in summary and "📎 Файлы: не приложены" in summary
    log = await h.press_button(EMP, "Отправить")
    assert "в срок" in log.to(MGR).text
    assert "✏️ Изменить оценку" in h.find_message(MGR, "Результат по задаче").button_texts

    task = await office.task()
    assert task.status == TaskStatus.SUBMITTED and task.last_submission.is_late is False
    for when in (local(10, 6, 18), local(10, 7, 16), local(10, 7, 19), local(10, 8, 10)):
        assert not (await office.tick(when)).to(EMP).calls
    assert len([text for text in h.sent_to(EMP) if text.startswith("⏰")]) == 1


async def test_submit_button_in_reminder_rejects_other_employee(office: Office) -> None:
    """Чужой сотрудник «подделывает» нажатие кнопки сдачи из напоминания — получает отказ,
    диалог сдачи у него не начинается."""
    h = office.h
    await h.seed_user(EMP2, "Сидорова Мария Олеговна")
    await office.tick(local(10, 7, 19))
    data = h.find_button(EMP, "Сдать результат")
    log = await h.press(EMP2, data)
    assert log.alert and "исполнитель" in log.alert
    assert await h.get_state(EMP2) is None


async def test_submit_button_in_old_reminder_after_task_cancelled(office: Office) -> None:
    """Руководитель отменил задачу, а сотрудник жмёт «📤 Сдать результат» в старом напоминании —
    alert «Задача не в работе», диалог не начинается."""
    h = office.h
    await office.tick(local(10, 7, 19))
    await as_manager(h, lambda s, mgr: tasks_svc.cancel_task(s, office.task_id, mgr, "Неактуально"))
    log = await h.press_button(EMP, "Сдать результат")
    assert log.alert and ("не в работе" in log.alert or "отменен" in log.alert.lower())
    assert await h.get_state(EMP) is None


async def test_overdue_daily_reminder_after_10_local_once_a_day(office: Office) -> None:
    """Сотрудник так и не сдал результат: на следующий день — одно напоминание после 10:00
    (в 8:00 и 9:45 — ещё нет), назавтра — снова одно. Руководителю повторно не пишут."""
    h = office.h
    await office.tick(local(10, 7, 19))
    for when in (local(10, 7, 20, 45), local(10, 8, 8), local(10, 8, 9, 45)):
        assert not (await office.tick(when)).calls

    log = await office.tick(local(10, 8, 10))
    assert log.chats == {EMP} and log.result == 1
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith(
        "⏰ Задача «Анализ договоров» просрочена на 16 часов (срок — 7 октября (ср), 18:00)."
    )
    assert "что фактически сделано" in msg.text
    assert msg.button_texts == SUBMIT_BUTTONS

    for when in (local(10, 8, 10, 15), local(10, 8, 20, 45), local(10, 9, 9, 45)):
        assert not (await office.tick(when)).calls

    log = await office.tick(local(10, 9, 10))
    assert log.chats == {EMP}
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith("⏰ Задача «Анализ договоров» просрочена на ")

    kinds = await logged(h, office.task_id)
    assert {"deadline_passed", "overdue_manager", "overdue_2026-10-08", "overdue_2026-10-09"} <= kinds
    # В день, когда ушло «срок истёк», отдельное ежедневное напоминание не нужно — помечено заранее.
    assert "overdue_2026-10-07" in kinds
    assert len(h.sent_to(MGR)) == 1


async def test_overdue_daily_reminder_days_match_task_card(office: Office) -> None:
    """Через 1 день 16 часов после срока напоминание и карточка задачи должны называть одну и ту же
    просрочку: в карточке «просрочено на 1 дн.», значит и в напоминании «просрочена на 1 день»
    (сейчас напоминание округляет до ближайшего и пишет «на 2 дня»)."""
    h = office.h
    await office.tick(local(10, 7, 19))
    await office.tick(local(10, 8, 10))
    log = await office.tick(local(10, 9, 10))
    [msg] = sent(h, log, EMP)
    reminder_text = msg.text
    await h.press_button(EMP, "Открыть", msg.message_id)
    assert "просрочено на 1 дн." in h.last_text(EMP)
    assert "просрочена на 1 день" in reminder_text


# =====================================================================================================
# 3. Тихие часы
# =====================================================================================================


@pytest.mark.parametrize(
    ("day", "hour", "minute"),
    [(7, 21, 0), (7, 23, 30), (8, 3, 0), (8, 7, 59)],
    ids=["21:00", "23:30", "03:00", "07:59"],
)
async def test_quiet_hours_nothing_sent_until_8(office: Office, day: int, hour: int, minute: int) -> None:
    """Тихие часы 21:00–08:00: срок уже истёк, но бот молчит и ничего не помечает отправленным.
    В 08:00 приходят «⌛ Срок истёк» сотруднику и «⚠️ Просрочена» руководителю, а ежедневное
    напоминание в этот же день (в 10:00) уже не дублирует их."""
    h = office.h
    log = await office.tick(local(10, day, hour, minute))
    assert log.result == 0 and not log.calls
    assert await logged(h, office.task_id) == set()

    log = await office.tick(local(10, 8, 8))
    assert log.result == 2
    assert sent(h, log, EMP)[0].text.startswith("⌛ Срок задачи «Анализ договоров» истёк")
    assert sent(h, log, MGR)[0].text.startswith("⚠️ Просрочена задача")
    assert not (await office.tick(local(10, 8, 10))).calls


async def test_quiet_hours_boundaries(office: Office) -> None:
    """В 20:59 бот ещё пишет (последний момент до тишины), в 21:00 — уже нет."""
    h = office.h
    assert not (await office.tick(local(10, 7, 21))).calls  # 21:00 — тишина, ничего не помечено
    log = await office.tick(local(10, 7, 20, 59))  # а сработай планировщик минутой раньше — ушло бы
    assert log.result == 2 and log.chats == {EMP, MGR}
    assert await logged(h, office.task_id) >= {"deadline_passed", "overdue_manager"}


# =====================================================================================================
# 4. Непроверенный результат — напоминание руководителю
# =====================================================================================================


async def test_manager_reminded_about_unreviewed_result_once_a_day(office: Office) -> None:
    """Сотрудник сдал результат в срок (06.10 18:00), руководитель не проверяет. Через 2 дня —
    «📝 Ждёт проверки 2 дня …» с кнопкой «🔍 Проверить», не чаще раза в день. Сотрудника, сдавшего
    в срок, после истечения срока не дёргают. Кнопка открывает проверку, после решения напоминания
    прекращаются."""
    h, clock = office.h, office.clock
    clock.set(local(10, 6, 18))
    await submit(h, office.task_id)

    for when in (local(10, 7, 19), local(10, 8, 10), local(10, 8, 17, 59)):
        assert not (await office.tick(when)).calls, "до 2 суток напоминать рано"

    log = await office.tick(local(10, 8, 18, 15))
    assert log.chats == {MGR} and log.result == 1
    [msg] = sent(h, log, MGR)
    assert msg.text.startswith(
        f"📝 Ждёт проверки 2 дня: задача #{office.task_id} «Анализ договоров» (Иванов И. И.)"
    )
    assert "📤 Сдано: 06.10 18:00" in msg.text
    assert msg.button_texts == ["🔍 Проверить"]

    assert not (await office.tick(local(10, 8, 20))).calls  # в тот же день — не повторяем
    log = await office.tick(local(10, 9, 8))  # на следующее утро — снова
    assert log.chats == {MGR}
    assert "Ждёт проверки 2 дня" in log.text
    assert {"review_2026-10-08", "review_2026-10-09"} <= await logged(h, office.task_id)

    log = await h.press_button(MGR, "Проверить")
    assert log.answers and log.alert is None
    review = h.last_text(MGR)
    assert f"📝 Результат по задаче #{office.task_id}" in review
    assert "110 %" in review
    assert "✅ Подтвердить 110 %" in h.buttons(MGR)
    await h.press_button(MGR, "Подтвердить 110")
    task = await office.task()
    assert task.status == TaskStatus.DONE and task.final_score == 110

    for when in (local(10, 10, 9), local(10, 11, 12)):
        assert not (await office.tick(when)).calls
    assert all(not text.startswith(("⌛", "⏰")) for text in h.sent_to(EMP))


async def test_review_reminder_button_after_another_manager_already_reviewed(office: Office) -> None:
    """Напоминание «🔍 Проверить» пришло, но результат уже проверен — нажатие показывает alert
    «уже обработан», а не ошибку."""
    h, clock = office.h, office.clock
    clock.set(local(10, 6, 18))
    sub_id = await submit(h, office.task_id)
    await office.tick(local(10, 8, 18, 15))
    await as_manager(h, lambda s, mgr: tasks_svc.review_confirm(s, sub_id, mgr))
    log = await h.press_button(MGR, "Проверить")
    assert log.alert and "обработан" in log.alert


# =====================================================================================================
# 5. Повторные запуски, недоступные получатели, сбои
# =====================================================================================================


async def test_repeated_runs_are_idempotent(office: Office) -> None:
    """Планировщик срабатывает несколько раз подряд в один и тот же момент (перезапуск бота,
    наложение интервалов) — каждое напоминание уходит один раз."""
    h = office.h
    first = await office.tick(local(10, 7, 19))
    assert first.result == 2
    for _ in range(3):
        again = await office.tick(local(10, 7, 19))
        assert again.result == 0 and not again.calls
    assert len(h.sent_to(EMP)) == 1 and len(h.sent_to(MGR)) == 1
    assert await reminder_events(h, office.task_id) == ["deadline_passed", "overdue_manager"]


async def test_employee_blocked_bot_marked_sent_without_crash(office: Office) -> None:
    """Сотрудник заблокировал бота: напоминание не доходит, бот не падает и не «долбит» его каждые
    15 минут; руководитель о просрочке всё равно узнаёт. Когда сотрудник разблокировал бота,
    следующее ежедневное напоминание доходит."""
    h = office.h
    h.api.blocked_chats.add(EMP)

    log = await office.tick(local(10, 4, 18))
    assert log.result == 0
    assert [type(error).__name__ for error in log.errors] == ["TelegramForbiddenError"]
    assert "before_3d" in await logged(h, office.task_id)
    assert await reminder_events(h, office.task_id) == []  # недоставленное в историю не пишем
    assert not (await office.tick(local(10, 4, 18, 15))).calls

    log = await office.tick(local(10, 7, 19))
    assert log.result == 1
    assert sent(h, log, MGR)[0].text.startswith("⚠️ Просрочена задача")
    assert {"deadline_passed", "overdue_manager"} <= await logged(h, office.task_id)
    assert not (await office.tick(local(10, 7, 19, 15))).calls

    h.api.blocked_chats.discard(EMP)
    log = await office.tick(local(10, 8, 10))
    assert log.result == 1
    assert sent(h, log, EMP)[0].text.startswith("⏰ Задача «Анализ договоров» просрочена")


async def test_manager_blocked_bot_overdue_notice_marked_sent(office: Office) -> None:
    """Руководитель заблокировал бота: сотрудник получает «срок истёк», уведомление руководителю
    помечается отправленным и больше не пытается уйти."""
    h = office.h
    h.api.blocked_chats.add(MGR)
    log = await office.tick(local(10, 7, 19))
    assert log.result == 1 and sent(h, log, EMP)
    assert {"deadline_passed", "overdue_manager"} <= await logged(h, office.task_id)
    assert not (await office.tick(local(10, 7, 19, 15))).calls


async def test_network_failure_retried_on_next_run(office: Office, monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбой сети при отправке напоминания: оно не помечается отправленным и приходит при следующем
    запуске планировщика (а не теряется)."""
    h = office.h
    failing = {EMP}
    original = h.api._handle

    async def flaky(bot: Any, method: Any, call: Any) -> Any:
        if call.chat_id in failing and isinstance(method, m.SendMessage):
            raise TelegramNetworkError(method=method, message="HTTP Client says - ClientConnectorError")
        return await original(bot, method, call)

    monkeypatch.setattr(h.api, "_handle", flaky)
    log = await office.tick(local(10, 4, 18))
    assert log.result == 0 and log.errors
    assert "before_3d" not in await logged(h, office.task_id)

    failing.clear()
    log = await office.tick(local(10, 4, 18, 15))
    assert log.result == 1
    assert "осталось 3 дня" in sent(h, log, EMP)[0].text
    assert "before_3d" in await logged(h, office.task_id)


async def test_deactivated_employee_gets_nothing_manager_still_warned(office: Office) -> None:
    """Сотрудника заблокировали в боте (уволен): напоминания ему не уходят (помечаются),
    а руководитель о просроченной задаче узнаёт."""
    h = office.h
    await set_status(h, EMP, UserStatus.BLOCKED)
    assert not (await office.tick(local(10, 4, 18))).calls
    assert "before_3d" in await logged(h, office.task_id)
    log = await office.tick(local(10, 7, 19))
    assert log.chats == {MGR}
    assert sent(h, log, MGR)[0].text.startswith("⚠️ Просрочена задача")


async def test_overdue_notice_goes_to_responsible_manager_only(app: BotHarness, travel: Any) -> None:
    """Руководителей двое: уведомление о просрочке получает тот, кто ставил задачу. Если его
    заблокировали в боте — уведомление получают все активные руководители."""
    h = app
    clock = travel
    clock.set(created_at())
    await seed_office(h)
    await h.seed_user(MGR2, "Смирнов Олег Петрович", role="manager")
    first = await make_task(h, manager_tg=MGR2)
    second = await make_task(h, manager_tg=MGR, title="Отчёт по поставщикам", due=local(10, 8, 18))
    office = Office(h, clock, first)

    log = await office.tick(local(10, 7, 19))
    assert sent(h, log, MGR2)[0].text.startswith(f"⚠️ Просрочена задача #{first}")
    assert not sent(h, log, MGR)

    await set_status(h, MGR, UserStatus.BLOCKED)
    log = await office.tick(local(10, 8, 19))
    to_mgr2 = [msg.text for msg in sent(h, log, MGR2)]
    assert any(text.startswith(f"⚠️ Просрочена задача #{second} «Отчёт по поставщикам»") for text in to_mgr2)
    assert not log.to(MGR).calls


async def test_closed_and_proposed_tasks_get_no_reminders(app: BotHarness, travel: Any) -> None:
    """Отменённая, выполненная и ещё не подтверждённая (внесённая сотрудником) задачи
    со сроком в прошлом — никаких напоминаний ни сотруднику, ни руководителю."""
    h = app
    clock = travel
    clock.set(created_at())
    await seed_office(h)
    cancelled = await make_task(h, title="Отменённая")
    done = await make_task(h, title="Выполненная")
    async with h.db() as s:
        emp = await users_svc.get_by_tg(s, EMP)
        proposal = await tasks_svc.propose_task(
            s, employee=emp, title="Устное поручение", expected_result="Сделать", deadline=deadline()
        )
        await s.commit()
    await as_manager(h, lambda s, mgr: tasks_svc.cancel_task(s, cancelled, mgr))
    clock.set(local(10, 5, 12))
    sub_id = await submit(h, done)
    await as_manager(h, lambda s, mgr: tasks_svc.review_confirm(s, sub_id, mgr))

    office = Office(h, clock, done)
    for when in (local(10, 6, 18), local(10, 7, 16), local(10, 7, 19), local(10, 8, 10), local(10, 10, 12)):
        log = await office.tick(when)
        assert log.result == 0 and not log.calls, log.texts
    for task_id in (cancelled, done, proposal.id):
        assert await logged(h, task_id) == set()


async def test_deadline_extended_reminders_start_over(office: Office) -> None:
    """После «осталось 1 день» руководитель перенёс срок на неделю: по старому сроку ничего
    не приходит, а по новому напоминания идут заново («осталось 3 дня» — снова)."""
    h, clock = office.h, office.clock
    await office.tick(local(10, 4, 18))
    await office.tick(local(10, 6, 18))
    clock.set(local(10, 6, 19))
    await as_manager(h, lambda s, mgr: tasks_svc.update_task(s, office.task_id, mgr, deadline=local(10, 14, 18)))
    assert await logged(h, office.task_id) == set()

    for when in (local(10, 7, 16), local(10, 7, 19), local(10, 8, 10)):
        assert not (await office.tick(when)).calls
    log = await office.tick(local(10, 11, 18))
    [msg] = sent(h, log, EMP)
    assert "осталось 3 дня (14 октября (ср), 18:00)" in msg.text


async def test_rework_with_new_deadline_restarts_reminders(office: Office) -> None:
    """Результат вернули на доработку с новым сроком: задача снова «в работе», напоминания идут
    по новому сроку, а по прошедшему старому сроку «⌛ истёк» не приходит."""
    h, clock = office.h, office.clock
    clock.set(local(10, 7, 12))
    sub_id = await submit(h, office.task_id)
    clock.set(local(10, 8, 11))
    await as_manager(
        h,
        lambda s, mgr: tasks_svc.review_rework(s, sub_id, mgr, "Добавьте рекомендации", new_deadline=local(10, 12, 18)),
    )
    assert not (await office.tick(local(10, 8, 11, 15))).calls
    log = await office.tick(local(10, 9, 18))
    [msg] = sent(h, log, EMP)
    assert "осталось 3 дня (12 октября (пн), 18:00)" in msg.text
    assert msg.button_texts == SUBMIT_BUTTONS

    log = await office.tick(local(10, 12, 19))
    assert sent(h, log, EMP)[0].text.startswith("⌛ Срок задачи «Анализ договоров» истёк (12 октября (пн), 18:00)")
    assert sent(h, log, MGR)[0].text.startswith("⚠️ Просрочена задача")


# =====================================================================================================
# 6. Еженедельная сводка руководителю
# =====================================================================================================


async def seed_week(h: BotHarness, clock: Any) -> None:
    """Неделя 05.10–11.10: Иванов сдал «Анализ договоров» (110 %, вес 30) и ещё одну задачу
    (на проверке), Сидорова просрочила «Сверку с поставщиками» (вес 20, 0 %) и внесла поручение."""
    clock.set(created_at())
    await seed_office(h)
    await h.seed_user(MGR2, "Смирнов Олег Петрович", role="manager")
    await h.seed_user(EMP2, "Сидорова Мария Олеговна", position="Экономист")
    done = await make_task(h, weight=30)
    await make_task(h, assignee_tg=EMP2, title="Сверка с поставщиками", due=local(10, 8, 18), plan_value=None)
    review = await make_task(h, title="Отчёт по претензиям", due=local(10, 9, 18), weight=10)
    async with h.db() as s:
        emp2 = await users_svc.get_by_tg(s, EMP2)
        await tasks_svc.propose_task(
            s,
            employee=emp2,
            title="Устное поручение",
            expected_result="Подготовить справку",
            deadline=local(10, 20, 18),
        )
        await s.commit()
    clock.set(local(10, 6, 12))
    sub_id = await submit(h, done)
    await as_manager(h, lambda s, mgr: tasks_svc.review_confirm(s, sub_id, mgr))
    clock.set(local(10, 9, 12))
    await submit(h, review)


async def test_weekly_digest_to_every_manager(app: BotHarness, travel: Any) -> None:
    """Понедельник 9:00: каждый руководитель получает сводку за прошлую неделю — KPI команды
    и каждого сотрудника, что ждёт решения, кнопки периодов и сотрудников. Сотрудникам сводка
    не приходит."""
    h = app
    clock = travel
    await seed_week(h, clock)
    now = local(10, 12, 9)
    clock.set(now)
    log = await h.capture(weekly_digest(h.bot, h.sessionmaker, now=now))
    assert log.chats == {MGR, MGR2}

    for manager in (MGR, MGR2):
        [msg] = sent(h, log, manager)
        text = msg.text
        assert text.startswith("🗓 Еженедельная сводка")
        assert "Ждут вашего решения: 📝 на проверке — 1 · 📥 предложений — 1" in text
        assert "📊 Команда · Неделя 05.10–11.10.2026" in text
        assert "Эффективность команды: 66 %" in text  # (30×110 + 20×0) / 50
        assert "1. Иванов И. И. — 110 %" in text
        assert "2. Сидорова М. О. — 0 %" in text
        assert "👤 Иванов И. И. — 110 %" in msg.button_texts
        assert "👤 Сидорова М. О. — 0 %" in msg.button_texts
        assert any("Неделя" in button for button in msg.button_texts)
        # То, что ждёт решения, — сразу кнопками, без похода через меню.
        assert msg.button_texts[:2] == ["📝 На проверке (1)", "📥 Предложения (1)"]
    assert h.sent_to(EMP) == [] and h.sent_to(EMP2) == []

    log = await h.press_button(MGR, "На проверке (1)")
    assert log.answers and log.alert is None
    assert "📝 На проверке — 1 результат" in h.last_text(MGR) and "Отчёт по претензиям" in h.last_text(MGR)
    log = await h.press_button(MGR2, "Предложения (1)")
    assert log.answers and log.alert is None
    assert "Предложения сотрудников" in h.last_text(MGR2) and "Устное поручение" in h.last_text(MGR2)


async def test_weekly_digest_buttons_work(app: BotHarness, travel: Any) -> None:
    """Руководитель нажимает в сводке кнопку сотрудника — открывается его карточка эффективности;
    кнопка периода «Месяц» переключает дашборд команды."""
    h = app
    clock = travel
    await seed_week(h, clock)
    now = local(10, 12, 9)
    clock.set(now)
    await h.capture(weekly_digest(h.bot, h.sessionmaker, now=now))

    log = await h.press_button(MGR, "Иванов И. И.")
    assert log.answers and log.alert is None
    assert "Иванов" in h.last_text(MGR)
    assert any("История оценок" in button for button in h.buttons(MGR))
    # Кнопка «👤 Иванов — 110 %» из сводки за прошлую неделю открывает карточку за ту же неделю
    # (а не за текущую, где в понедельник утром ещё «нет данных»), «◀ К команде» — к ней же.
    assert "Неделя 05.10–11.10.2026" in h.last_text(MGR) and "110 %" in h.last_text(MGR)
    await h.press_button(MGR, "К команде")
    assert "📊 Команда · Неделя 05.10–11.10.2026" in h.last_text(MGR)

    await h.capture(weekly_digest(h.bot, h.sessionmaker, now=now))
    log = await h.press_button(MGR, "Месяц")
    assert log.answers and log.alert is None
    assert "📊 Команда" in h.last_text(MGR)


async def test_weekly_digest_survives_blocked_and_deactivated_managers(app: BotHarness, travel: Any) -> None:
    """Один руководитель заблокировал бота, другого отключили в боте: сводка без ошибок доходит
    до оставшегося, отключённому не отправляется вовсе."""
    h = app
    clock = travel
    await seed_week(h, clock)
    await h.seed_user(1003, "Ким Ольга Викторовна", role="manager")
    await set_status(h, 1003, UserStatus.BLOCKED)
    h.api.blocked_chats.add(MGR)
    now = local(10, 12, 9)
    log = await h.capture(weekly_digest(h.bot, h.sessionmaker, now=now))
    assert sent(h, log, MGR2)[0].text.startswith("🗓 Еженедельная сводка")
    assert [type(error).__name__ for error in log.errors] == ["TelegramForbiddenError"]
    assert not log.to(1003).calls


async def test_weekly_digest_without_employees_sends_nothing(app: BotHarness, travel: Any) -> None:
    """В боте пока только руководитель — пустую сводку не шлём."""
    h = app
    clock = travel
    await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    log = await h.capture(weekly_digest(h.bot, h.sessionmaker, now=local(10, 12, 9)))
    assert not log.calls


async def test_weekly_digest_new_employee_without_tasks(app: BotHarness, travel: Any) -> None:
    """Сотрудник есть, задач на прошлой неделе не было — сводка приходит, «нет данных» / «задач нет»."""
    h = app
    clock = travel
    await seed_office(h)
    log = await h.capture(weekly_digest(h.bot, h.sessionmaker, now=local(10, 12, 9)))
    [msg] = sent(h, log, MGR)
    assert "Эффективность команды: нет данных" in msg.text
    assert "Иванов И. И. — нет данных" in msg.text and "задач нет" in msg.text
    assert "Ждут вашего решения" not in msg.text


async def test_weekly_digest_large_team_fits_telegram_limits(app: BotHarness, travel: Any) -> None:
    """В отделе 70 сотрудников с длинными ФИО: сводка всё равно доходит (текст не длиннее лимита
    Telegram, кнопок не больше разумного), без ошибок разметки."""
    h = app
    await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    for index in range(70):
        await h.seed_user(3000 + index, f"Константинопольский{index:02d} Александр Владиславович")
    log = await h.capture(weekly_digest(h.bot, h.sessionmaker, now=local(10, 12, 9)))
    assert not log.errors and not h.api.bug_errors
    [msg] = sent(h, log, MGR)
    assert msg.text.startswith("🗓 Еженедельная сводка") and len(msg.text) <= 4096
    assert "… и ещё" in msg.text and "сотрудник" in msg.text  # остальные свёрнуты в одну строку
    assert len(msg.buttons) <= 100


async def test_scheduler_setup_jobs(app: BotHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Расписание: напоминания — каждые 15 минут, сводка — по понедельникам в 9:00 по Ташкенту;
    DIGEST_WEEKDAY=4 / DIGEST_HOUR=17 переносит её на пятницу 17:00, неверные значения —
    обратно на понедельник 9:00 (бот не падает)."""
    from bot.config import get_settings
    from bot.scheduler.jobs import setup_scheduler

    def fields(job: Any) -> dict[str, str]:
        return {field.name: str(field) for field in job.trigger.fields}

    def configure(**env: str) -> Any:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return setup_scheduler(h.bot, h.sessionmaker)

    h = app
    scheduler = configure(SCHEDULER_INTERVAL_MIN="15", DIGEST_WEEKDAY="0", DIGEST_HOUR="9")
    reminders_job = scheduler.get_job("reminders")
    assert reminders_job.trigger.interval.total_seconds() == 15 * 60
    digest = fields(scheduler.get_job("weekly_digest"))
    assert (digest["day_of_week"], digest["hour"], digest["minute"]) == ("mon", "9", "0")
    assert str(scheduler.get_job("weekly_digest").trigger.timezone) == "Asia/Tashkent"

    digest = fields(configure(DIGEST_WEEKDAY="4", DIGEST_HOUR="17").get_job("weekly_digest"))
    assert (digest["day_of_week"], digest["hour"]) == ("fri", "17")
    digest = fields(configure(DIGEST_WEEKDAY="9", DIGEST_HOUR="30").get_job("weekly_digest"))
    assert (digest["day_of_week"], digest["hour"]) == ("mon", "9")


def next_digest_run(app: BotHarness, monkeypatch: pytest.MonkeyPatch, started: datetime) -> datetime:
    """Бот запущен в момент started (местное время): когда планировщик отправит сводку."""
    import apscheduler.schedulers.base as aps_base

    from bot.config import get_settings
    from bot.scheduler import jobs

    tz = get_settings().tz
    started = started.replace(tzinfo=tz)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return started.astimezone(tz) if tz is not None else started.replace(tzinfo=None)

    monkeypatch.setattr(aps_base, "datetime", FrozenDatetime)  # «сейчас» APScheduler
    monkeypatch.setattr(jobs, "utcnow", lambda: to_utc(started))  # «сейчас» бота
    scheduler = jobs.setup_scheduler(app.bot, app.sessionmaker)
    scheduler.start(paused=True)
    try:
        job = scheduler.get_job("weekly_digest")
        assert job.kwargs == {"once": True}  # по расписанию — не больше одной сводки за неделю
        return job.next_run_time.astimezone(tz).replace(tzinfo=None)
    finally:
        scheduler.shutdown(wait=False)


async def test_weekly_digest_not_lost_when_bot_started_after_9(
    app: BotHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Компьютер с ботом включили в понедельник в 9:30 (сводка была в 9:00, бот был выключен).
    Как обещает настройка «опоздали не больше чем на 6 ч — отправить», сводка за прошлую неделю
    уходит сегодня через минуту после запуска, а не через неделю."""
    next_run = next_digest_run(app, monkeypatch, datetime(2026, 10, 12, 9, 30))
    assert next_run == datetime(2026, 10, 12, 9, 31)


@pytest.mark.parametrize(
    ("started", "expected"),
    [
        (datetime(2026, 10, 12, 8, 0), datetime(2026, 10, 12, 9, 0)),    # до сводки — как обычно, в 9:00
        (datetime(2026, 10, 12, 15, 0), datetime(2026, 10, 12, 15, 1)),  # опоздали ровно на 6 ч — ещё догоняем
        (datetime(2026, 10, 12, 15, 30), datetime(2026, 10, 19, 9, 0)),  # больше 6 ч — следующая неделя
        (datetime(2026, 10, 14, 11, 0), datetime(2026, 10, 19, 9, 0)),   # среда — обычное расписание
        (datetime(2026, 10, 11, 23, 0), datetime(2026, 10, 12, 9, 0)),   # воскресенье вечером
    ],
)
async def test_weekly_digest_catch_up_window(
    app: BotHarness, monkeypatch: pytest.MonkeyPatch, started: datetime, expected: datetime
) -> None:
    """Пропущенную сводку догоняем, только если бот включили не позже чем через 6 ч после её
    времени; иначе она уйдёт по расписанию — в следующий понедельник в 9:00."""
    assert next_digest_run(app, monkeypatch, started) == expected


async def test_caught_up_digest_is_not_sent_twice(app: BotHarness, travel: Any) -> None:
    """Сводку за неделю 05.10–11.10 догнали после запуска в 9:31. В 10:00 бот перезапустили, и
    планировщик снова попробовал её отправить, — руководители не получают её второй раз. Через
    неделю приходит уже новая сводка. Учёт ведётся в БД (DigestLog), поэтому переживает перезапуск."""
    h = app
    clock = travel
    await seed_week(h, clock)
    now = local(10, 12, 9, 31)
    clock.set(now)
    log = await h.capture(weekly_digest(h.bot, h.sessionmaker, now=now, once=True))
    assert sent(h, log, MGR)[0].text.startswith("🗓 Еженедельная сводка")
    assert "Неделя 05.10–11.10.2026" in sent(h, log, MGR2)[0].text

    clock.set(local(10, 12, 10))
    log = await h.capture(weekly_digest(h.bot, h.sessionmaker, now=local(10, 12, 10), once=True))
    assert not log.calls

    clock.set(local(10, 19, 9))
    log = await h.capture(weekly_digest(h.bot, h.sessionmaker, now=local(10, 19, 9), once=True))
    assert "Неделя 12.10–18.10.2026" in sent(h, log, MGR)[0].text
    assert len(await h.scalars(select(DigestLog))) == 2


async def test_digest_not_delivered_to_anyone_is_retried(app: BotHarness, travel: Any) -> None:
    """Оба руководителя заблокировали бота — сводка никому не дошла и не считается отправленной:
    когда планировщик попробует снова (например, после перезапуска), она уйдёт."""
    h = app
    clock = travel
    await seed_week(h, clock)
    now = local(10, 12, 9)
    clock.set(now)
    h.api.blocked_chats.update({MGR, MGR2})
    log = await h.capture(weekly_digest(h.bot, h.sessionmaker, now=now, once=True))
    assert not sent(h, log, MGR) and not sent(h, log, MGR2)
    assert await h.scalars(select(DigestLog)) == []

    h.api.blocked_chats.clear()
    log = await h.capture(weekly_digest(h.bot, h.sessionmaker, now=local(10, 12, 9, 31), once=True))
    assert log.chats == {MGR, MGR2}


# =====================================================================================================
# 7. Уведомления bot.notify
# =====================================================================================================


async def test_notify_new_task_card_and_accept_button(office: Office) -> None:
    """Сотруднику приходит «🆕 Вам поставлена новая задача» с карточкой (без строки «Исполнитель»)
    и кнопками «✅ Принял в работу» / «📋 Открыть»; нажатие «Принял» фиксирует получение."""
    h = office.h
    task = await office.task()
    log = await h.capture(notify.notify_new_task(h.bot, task))
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith("🆕 Вам поставлена новая задача")
    assert f"Задача #{task.id}: Анализ договоров" in msg.text
    assert "Проверить 100 договоров и представить отчёт" in msg.text
    assert "План: 100 договоров" in msg.text
    assert "Исполнитель:" not in msg.text
    assert "Нажмите «✅ Принял в работу»" in msg.text
    assert msg.button_texts == ["✅ Принял в работу", "📋 Открыть"]
    assert log.chats == {EMP}

    log = await h.press_button(EMP, "Принял в работу", msg.message_id)
    assert log.alert and "Принято в работу" in log.alert
    assert (await office.task()).accepted_at is not None


async def test_notify_new_task_skips_deactivated_assignee(office: Office) -> None:
    """Исполнителя отключили в боте — уведомление о задаче ему не отправляется."""
    h = office.h
    await set_status(h, EMP, UserStatus.BLOCKED)
    log = await h.capture(notify.notify_new_task(h.bot, await office.task()))
    assert not log.calls


async def test_notify_new_task_escapes_html(app: BotHarness, travel: Any) -> None:
    """Название с «<b>» и «&» доходит как текст (не ломает разметку и не теряется)."""
    h = app
    clock = travel
    clock.set(created_at())
    await seed_office(h)
    task_id = await make_task(h, title="Акт <b>сверки</b> & отчёт", expected="Сдать 5 актов <срочно>")
    log = await h.capture(notify.notify_new_task(h.bot, await h.get_task(task_id)))
    [msg] = sent(h, log, EMP)
    assert "Акт <b>сверки</b> & отчёт" in msg.text
    assert "Сдать 5 актов <срочно>" in msg.text


async def test_very_long_texts_do_not_break_messages(app: BotHarness, travel: Any) -> None:
    """Название на 255 символов и ожидаемый результат на 6000 символов с «<», «&»: новая задача,
    напоминание, уведомление о просрочке, об изменении и о сдаче доходят без ошибок Telegram
    (текст обрезан до лимита, разметка цела)."""
    h = app
    clock = travel
    await seed_office(h)
    title = ("Сверка <актов> & договоров " * 12)[:255]
    expected = "Проверить договор № <N> & составить акт. " * 150
    task_id = await make_task(h, title=title, expected=expected)
    office = Office(h, clock, task_id)

    logs = [await h.capture(notify.notify_new_task(h.bot, await office.task()))]
    logs.append(await office.tick(local(10, 4, 18)))
    logs.append(await office.tick(local(10, 7, 19)))
    clock.set(local(10, 7, 20))
    _, changes = await as_manager(
        h, lambda s, mgr: tasks_svc.update_task(s, task_id, mgr, expected_result=expected[::-1])
    )
    logs.append(await h.capture(notify.notify_task_changed(h.bot, await office.task(), changes)))
    await submit(h, task_id, attachments=[])
    logs.append(await notify_submission(h, task_id))

    for log in logs:
        assert log.calls and not log.errors, log.texts
    assert not h.api.bug_errors
    for chat in (EMP, MGR):
        assert all(len(msg.content) <= 4096 for msg in h.messages(chat))
    assert len(h.sent_to(EMP)) == 4 and len(h.sent_to(MGR)) == 2


async def test_notify_proposal_to_all_active_managers(app: BotHarness, travel: Any) -> None:
    """Сотрудник внёс устное поручение: всем активным руководителям — «📥 Сотрудник внёс поручение»
    с кнопками «✅ Подтвердить» / «✏️ Изменить» / «❌ Отклонить»; отключённому руководителю — нет;
    руководитель, заблокировавший бота, не мешает остальным."""
    h = app
    clock = travel
    clock.set(created_at())
    await seed_office(h)
    await h.seed_user(MGR2, "Смирнов Олег Петрович", role="manager")
    await h.seed_user(1003, "Ким Ольга Викторовна", role="manager", status="blocked")
    await h.seed_user(1004, "Пак Денис Ильич", role="manager")
    h.api.blocked_chats.add(1004)
    async with h.db() as s:
        emp = await users_svc.get_by_tg(s, EMP)
        task = await tasks_svc.propose_task(
            s, employee=emp, title="Справка для аудита", expected_result="Подготовить справку", deadline=deadline()
        )
        await s.commit()
        log = await h.capture(notify.notify_proposal(h.bot, s, task))
    assert log.chats == {MGR, MGR2}
    assert not log.to(1003).calls
    for manager in (MGR, MGR2):
        [msg] = sent(h, log, manager)
        assert msg.text.startswith("📥 Сотрудник внёс поручение — нужно ваше решение")
        assert "Справка для аудита" in msg.text
        assert "Иванов И. И." in msg.text
        assert "✋ Внесена сотрудником" in msg.text
        assert msg.button_texts == ["✅ Подтвердить", "✏️ Изменить", "❌ Отклонить"]


async def test_notify_proposal_decision_approved(app: BotHarness, travel: Any) -> None:
    """Руководитель подтвердил поручение: сотруднику — «✅ Руководитель подтвердил ваше поручение»,
    карточка и кнопка «📤 Сдать результат», которая сразу запускает сдачу."""
    h = app
    clock = travel
    clock.set(created_at())
    await seed_office(h)
    async with h.db() as s:
        emp = await users_svc.get_by_tg(s, EMP)
        task = await tasks_svc.propose_task(
            s, employee=emp, title="Справка для аудита", expected_result="Подготовить справку", deadline=deadline()
        )
        await s.commit()
    await as_manager(h, lambda s, mgr: tasks_svc.approve_proposal(s, task.id, mgr, weight=15))
    log = await h.capture(notify.notify_proposal_decision(h.bot, await h.get_task(task.id), approved=True))
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith("✅ Руководитель подтвердил ваше поручение")
    assert "Справка для аудита" in msg.text and "15 %" in msg.text
    assert msg.button_texts == SUBMIT_BUTTONS
    await h.press_button(EMP, "Сдать результат", msg.message_id)
    assert "Что фактически сделано?" in h.last_text(EMP)


async def test_notify_proposal_decision_rejected_with_reason(app: BotHarness, travel: Any) -> None:
    """Поручение отклонено: «❌ Руководитель отклонил ваше поручение», причина показана как есть
    (HTML в причине не ломает сообщение), кнопок нет."""
    h = app
    clock = travel
    clock.set(created_at())
    await seed_office(h)
    async with h.db() as s:
        emp = await users_svc.get_by_tg(s, EMP)
        task = await tasks_svc.propose_task(
            s, employee=emp, title="Справка", expected_result="Подготовить справку", deadline=deadline()
        )
        await s.commit()
    reason = "Это <b>не</b> наша зона & уже сделано"
    await as_manager(h, lambda s, mgr: tasks_svc.reject_proposal(s, task.id, mgr, reason))
    log = await h.capture(
        notify.notify_proposal_decision(h.bot, await h.get_task(task.id), approved=False, reason=reason)
    )
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith("❌ Руководитель отклонил ваше поручение")
    assert f"#{task.id}" in msg.text and "«Справка»" in msg.text
    assert f"💬 Причина: {reason}" in msg.text
    assert msg.buttons == []


async def test_notify_task_changed_lists_old_and_new_values(office: Office) -> None:
    """Руководитель изменил срок, вес, приоритет и название: сотрудник видит «было → стало»
    по каждому полю и кнопки «📤 Сдать результат» / «📋 Открыть». Без изменений — ничего не шлём."""
    h, clock = office.h, office.clock
    clock.set(local(10, 3, 12))
    _, changes = await as_manager(
        h,
        lambda s, mgr: tasks_svc.update_task(
            s,
            office.task_id,
            mgr,
            deadline=local(10, 9, 18),
            weight=30,
            priority=Priority.HIGH,
            title="Анализ договоров поставки",
        ),
    )
    log = await h.capture(notify.notify_task_changed(h.bot, await office.task(), changes))
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith("✏️ Руководитель изменил задачу")
    assert f"#{office.task_id} «Анализ договоров поставки»" in msg.text
    assert "• Срок: 7 октября (ср), 18:00 → 9 октября (пт), 18:00" in msg.text
    assert "• Вес: 20 % → 30 %" in msg.text
    assert "• Приоритет: 🟡 Средний → 🔴 Высокий" in msg.text
    assert "• Название: «Анализ договоров» → «Анализ договоров поставки»" in msg.text
    assert msg.button_texts == SUBMIT_BUTTONS

    log = await h.capture(notify.notify_task_changed(h.bot, await office.task(), {}))
    assert not log.calls


async def test_notify_task_changed_for_proposal(app: BotHarness, travel: Any) -> None:
    """Руководитель скорректировал ещё не подтверждённое поручение — «скорректировал ваше поручение»,
    кнопка только «📋 Открыть» (сдавать пока нечего)."""
    h = app
    clock = travel
    clock.set(created_at())
    await seed_office(h)
    async with h.db() as s:
        emp = await users_svc.get_by_tg(s, EMP)
        task = await tasks_svc.propose_task(
            s, employee=emp, title="Справка", expected_result="Подготовить справку", deadline=deadline()
        )
        await s.commit()
    _, changes = await as_manager(
        h, lambda s, mgr: tasks_svc.update_task(s, task.id, mgr, expected_result="Справка на 2 страницы")
    )
    log = await h.capture(notify.notify_task_changed(h.bot, await h.get_task(task.id), changes))
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith("✏️ Руководитель скорректировал ваше поручение")
    assert "• Ожидаемый результат: «Подготовить справку» → «Справка на 2 страницы»" in msg.text
    assert msg.button_texts == ["📋 Открыть"]


async def test_notify_task_cancelled(office: Office) -> None:
    """Задачу отменили: «🚫 Задача отменена руководителем», причина, «Сдавать результат по ней
    не нужно», кнопок нет."""
    h = office.h
    await as_manager(h, lambda s, mgr: tasks_svc.cancel_task(s, office.task_id, mgr, "Договоры отозваны"))
    log = await h.capture(notify.notify_task_cancelled(h.bot, await office.task(), "Договоры отозваны"))
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith("🚫 Задача отменена руководителем")
    assert "«Анализ договоров»" in msg.text
    assert "💬 Причина: Договоры отозваны" in msg.text
    assert "Сдавать результат по ней не нужно." in msg.text
    assert msg.buttons == []


async def submission_with_files(h: BotHarness, office: Office, photos: int, docs: list[str], videos: int) -> int:
    """Сдача с файлами, которые сотрудник «прислал в Telegram» (file_id настоящего типа)."""
    attachments = []
    for _ in range(photos):
        info = h.api.register_file("photo", b"jpg", mime_type="image/jpeg")
        attachments.append(tasks_svc.AttachmentIn(AttachmentKind.PHOTO, info.file_id, info.file_unique_id))
    for name in docs:
        info = h.api.register_file("document", b"doc", file_name=name)
        attachments.append(
            tasks_svc.AttachmentIn(AttachmentKind.DOCUMENT, info.file_id, info.file_unique_id, file_name=name)
        )
    for index in range(videos):
        info = h.api.register_file("video", b"mp4", file_name=f"obzor{index + 1}.mp4", mime_type="video/mp4")
        attachments.append(
            tasks_svc.AttachmentIn(AttachmentKind.VIDEO, info.file_id, info.file_unique_id, file_name=info.file_name)
        )
    office.clock.set(local(10, 6, 12))
    return await submit(h, office.task_id, attachments=attachments)


async def notify_submission(h: BotHarness, task_id: int) -> RequestLog:
    async with h.db() as s:
        task = await tasks_svc.get_task(s, task_id)
        return await h.capture(notify.notify_submission(h.bot, s, task, task.last_submission))


async def test_notify_submission_photos_as_album_documents_one_by_one(office: Office) -> None:
    """Сотрудник сдал результат с 3 фото, 2 документами и видео. Руководитель получает
    «📝 Результат по задаче» (план ↔ факт, оценка, кнопки проверки и «📎 Файлы (6)»), затем фото
    одним альбомом с подписью, документы — каждый отдельным сообщением с именем файла, видео — видео."""
    h = office.h
    await submission_with_files(h, office, photos=3, docs=["Анализ.xlsx", "Акт.pdf"], videos=1)
    log = await notify_submission(h, office.task_id)
    assert log.chats == {MGR}
    assert methods_to(log, MGR) == ["SendMessage", "SendMediaGroup", "SendDocument", "SendDocument", "SendVideo"]

    [card, *_] = sent(h, log, MGR)
    assert card.text.startswith(f"📝 Результат по задаче #{office.task_id}")
    assert "🎯 План: Проверить 100 договоров и представить отчёт" in card.text
    assert "✅ Факт: Проверено 110 договоров" in card.text
    assert "🔢 План: 100 договоров → Факт: 110 договоров (110 %)" in card.text
    assert "в срок" in card.text
    assert "📎 Файлы (6)" in card.text
    assert "📐 Расчёт по правилам (AI недоступен): 110 %" in card.text
    assert card.button_texts == ["✅ Подтвердить 110 %", "✏️ Изменить оценку", "↩ На доработку", "📎 Файлы (6)"]

    album = log.of(m.SendMediaGroup)[0]
    assert len(album.media) == 3 and all(isinstance(item, InputMediaPhoto) for item in album.media)
    assert [item.caption for item in album.media] == [
        f"📷 Фото · задача #{office.task_id}, попытка 1",
        None,
        None,
    ]
    docs = log.documents
    assert [doc.file_name for doc in docs] == ["Анализ.xlsx", "Акт.pdf"]
    assert docs[0].caption == f"📎 Анализ.xlsx\nзадача #{office.task_id}, попытка 1"
    [video] = [file for file in log.files if file.kind == "video"]
    assert video.file_name == "obzor1.mp4"


@pytest.mark.parametrize(
    ("photos", "expected"),
    [
        (1, ["SendMessage", "SendPhoto"]),
        (2, ["SendMessage", "SendMediaGroup"]),
        (10, ["SendMessage", "SendMediaGroup"]),
        (11, ["SendMessage", "SendMediaGroup", "SendPhoto"]),
        (12, ["SendMessage", "SendMediaGroup", "SendMediaGroup"]),
    ],
)
async def test_notify_submission_photo_albums_respect_telegram_limits(
    office: Office, photos: int, expected: list[str]
) -> None:
    """Одно фото — обычным фото (альбом из одного Telegram не примет), до 10 — одним альбомом,
    больше 10 — несколькими альбомами (в каждом 2–10 фото)."""
    h = office.h
    await submission_with_files(h, office, photos=photos, docs=[], videos=0)
    log = await notify_submission(h, office.task_id)
    assert methods_to(log, MGR) == expected
    assert not log.errors
    assert len([file for file in log.files if file.kind == "photo"]) == photos


async def test_notify_submission_goes_to_responsible_manager(app: BotHarness, travel: Any) -> None:
    """Руководителей двое: результат проверяет тот, кто ставил задачу. Если его отключили в боте —
    результат получают все активные руководители."""
    h = app
    clock = travel
    clock.set(created_at())
    await seed_office(h)
    await h.seed_user(MGR2, "Смирнов Олег Петрович", role="manager")
    task_id = await make_task(h, manager_tg=MGR2)
    clock.set(local(10, 6, 12))
    await submit(h, task_id)
    log = await notify_submission(h, task_id)
    assert log.chats == {MGR2}

    await set_status(h, MGR2, UserStatus.BLOCKED)
    log = await notify_submission(h, task_id)
    assert log.chats == {MGR}


async def test_notify_submission_manager_blocked_bot_no_files_no_crash(office: Office) -> None:
    """Руководитель заблокировал бота: сообщение не доходит, файлы ему даже не пытаются слать,
    исключений нет."""
    h = office.h
    await submission_with_files(h, office, photos=2, docs=["Акт.pdf"], videos=0)
    h.api.blocked_chats.add(MGR)
    log = await notify_submission(h, office.task_id)
    assert methods_to(log, MGR) == ["SendMessage"]
    assert [type(error).__name__ for error in log.errors] == ["TelegramForbiddenError"]


async def test_files_button_in_submission_resends_attachments(office: Office) -> None:
    """Руководитель нажимает «📎 Файлы (3)» в сообщении о сдаче — фото приходят альбомом,
    документ — отдельно."""
    h = office.h
    await submission_with_files(h, office, photos=2, docs=["Акт.pdf"], videos=0)
    await notify_submission(h, office.task_id)
    log = await h.press_button(MGR, "Файлы (3)")
    assert log.answers
    assert [name for name in methods_to(log, MGR) if name.startswith("Send")] == ["SendMediaGroup", "SendDocument"]


async def test_notify_review_result_confirmed_and_changed(office: Office) -> None:
    """Сотруднику приходит итог проверки: «🏁 Результат по задаче #N оценён», итоговая оценка
    и решение; при изменённой оценке — комментарий руководителя. Кнопка — только «📋 Открыть»."""
    h, clock = office.h, office.clock
    clock.set(local(10, 6, 12))
    sub_id = await submit(h, office.task_id)
    await as_manager(h, lambda s, mgr: tasks_svc.review_set_score(s, sub_id, mgr, 95, "Отчёт без рекомендаций"))
    task = await office.task()
    log = await h.capture(notify.notify_review_result(h.bot, task, task.last_submission))
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith(f"🏁 Результат по задаче #{office.task_id} оценён")
    assert "Итоговая оценка: 95 %" in msg.text
    assert "✏️ Оценку выставил руководитель." in msg.text
    assert "💬 Комментарий руководителя: Отчёт без рекомендаций" in msg.text
    assert msg.button_texts == ["📋 Открыть"]

    other = await make_task(h, title="Отчёт по претензиям", due=local(10, 9, 18))
    other_sub = await submit(h, other)
    await as_manager(h, lambda s, mgr: tasks_svc.review_confirm(s, other_sub, mgr))
    task = await h.get_task(other)
    log = await h.capture(notify.notify_review_result(h.bot, task, task.last_submission))
    [msg] = sent(h, log, EMP)
    assert "Итоговая оценка: 110 %" in msg.text
    assert "✅ Руководитель подтвердил предварительную оценку." in msg.text


async def test_notify_rework_submit_button_shows_manager_comment(office: Office) -> None:
    """Результат вернули на доработку: сотрудник видит «↩️ Задача #N возвращена на доработку»,
    комментарий, новый срок и кнопку «📤 Сдать результат», которая начинает повторную сдачу
    с комментарием руководителя перед глазами."""
    h, clock = office.h, office.clock
    clock.set(local(10, 6, 12))
    sub_id = await submit(h, office.task_id)
    await as_manager(
        h,
        lambda s, mgr: tasks_svc.review_rework(
            s, sub_id, mgr, "Добавьте рекомендации по <нарушениям>", new_deadline=local(10, 9, 18)
        ),
    )
    task = await office.task()
    log = await h.capture(notify.notify_rework(h.bot, task, task.last_submission))
    [msg] = sent(h, log, EMP)
    assert msg.text.startswith(f"↩️ Задача #{office.task_id} возвращена на доработку")
    assert "💬 Комментарий руководителя: Добавьте рекомендации по <нарушениям>" in msg.text
    assert "📅 Срок: 9 октября (пт), 18:00" in msg.text
    assert msg.button_texts == SUBMIT_BUTTONS

    await h.press_button(EMP, "Сдать результат", msg.message_id)
    intro = h.last_text(EMP)
    assert "↩️ Задача возвращена на доработку." in intro
    assert "Добавьте рекомендации по <нарушениям>" in intro
    assert "Попытка сдачи №2" in intro


async def test_notify_registration_to_managers(app: BotHarness) -> None:
    """Новый сотрудник отправил заявку: каждому активному руководителю — «👤 Новая заявка на доступ»
    с ФИО, должностью, @username и кнопками «✅ Подтвердить» / «❌ Отклонить»."""
    h = app
    await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    await h.seed_user(MGR2, "Смирнов Олег Петрович", role="manager")
    user = await h.seed_user(3001, "О'Нил <Иван> Петрович", status="pending", username="oneil")
    async with h.db() as s:
        log = await h.capture(notify.notify_registration(h.bot, s, user))
    assert log.chats == {MGR, MGR2}
    [msg] = sent(h, log, MGR)
    assert msg.text.startswith("👤 Новая заявка на доступ к боту")
    assert "ФИО: О'Нил <Иван> Петрович" in msg.text
    assert "Должность: не указана" in msg.text
    assert "Telegram: @oneil" in msg.text
    assert msg.button_texts == ["✅ Подтвердить", "❌ Отклонить"]


async def test_notify_user_decision_approved_and_rejected(app: BotHarness) -> None:
    """Заявку подтвердили — «✅ Доступ к боту открыт!», роль и меню сотрудника; отклонили —
    «❌ Заявка на доступ отклонена», меню убирается."""
    h = app
    await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    approved = await h.seed_user(3001, "Ким Ольга Викторовна")
    log = await h.capture(notify.notify_user_decision(h.bot, approved, approved=True))
    [msg] = sent(h, log, 3001)
    assert msg.text.startswith("✅ Доступ к боту открыт!")
    assert "Ваша роль: сотрудник." in msg.text
    menu = h.reply_keyboard(3001)
    assert menu is not None and BTN_SUBMIT in menu and BTN_MY_TASKS in menu

    rejected = await h.seed_user(3002, "Пак Денис Ильич", status="blocked")
    h.api.reply_keyboards[3002] = h.api.reply_keyboards[3001]  # у него было показано меню
    log = await h.capture(notify.notify_user_decision(h.bot, rejected, approved=False))
    [msg] = sent(h, log, 3002)
    assert msg.text.startswith("❌ Заявка на доступ отклонена руководителем.")
    assert h.reply_keyboard(3002) is None


async def test_safe_send_waits_once_on_flood_limit(office: Office, monkeypatch: pytest.MonkeyPatch) -> None:
    """Telegram ответил «слишком часто, подождите 3 с»: бот ждёт и повторяет один раз — сообщение
    доходит. Если просят ждать дольше минуты — не ждём, возвращаем None (повторит планировщик)."""
    h = office.h
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(notify, "asyncio", SimpleNamespace(sleep=fake_sleep))
    original = h.api._handle
    flood = {"retry_after": 3, "times": 1}

    async def limited(bot: Any, method: Any, call: Any) -> Any:
        if isinstance(method, m.SendMessage) and flood["times"] > 0:
            flood["times"] -= 1
            raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=flood["retry_after"])
        return await original(bot, method, call)

    monkeypatch.setattr(h.api, "_handle", limited)
    message = await notify.safe_send(h.bot, EMP, "Проверка")
    assert message is not None and waits == [3]
    assert h.last_text(EMP) == "Проверка"

    flood.update(retry_after=600, times=1)
    assert await notify.safe_send(h.bot, EMP, "Вторая") is None
    assert waits == [3]


async def test_safe_send_swallows_telegram_errors(office: Office) -> None:
    """Пользователь заблокировал бота или Telegram отклонил сообщение — safe_send возвращает None,
    исключение не вылетает (действие пользователя уже сохранено)."""
    h = office.h
    h.api.blocked_chats.add(EMP)
    assert await notify.safe_send(h.bot, EMP, "Привет") is None
    with h.relaxed():
        assert await notify.safe_send(h.bot, MGR, "<b>битый HTML") is None


