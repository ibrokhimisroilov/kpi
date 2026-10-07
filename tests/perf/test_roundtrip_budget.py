"""Сколько обменов с базой данных делает каждый сценарий бота — и бюджеты, которые нельзя превышать.

Каждый тест готовит данные (без замера), затем замеряет действие пользователя через настоящий
Dispatcher бота (``bot.main.build_dispatcher``) и фейковый Telegram (tests/e2e/fakebot.py):
SQL-выражения, транзакции, выдачи соединений, новые соединения, проверки соединений, обмены по модели
PostgreSQL+asyncpg — отдельно те, что ждёт пользователь, и фоновую запись хранилища диалогов — и время
с задержкой 150 мс на обмен (см. tests/perf/roundtrips.py). Таблица печатается в конце запуска.

Бюджеты (``BUDGETS``): каждый замер (сценарий и каждый его шаг) сверяется с числом обменов, которое
он делал после ускорения (07.10.2026), плюс небольшой запас (``_slack``). Тест падает, если изменение
кода добавило апдейту обменов с базой: лишний запрос, pre-ping, транзакцию, отдельную сессию… Каждый
обмен — это 130–190 мс, пока база в другом регионе. Если обменов стало МЕНЬШЕ — уменьшите бюджет
(числа печатаются в таблице), если больше и это оправдано — увеличьте осознанно, с объяснением.
Счётчики одинаковы на SQLite и PostgreSQL (TEST_DATABASE_URL) и не зависят от задержки.

Запуск с настоящей задержкой 150 мс на обмен (как Render -> Supabase в другом регионе):

    PYTHONUTF8=1 ./.venv/Scripts/python -m pytest tests/perf -m perf -q -p no:cacheprovider

``PERF_SQL=1`` — журнал SQL каждого замера (с ``-s``), ``PERF_REPORT=perf.json`` — замеры в JSON,
``PERF_LATENCY_MS=…`` — своя задержка. С TEST_DATABASE_URL — то же на PostgreSQL.

Сценарии: S1 /start руководителя; S2 регистрация сотрудника; S3 «📋 Задачи» (10 задач);
S4 карточка задачи; S5 мастер «➕ Поставить задачу» по шагам (AI выключен); S6 «✅ Сдать результат»
по шагам (AI выключен); S7 подтверждение оценки (SubCB ok); S8 «📊 Команда» (5 сотрудников × 10 задач);
S9 обычный текст без диалога (+ S9c — то же после пересоздания соединений пулов, S9i — после простоя
дольше 10 минут: проверка соединений и перечитывание диалога, S9r — первый апдейт после перезапуска
бота); S10 один запуск run_due_jobs с 20 открытыми задачами (+ S10b — следующий запуск, когда
отправлять нечего).
"""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.db.base import make_engine, make_storage_engine, warm_up
from bot.db.models import (
    Attachment,
    AttachmentKind,
    Priority,
    ReviewDecision,
    Submission,
    Task,
    TaskSource,
    TaskStatus,
    User,
)
from bot.scheduler.jobs import run_due_jobs
from bot.ui.callbacks import TaskCB
from bot.ui.texts import BTN_NEW_TASK, BTN_SUBMIT, BTN_TASKS, BTN_TEAM
from bot.utils.dates import utcnow
from e2e.fakebot import MANAGER_TG_ID, BotHarness

from .roundtrips import LOCAL_CONNECT_ROUND_TRIPS, Counts, Measurement, RoundTripProbe, Scenario, WireCounter

if TYPE_CHECKING:
    from .conftest import PerfApp

pytestmark = [pytest.mark.asyncio, pytest.mark.perf]

MGR = MANAGER_TG_ID
EMPLOYEES = [
    (2001, "Иванов Иван Иванович"),
    (2002, "Сидоров Пётр Ильич"),
    (2003, "Кузнецова Анна Сергеевна"),
    (2004, "Смирнов Олег Павлович"),
    (2005, "Попова Мария Игоревна"),
]
EMP = EMPLOYEES[0][0]
# Пятница 02.10.2026, 12:00 по Ташкенту: не тихие часы, не время еженедельной сводки и резервной копии.
TICK_NOW = datetime(2026, 10, 2, 7, 0)
# Состав задач сотрудника на дашборде команды (S8).
DASHBOARD_MIX = ["done", "done", "done", "done", "active", "active", "active", "overdue", "submitted", "rework"]


# --- Бюджеты ----------------------------------------------------------------------------------------

# Замер -> (обменов, которые ждёт пользователь; обменов фоновой записи хранилища диалогов) — столько
# было после ускорения 07.10.2026 (до него, для сравнения, в комментарии). Шаг сценария — «сценарий / шаг».
BUDGETS: dict[str, tuple[int, int]] = {
    "S1 /start руководителя": (3, 0),  # было 33
    "S2 регистрация: /start + ФИО + должность": (14, 11),  # было 133
    "S2 регистрация: /start + ФИО + должность / /start нового": (6, 4),  # было 48
    "S2 регистрация: /start + ФИО + должность / ФИО": (1, 3),  # было 38
    "S2 регистрация: /start + ФИО + должность / должность": (7, 4),  # было 47
    "S3 «📋 Задачи», 10 задач": (9, 0),  # было 50
    "S4 карточка задачи": (7, 0),  # было 40
    "S5 «➕ Поставить задачу» (без AI)": (21, 29),  # было 508
    "S5 «➕ Поставить задачу» (без AI) / меню «Поставить задачу»": (2, 4),  # было 59
    "S5 «➕ Поставить задачу» (без AI) / сотрудник": (3, 3),  # было 52
    "S5 «➕ Поставить задачу» (без AI) / название": (1, 3),  # было 50
    "S5 «➕ Поставить задачу» (без AI) / результат": (1, 3),  # было 76
    "S5 «➕ Поставить задачу» (без AI) / «Принять»": (1, 3),  # было 62
    "S5 «➕ Поставить задачу» (без AI) / срок «Завтра»": (1, 3),  # было 50
    "S5 «➕ Поставить задачу» (без AI) / приоритет": (3, 3),  # было 52
    "S5 «➕ Поставить задачу» (без AI) / вес": (1, 3),  # было 57
    "S5 «➕ Поставить задачу» (без AI) / «Создать»": (8, 4),  # было 50
    "S6 «✅ Сдать результат» (без AI)": (38, 20),  # было 389
    "S6 «✅ Сдать результат» (без AI) / меню «Сдать результат»": (5, 0),  # было 44
    "S6 «✅ Сдать результат» (без AI) / выбор задачи": (4, 4),  # было 71
    "S6 «✅ Сдать результат» (без AI) / что сделано": (1, 3),  # было 44
    "S6 «✅ Сдать результат» (без AI) / какой результат": (1, 3),  # было 50
    "S6 «✅ Сдать результат» (без AI) / факт-число": (1, 3),  # было 44
    "S6 «✅ Сдать результат» (без AI) / «Без файлов»": (3, 3),  # было 48
    "S6 «✅ Сдать результат» (без AI) / «Отправить»": (23, 4),  # было 88
    "S7 «✅ Подтвердить» оценку (SubCB ok)": (14, 0),  # было 50
    "S8 «📊 Команда», 5 × 10 задач": (5, 0),  # было 44
    "S9 текст без диалога": (1, 0),  # было 18
    "S9c текст после pool_recycle": (9, 0),  # было 29
    "S9i текст после простоя >10 мин": (4, 0),  # новый замер
    "S9r первый апдейт после перезапуска": (4, 0),  # новый замер
    "S10 run_due_jobs: 20 напоминаний": (131, 0),  # было 268
    "S10b run_due_jobs: отправлять нечего": (4, 0),  # было 18
}


def _slack(value: int) -> int:
    """Запас к бюджету: 10 %, но не меньше одного обмена."""
    return max(1, round(value * 0.1))


def check_budget(measurement: Measurement) -> None:
    """Сценарий и каждый его шаг укладываются в BUDGETS (+ запас)."""
    problems: list[str] = []
    for item, name in [(measurement, measurement.name)] + [
        (step, f"{measurement.name} / {step.name}") for step in measurement.steps
    ]:
        budget = BUDGETS.get(name)
        assert budget is not None, f"нет бюджета для замера {name!r}: добавьте его в BUDGETS"
        waited, background = budget
        if item.round_trips > waited + _slack(waited):
            problems.append(f"{name}: пользователь ждёт {item.round_trips} обменов, бюджет {waited} (+{_slack(waited)})")
        if item.background_round_trips > background + _slack(background):
            problems.append(
                f"{name}: фоновых обменов {item.background_round_trips}, бюджет {background} (+{_slack(background)})"
            )
    assert not problems, "Обменов с базой стало больше, чем в бюджете:\n" + "\n".join(problems)


# --- Подготовка данных (без замера) -----------------------------------------------------------------


async def seed_team(h: BotHarness, employees: int, *, start: bool = True) -> tuple[User, list[User]]:
    """Активный руководитель MGR и ``employees`` активных сотрудников; все открыли бота (/start)."""
    mgr = await h.seed_user(MGR, "Петрова Анна Сергеевна", role="manager")
    staff = [await h.seed_user(tg, name, position="Юрист") for tg, name in EMPLOYEES[:employees]]
    if start:
        for user in (mgr, *staff):
            await h.send_command(user.tg_id, "start")
    return mgr, staff


def _task(mgr: User, emp: User, kind: str, n: int, now: datetime, deadline: datetime | None) -> Task:
    """Задача вида kind: active | overdue | rework | submitted | done (сдачи — с файлом и проверкой)."""
    task = Task(
        title=f"Задача {n + 1} ({emp.full_name.split()[0]})",
        expected_result="Проверить 10 договоров и представить отчёт",
        plan_value=10.0,
        plan_unit="договоров",
        deadline=deadline + timedelta(minutes=n) if deadline else now + timedelta(days=2 + n % 4, hours=n),
        priority=Priority.MEDIUM,
        weight=10,
        status=TaskStatus.ACTIVE,
        source=TaskSource.MANAGER,
        assignee_id=emp.id,
        created_by_id=mgr.id,
        manager_id=mgr.id,
        accepted_at=now - timedelta(days=1),
    )
    if kind == "overdue":
        task.deadline = now - timedelta(days=1, hours=n)
    elif kind in ("rework", "submitted", "done"):
        if kind == "done":
            task.deadline = now - timedelta(hours=1 + n)
        sub = Submission(
            attempt=1,
            fact_text="Проверено 10 договоров",
            fact_value=10.0,
            created_at=now - timedelta(hours=5),
            deadline_at_submit=task.deadline,
            ai_score=100.0,
            ai_rationale="План выполнен.",
            ai_source="rules",
            attachments=[
                Attachment(kind=AttachmentKind.DOCUMENT, file_id=f"file-{emp.id}-{n}", file_name="report.pdf")
            ],
        )
        task.submissions = [sub]
        task.submitted_at = sub.created_at
        task.ai_score = 100.0
        if kind == "rework":
            task.status = TaskStatus.REWORK
            task.rework_count = 1
            sub.decision = ReviewDecision.REWORK
            sub.review_comment = "Добавьте выводы"
        elif kind == "submitted":
            task.status = TaskStatus.SUBMITTED
        else:
            task.status = TaskStatus.DONE
            task.final_score = 100.0
            task.completed_at = now - timedelta(hours=1)
            sub.decision = ReviewDecision.APPROVED
            sub.final_score = 100.0
        if sub.decision is not None:
            sub.reviewer_id = mgr.id
            sub.reviewed_at = now - timedelta(hours=2)
    return task


async def add_tasks(
    h: BotHarness,
    mgr: User,
    emp: User,
    kinds: list[str],
    *,
    now: datetime | None = None,
    deadline: datetime | None = None,
) -> list[int]:
    """Задачи сотрудника прямо в БД (как поставленные раньше); deadline — общий срок для всех."""
    now = now or utcnow()
    async with h.db() as session:
        tasks = [_task(mgr, emp, kind, n, now, deadline) for n, kind in enumerate(kinds)]
        session.add_all(tasks)
        await session.commit()
        return [task.id for task in tasks]


async def submit_flow(h: BotHarness, title: str, sc: Scenario | None = None) -> None:
    """«✅ Сдать результат» до «📤 Отправить» включительно (без файлов); sc — замер по шагам."""

    async def run(name: str, awaitable: Awaitable[object]) -> None:
        if sc is None:
            await awaitable
        else:
            await sc.step(name, awaitable)

    await run("меню «Сдать результат»", h.press_menu(EMP, BTN_SUBMIT))
    await run("выбор задачи", h.press_button(EMP, title))
    await run("что сделано", h.send_text(EMP, "Проверено 110 договоров, в 12 выявлены нарушения"))
    await run("какой результат", h.send_text(EMP, "Подготовлены рекомендации по нарушениям"))
    if "Фактическое значение" in (h.last_text(EMP) or ""):
        await run("факт-число", h.send_text(EMP, "110"))
    await run("«Без файлов»", h.press_button(EMP, "Без файлов"))
    assert "Проверьте перед отправкой" in (h.last_text(EMP) or "")
    await run("«Отправить»", h.press_button(EMP, "Отправить"))


async def task_for_submit(h: BotHarness, mgr: User, emp: User) -> int:
    """Задача из ТЗ: план 100 договоров, срок через 3 дня."""
    async with h.db() as session:
        task = Task(
            title="Анализ договоров",
            expected_result="Проверить 100 договоров и представить отчёт",
            plan_value=100.0,
            plan_unit="договоров",
            deadline=utcnow() + timedelta(days=3),
            priority=Priority.MEDIUM,
            weight=20,
            status=TaskStatus.ACTIVE,
            source=TaskSource.MANAGER,
            assignee_id=emp.id,
            created_by_id=mgr.id,
            manager_id=mgr.id,
        )
        session.add(task)
        await session.commit()
        return task.id


# --- Самопроверка счётчика ------------------------------------------------------------------------


async def test_probe_counts_like_asyncpg(perf: PerfApp) -> None:
    """Модель обменов движка бота (отложенный BEGIN, проверка после простоя): чтение — без BEGIN и
    COMMIT; запись — BEGIN + выражения + COMMIT/ROLLBACK; сессия без запросов — ничего; первое выполнение
    текста — Parse; после простоя — одна проверка SELECT 1; после пересоздания — новое соединение без проверки;
    задержка реально добавляется; запись хранилища диалогов — в фоне, отдельно."""
    probe = perf.probe
    touch = update(User).where(User.id == -1).values(position="x")  # запись, которая ничего не меняет
    async with perf.sessionmaker() as session:  # прогрев: соединение открыто, выражения подготовлены
        await session.scalar(select(func.count(User.id)))
        await session.execute(touch)
        await session.commit()
    async with perf.fsm_engine.connect() as conn:  # и пул хранилища диалогов: соединение уже открыто
        await conn.scalar(select(func.count(User.id)))

    async with probe.measure("read") as m:
        async with perf.sessionmaker() as session:
            await session.scalar(select(func.count(User.id)))
            await session.commit()  # только чтения: транзакции не было — COMMIT не уходит
    assert m.engine("main") == Counts(statements=1, checkouts=1)
    assert m.round_trips == 1

    async with probe.measure("write") as m:
        async with perf.sessionmaker() as session:
            await session.scalar(select(func.count(User.id)))  # до первой записи — без транзакции
            await session.execute(touch)
            await session.commit()
    assert m.engine("main") == Counts(statements=2, begins=1, commits=1, checkouts=1)
    assert m.round_trips == 2 + 1 + 1

    async with probe.measure("write, rollback") as m:
        async with perf.sessionmaker() as session:
            await session.execute(touch)  # без commit — ROLLBACK при закрытии
    assert m.engine("main") == Counts(statements=1, begins=1, rollbacks=1, checkouts=1)

    async with probe.measure("nothing") as m:
        async with perf.sessionmaker() as session:
            await session.commit()  # без запросов — в базу ничего не уходит
    assert m.total == Counts()

    async with probe.measure("new statement") as m:  # первое выполнение текста на соединении — Parse
        async with perf.sessionmaker() as session:
            await session.scalar(select(func.max(User.id)))
    assert m.engine("main") == Counts(statements=1, prepares=1, checkouts=1)

    previous, probe.latency_ms = probe.latency_ms, 20.0
    try:
        async with probe.measure("latency") as m:
            async with perf.sessionmaker() as session:
                await session.execute(touch)
                await session.commit()
    finally:
        probe.latency_ms = previous
    assert m.round_trips == 3
    assert m.wall_ms >= 3 * 20 * 0.95

    await probe.idle(perf.main_engine, "main")
    async with probe.measure("idle") as m:
        for _ in range(2):  # второй раз — то же соединение (LIFO), уже проверенное
            async with perf.sessionmaker() as session:
                await session.scalar(select(func.count(User.id)))
    assert m.engine("main") == Counts(statements=2, checkouts=2, pings=1)
    assert m.round_trips == 2 + 1

    await probe.recycle(perf.main_engine, "main")
    async with probe.measure("reconnect") as m:
        async with perf.sessionmaker() as session:
            await session.scalar(select(func.count(User.id)))
    main = m.engine("main")
    assert (main.connects, main.pings, main.prepares) == (1, 0, 1)  # новое соединение: без проверки, кэш пуст

    key = StorageKey(bot_id=42, chat_id=EMP, user_id=EMP)
    async with probe.measure("fsm: первое обращение + изменение") as m:
        await perf.storage.set_state(key, "Probe:step")
    assert m.engine("fsm") == Counts(statements=1, prepares=1, checkouts=1)  # чтение ключа — без транзакции
    assert m.background["fsm"] == Counts(statements=1, prepares=1, begins=1, commits=1, checkouts=1)
    assert (m.round_trips, m.background_round_trips) == (2, 4)

    async with probe.measure("fsm: из памяти") as m:
        assert await perf.storage.get_state(key) == "Probe:step"
        await perf.storage.set_state(key, None)  # диалог закончился — строка удаляется в фоне
    assert m.total == Counts()
    assert m.background["fsm"] == Counts(statements=1, prepares=1, begins=1, commits=1, checkouts=1)


# --- Сценарии -------------------------------------------------------------------------------------


async def test_s1_start_of_active_manager(perf: PerfApp) -> None:
    h = perf.h
    await seed_team(h, 1)
    async with perf.probe.measure("S1 /start руководителя") as m:
        log = await h.send_command(MGR, "start")
    assert log.to(MGR).texts
    check_budget(perf.record(m))


async def test_s2_registration_of_new_employee(perf: PerfApp) -> None:
    h = perf.h
    await seed_team(h, 0)
    async with perf.probe.scenario("S2 регистрация: /start + ФИО + должность") as sc:
        await sc.step("/start нового", h.send_command(EMP, "start", first_name="Иван"))
        await sc.step("ФИО", h.send_text(EMP, "Иванов Иван Иванович"))
        await sc.step("должность", h.send_text(EMP, "Юрист"))
    assert "Заявка отправлена" in (h.last_text(EMP) or "")
    assert h.has_button(MGR, "Подтвердить")
    check_budget(perf.record(sc.measurement))


async def _ten_open_tasks(h: BotHarness) -> list[int]:
    mgr, staff = await seed_team(h, 2)
    ids: list[int] = []
    for emp in staff:
        ids += await add_tasks(h, mgr, emp, ["active"] * 4 + ["rework"])
    return ids


async def test_s3_manager_task_list(perf: PerfApp) -> None:
    h = perf.h
    await _ten_open_tasks(h)
    async with perf.probe.measure("S3 «📋 Задачи», 10 задач") as m:
        await h.press_menu(MGR, BTN_TASKS)
    assert "(10)" in (h.last_text(MGR) or "")
    check_budget(perf.record(m))


async def test_s4_task_card(perf: PerfApp) -> None:
    """Карточка задачи на доработке: сдача + файл + проверивший (каскад selectin-связей)."""
    h = perf.h
    ids = await _ten_open_tasks(h)
    await h.press_menu(MGR, BTN_TASKS)
    task_id = ids[4]  # rework
    async with perf.probe.measure("S4 карточка задачи") as m:
        await h.press(MGR, TaskCB(action="open", task_id=task_id))
    assert f"Задача #{task_id}" in (h.last_text(MGR) or "")
    check_budget(perf.record(m))


async def test_s5_new_task_wizard(perf: PerfApp) -> None:
    h = perf.h
    await seed_team(h, 2)
    async with perf.probe.scenario("S5 «➕ Поставить задачу» (без AI)") as sc:
        await sc.step("меню «Поставить задачу»", h.press_menu(MGR, BTN_NEW_TASK))
        await sc.step("сотрудник", h.press_button(MGR, "Иванов"))
        await sc.step("название", h.send_text(MGR, "Провести анализ договоров"))
        await sc.step("результат", h.send_text(MGR, "проверить 100 договоров и представить отчёт"))
        await sc.step("«Принять»", h.press_button(MGR, "Принять"))
        if "Плановое число" in (h.last_text(MGR) or ""):
            await sc.step("план: «Пропустить»", h.press_button(MGR, "Пропустить"))
        await sc.step("срок «Завтра»", h.press_button(MGR, "Завтра"))
        await sc.step("приоритет", h.press_button(MGR, "Средний"))
        await sc.step("вес", h.press_button(MGR, "20 %"))
        await sc.step("«Создать»", h.press_button(MGR, "Создать"))
    assert await h.scalar(select(func.count(Task.id))) == 1
    check_budget(perf.record(sc.measurement))


async def test_s6_employee_submits_result(perf: PerfApp) -> None:
    h = perf.h
    mgr, (emp,) = await seed_team(h, 1)
    task_id = await task_for_submit(h, mgr, emp)
    async with perf.probe.scenario("S6 «✅ Сдать результат» (без AI)") as sc:
        await submit_flow(h, "Анализ договоров", sc)
    assert (await h.get_task(task_id)).status == TaskStatus.SUBMITTED
    assert h.has_button(MGR, "Подтвердить")
    check_budget(perf.record(sc.measurement))


async def test_s7_manager_confirms_review(perf: PerfApp) -> None:
    h = perf.h
    mgr, (emp,) = await seed_team(h, 1)
    task_id = await task_for_submit(h, mgr, emp)
    await submit_flow(h, "Анализ договоров")
    async with perf.probe.measure("S7 «✅ Подтвердить» оценку (SubCB ok)") as m:
        await h.press_button(MGR, "Подтвердить")
    assert (await h.get_task(task_id)).status == TaskStatus.DONE
    check_budget(perf.record(m))


async def test_s8_team_dashboard(perf: PerfApp) -> None:
    h = perf.h
    mgr, staff = await seed_team(h, 5)
    for emp in staff:
        await add_tasks(h, mgr, emp, DASHBOARD_MIX)
    async with perf.probe.measure("S8 «📊 Команда», 5 × 10 задач") as m:
        await h.press_menu(MGR, BTN_TEAM)
    assert h.last_text(MGR)
    check_budget(perf.record(m))


async def test_s9_plain_text_without_dialog(perf: PerfApp) -> None:
    h = perf.h
    await seed_team(h, 1)
    async with perf.probe.measure("S9 текст без диалога") as m:
        log = await h.send_text(EMP, "привет")
    assert log.to(EMP).texts
    check_budget(perf.record(m))


async def test_s9c_plain_text_after_pool_recycle(perf: PerfApp) -> None:
    """Как S9, но соединения обоих пулов пересоздаются (pool_recycle — раз в 30 мин)."""
    h = perf.h
    await seed_team(h, 1)
    await perf.recycle_connections()
    async with perf.probe.measure("S9c текст после pool_recycle") as m:
        log = await h.send_text(EMP, "привет")
    assert log.to(EMP).texts
    check_budget(perf.record(m))


async def test_s9i_plain_text_after_idle(perf: PerfApp) -> None:
    """Как S9, но после простоя дольше 10 минут — так приходит большинство апдейтов небольшой команды:
    соединения пулов проверяются (SELECT 1), диалог человека перечитывается из базы (cache_ttl)."""
    h = perf.h
    await seed_team(h, 1)
    await h.send_text(EMP, "привет")
    await perf.idle_connections()
    await perf.forget_dialogs()
    async with perf.probe.measure("S9i текст после простоя >10 мин") as m:
        log = await h.send_text(EMP, "привет")
    assert log.to(EMP).texts
    check_budget(perf.record(m))


async def test_s9r_first_update_after_restart(perf: PerfApp) -> None:
    """Первый апдейт после перезапуска бота (деплой): кэш диалогов пуст, выражения на соединениях не
    подготовлены; соединения пулов открыты при запуске (init_db и warm_up в bot.main.main)."""
    h = perf.h
    await seed_team(h, 1)
    await perf.recycle_connections()
    await warm_up(perf.main_engine, perf.fsm_engine)
    await perf.forget_dialogs()
    async with perf.probe.measure("S9r первый апдейт после перезапуска") as m:
        log = await h.send_text(EMP, "привет")
    assert log.to(EMP).texts
    check_budget(perf.record(m))


async def test_s10_run_due_jobs_with_20_open_tasks(perf: PerfApp) -> None:
    """Запуск заданий по времени (каждые 5 мин): 20 открытых задач, у всех подошёл срок «за 3 дня»;
    затем следующий запуск, когда отправлять уже нечего (так проходит большинство запусков)."""
    h = perf.h
    mgr, staff = await seed_team(h, 4)
    for emp in staff:
        await add_tasks(h, mgr, emp, ["active"] * 5, now=TICK_NOW, deadline=TICK_NOW + timedelta(days=2))

    async with perf.probe.measure("S10 run_due_jobs: 20 напоминаний") as m:
        log = await h.capture(run_due_jobs(h.bot, perf.sessionmaker, now=TICK_NOW))
    assert log.result["reminders"] == 20, log.result
    check_budget(perf.record(m))

    async with perf.probe.measure("S10b run_due_jobs: отправлять нечего") as m:
        log = await h.capture(run_due_jobs(h.bot, perf.sessionmaker, now=TICK_NOW + timedelta(minutes=5)))
    assert log.result["reminders"] == 0, log.result
    check_budget(perf.record(m))


# --- Сверка модели с настоящим трафиком PostgreSQL ------------------------------------------------


async def test_model_matches_postgres_wire(
    perf_env: None, engine: AsyncEngine, storage_engine: AsyncEngine | None
) -> None:
    """Модель обменов (RoundTripProbe) совпадает с настоящим трафиком asyncpg, подсчитанным TCP-прокси
    (WireCounter), — на апдейтах с чтением, записью, хранилищем диалогов (чтение и фоновая запись),
    заданиями, после простоя (проверка соединений) и после переподключения. Сравнивается всё, что видит
    сеть: обмены, которые ждёт пользователь, плюс фоновая запись хранилища."""
    if engine.dialect.name != "postgresql" or storage_engine is None:
        pytest.skip("нужен PostgreSQL с настоящими пулами: задайте TEST_DATABASE_URL")
    from .conftest import open_perf_app

    async with engine.connect() as conn:
        schema = await conn.scalar(text("SELECT current_schema()"))
    wire = await WireCounter(engine.url.host or "127.0.0.1", engine.url.port or 5432).start()
    url = engine.url.set(host="127.0.0.1", port=wire.port)
    settings = {"search_path": str(schema), "lock_timeout": "10s"}
    main_engine = make_engine(url, connect_args={"server_settings": settings})
    fsm_engine = make_storage_engine(url, connect_args={"server_settings": settings})
    assert fsm_engine is not None
    probe = RoundTripProbe(connect_round_trips=LOCAL_CONNECT_ROUND_TRIPS)
    results: list[tuple[str, int, int]] = []  # (замер, обменов по модели, обменов в трафике)
    try:
        async with open_perf_app(main_engine, fsm_engine, backend="postgresql", probe=probe) as app:
            h = app.h
            mgr, staff = await seed_team(h, 4)  # прогрев: соединения, инициализация диалекта
            for emp in staff:
                await add_tasks(
                    h, mgr, emp, ["active", "rework"], now=TICK_NOW, deadline=TICK_NOW + timedelta(days=2)
                )

            async def check(name: str, action: Awaitable[object]) -> None:
                await app.storage.flush()  # чужие фоновые записи — до замера
                before = wire.round_trips
                async with probe.measure(name) as m:
                    await action
                results.append((name, m.all_round_trips, wire.round_trips - before))

            await check("S1 /start", h.send_command(MGR, "start"))
            await check("S9 текст", h.send_text(EMP, "привет"))
            await check("S2 /start нового", h.send_command(2009, "start"))
            await check("S2 ФИО", h.send_text(2009, "Новиков Пётр Ильич"))
            await check("S2 должность", h.send_text(2009, "Юрист"))
            await check("S3 задачи", h.press_menu(MGR, BTN_TASKS))
            await check("S5 меню (запись диалога в фоне)", h.press_menu(MGR, BTN_NEW_TASK))
            await check("S5 сотрудник", h.press_button(MGR, "Иванов"))
            await check("/cancel (удаление диалога в фоне)", h.send_command(MGR, "cancel"))
            await check("S10 run_due_jobs", h.capture(run_due_jobs(h.bot, app.sessionmaker, now=TICK_NOW)))
            await app.idle_connections()
            await app.forget_dialogs()
            await check("S9i после простоя", h.send_text(EMP, "привет"))
            await app.recycle_connections()
            await check("S9c после переподключения", h.send_text(EMP, "привет"))
    finally:
        await main_engine.dispose()
        await fsm_engine.dispose()
        await wire.close()
    report = "\n".join(f"{name}: модель {model}, трафик {real}" for name, model, real in results)
    print(report)
    assert all(model == real for _name, model, real in results), report
