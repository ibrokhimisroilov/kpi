"""bot.ui.render и bot.ui.keyboards: тексты (HTML, экранирование, лимиты) и клавиатуры (callback ≤ 64 байт)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from html.parser import HTMLParser

import pytest
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

from bot.db.models import (
    Attachment,
    AttachmentKind,
    Role,
    Submission,
    Task,
    TaskStatus,
    User,
    UserStatus,
)
from bot.services import tasks as svc
from bot.services.kpi import kpi_for_team, kpi_for_user, team_kpi
from bot.services.periods import get_period
from bot.services.tasks import AttachmentIn
from bot.ui import keyboards as kb
from bot.ui import render, texts
from bot.ui.callbacks import ListCB, PeriodCB, PickCB, SubCB, TaskCB, UserCB

NASTY_TITLE = '<b>Анализ</b> & "договоров" <x>'
NASTY_NAME = "Злой <script>alert(1)</script> & Ко"
RAW_FRAGMENTS = ("<b>Анализ", "<script>", "<x>", "<i>Проверить", "<u>дог", "<b>доделать", "<img")
TELEGRAM_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler", "a", "tg-emoji",
    "code", "pre", "blockquote",
}


class _TelegramHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in TELEGRAM_TAGS:
            self.errors.append(f"недопустимый тег <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"несбалансированный </{tag}>")


def assert_message(text: str) -> None:
    """Непустой HTML для Telegram: только разрешённые теги, баланс, ≤ 4096, пользовательский текст экранирован."""
    assert isinstance(text, str) and text.strip()
    assert len(text) <= 4096
    parser = _TelegramHtml()
    parser.feed(text)
    parser.close()
    assert not parser.errors, parser.errors
    assert not parser.stack, f"не закрыты теги: {parser.stack}"
    for fragment in RAW_FRAGMENTS:
        assert fragment not in text, f"неэкранированный текст {fragment!r}"


def buttons(markup: InlineKeyboardMarkup) -> Iterator:
    for row in markup.inline_keyboard:
        yield from row


def callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    return [button.callback_data for button in buttons(markup)]


def assert_keyboard(markup: InlineKeyboardMarkup) -> None:
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard and all(markup.inline_keyboard)
    for button in buttons(markup):
        assert button.text.strip()
        assert button.callback_data, f"кнопка без callback: {button.text}"
        assert len(button.callback_data.encode()) <= 64, button.callback_data


def picks(markup: InlineKeyboardMarkup) -> list[tuple[str, str]]:
    result = []
    for data in callbacks(markup):
        cb = PickCB.unpack(data)
        result.append((cb.field, cb.value))
    return result


# --- Данные: полный цикл задачи с «опасным» пользовательским текстом ---------------------------------


@pytest.fixture
async def world(session, clock, manager: User, user_factory):
    """Сотрудник с HTML в ФИО и задачи во всех статусах, созданные через сервисы."""
    worker = await user_factory(2101, NASTY_NAME, position="<b>Юрист</b> & <i>аналитик</i>")
    worker.username = "worker"

    async def create(title: str = NASTY_TITLE, days: float = 3, weight: int = 20) -> Task:
        return await svc.create_task(
            session, creator=manager, assignee_id=worker.id, title=title,
            expected_result="<i>Проверить</i> 100 договоров & <img src=x>", deadline=clock.now + timedelta(days=days),
            weight=weight, plan_value=100, plan_unit="<u>дог</u>",
        )

    active = await create()
    await svc.accept_task(session, active.id, worker)

    reviewed = await create(weight=30)
    first = await svc.submit_result(
        session, reviewed.id, worker, fact_text="<b>Сделал</b> & всё", result_text="Отчёт <x>", fact_value=60,
        attachments=[AttachmentIn(AttachmentKind.DOCUMENT, "f1", file_name="<Отчёт>.xlsx"),
                     AttachmentIn(AttachmentKind.PHOTO, "f2")],
    )
    await svc.record_evaluation(session, first.id, score=60, rationale="Факт <b>60</b> & план 100", source="ai",
                                model="m")
    await svc.review_rework(session, first.id, manager, "<b>доделать</b> 40 & <x>", clock.now + timedelta(days=5))
    clock.advance(days=1)
    second = await svc.submit_result(session, reviewed.id, worker, fact_text="Доделал <x>", fact_value=105)
    await svc.record_evaluation(session, second.id, score=105, rationale="Расчёт по правилам: <ok>", source="rules")
    await svc.review_set_score(session, second.id, manager, 100, comment="<b>доделать</b> оформление")

    on_review = await create(weight=10)
    pending_sub = await svc.submit_result(session, on_review.id, worker, fact_text="Готово <x>", fact_value=100)
    await svc.record_evaluation(session, pending_sub.id, score=100, rationale="ok", source="ai", model="m")

    proposal = await svc.propose_task(
        session, employee=worker, title=NASTY_TITLE, expected_result="<i>Проверить</i>",
        deadline=clock.now + timedelta(days=2),
    )
    cancelled = await create(weight=5)
    await svc.cancel_task(session, cancelled.id, manager, "<b>доделать</b> не нужно")

    overdue = await create(days=0.5, weight=15)
    clock.advance(days=1)  # overdue теперь просрочена

    return {
        "worker": worker,
        "manager": manager,
        "active": active,
        "reviewed": reviewed,
        "first": first,
        "second": second,
        "on_review": on_review,
        "pending_sub": pending_sub,
        "proposal": proposal,
        "cancelled": cancelled,
        "overdue": overdue,
    }


# --- render ------------------------------------------------------------------------------------------


async def test_task_texts_are_valid_and_escaped(world, clock) -> None:
    now = clock.now
    tasks = [world[key] for key in ("active", "reviewed", "on_review", "proposal", "cancelled", "overdue")]
    for task in tasks:
        for text in (
            render.task_card(task, now),
            render.task_card(task, now, show_assignee=False),
            render.task_line(task, now),
            render.task_line(task, now, with_assignee=True),
            render.plan_text(task),
            render.deadline_label(task, now),
            render.status_label(task, now),
        ):
            assert_message(text)
    card = render.task_card(world["active"], now)
    assert "&lt;b&gt;Анализ&lt;/b&gt; &amp; &quot;договоров&quot; &lt;x&gt;" in card
    assert "Исполнитель: Злой &lt;. &amp;." in card  # short_name «Злой <. &. К.» — тоже экранирован
    assert f"#{world['active'].id}" in card


async def test_status_and_deadline_labels(world, clock) -> None:
    now = clock.now
    assert render.status_label(world["overdue"], now) == "⏰ Просрочена"
    assert render.status_label(world["active"], now) == render.STATUS_LABELS[TaskStatus.ACTIVE]
    assert render.status_label(world["reviewed"], now) == "✅ Выполнена"
    assert "просрочено на" in render.deadline_label(world["overdue"], now)
    assert "осталось" in render.deadline_label(world["active"], now)
    assert set(render.STATUS_LABELS) == set(TaskStatus)
    assert len(render.PRIORITY_LABELS) == 3


async def test_employee_does_not_see_ai_score_before_decision(world, clock) -> None:
    task = world["on_review"]
    manager_view = render.task_card(task, clock.now)
    employee_view = render.task_card(task, clock.now, show_assignee=False)
    assert "100 %" in manager_view and "AI предлагает" in manager_view
    assert "AI предлагает" not in employee_view


async def test_submission_and_review_texts(world) -> None:
    reviewed, first, second = world["reviewed"], world["first"], world["second"]
    for sub in (first, second, world["pending_sub"]):
        task = sub.task
        assert_message(render.submission_text(task, sub))
        assert_message(render.review_result_text(task, sub))
    text = render.submission_text(reviewed, first)
    assert "План" in text and "Факт" in text
    assert "&lt;Отчёт&gt;.xlsx" in text  # имя файла экранировано
    assert "60 %" in text
    pending = render.submission_text(world["on_review"], world["pending_sub"])
    assert "AI предлагает" in pending
    rules = render.submission_text(reviewed, second)
    assert "Расчёт по правилам" in rules
    # Подпись «Расчёт по правилам» — только в строке с оценкой, обоснование без повтора.
    assert rules.count("Расчёт по правилам") == 1 and "<i>&lt;ok&gt;</i>" in rules
    assert "100 %" in render.review_result_text(reviewed, second)
    assert "доработ" in render.review_result_text(reviewed, first)


async def test_late_submission_wording(session, clock, manager: User, employee: User) -> None:
    task = await svc.create_task(
        session, creator=manager, assignee_id=employee.id, title="Отчёт", expected_result="Сдать отчёт",
        deadline=clock.now + timedelta(days=1), weight=10,
    )
    clock.set(task.deadline + timedelta(days=1, hours=12))
    sub = await svc.submit_result(session, task.id, employee, fact_text="Сдал")
    text = render.submission_text(task, sub)
    assert_message(text)
    assert "с опозданием 1,5 дн." in text
    assert "⏳ Предварительная оценка ещё не рассчитана" in text
    assert "сдано с опозданием 1,5 дн." in render.deadline_label(task, clock.now)


async def test_events_text(world, session) -> None:
    for key in ("reviewed", "proposal", "cancelled", "active"):
        task = world[key]
        events = await svc.task_events(session, task.id)
        assert events
        assert_message(render.events_text(task, events))
    text = render.events_text(world["reviewed"], await svc.task_events(session, world["reviewed"].id))
    assert "попытка 2" in text
    assert_message(render.events_text(world["active"], []))


async def test_events_text_is_cut_to_limit(world, session) -> None:
    task = world["active"]
    for index in range(300):
        await svc.add_event(session, task, world["manager"], svc.EventType.REMINDER, kind=f"before_{index}d")
    text = render.events_text(task, await svc.task_events(session, task.id))
    assert_message(text)
    assert "ранее ещё" in text


async def test_task_summary_draft() -> None:
    draft = {
        "assignee_name": NASTY_NAME,
        "title": NASTY_TITLE,
        "expected_result": "<i>Проверить</i> 100",
        "plan_value": 100,
        "plan_unit": "<u>дог</u>",
        "deadline": "2026-10-05",
        "priority": "high",
        "weight": 20,
    }
    text = render.task_summary_draft(draft)
    assert_message(text)
    assert "05.10" not in text or "5 октября" in text
    assert "🔴 Высокий" in text and "20 %" in text
    assert_message(render.task_summary_draft({"title": "Только название"}))


async def test_kpi_texts(world, session, clock) -> None:
    worker = world["worker"]
    week = get_period("week", 0, clock.now)
    month = get_period("month", 0, clock.now)
    week_res = await kpi_for_user(session, worker.id, week, clock.now)
    month_res = await kpi_for_user(session, worker.id, month, clock.now)
    assert week_res.kpi is not None

    block = render.kpi_block("Неделя", week_res)
    assert_message(block)
    assert block.startswith("<b>Неделя:")
    empty = render.kpi_block("Месяц", type(week_res)())
    assert "нет оценённых задач" in empty

    for card in (
        render.employee_card(worker, week_res, month_res),
        render.employee_card(worker, week_res, month_res, period=month, current=month_res),
    ):
        assert_message(card)
        assert "Злой" in card

    rows = await kpi_for_team(session, week, clock.now)
    dashboard = render.team_dashboard(week, rows, team_kpi(rows))
    assert_message(dashboard)
    assert week.label in dashboard
    assert_message(render.team_dashboard(week, [], None))


async def test_tz_example_in_kpi_block() -> None:
    from bot.services.kpi import KpiItem, KpiResult

    res = KpiResult(
        kpi=101.5, items=[KpiItem(i, f"Задача {i}", 25, 100, False) for i in range(4)], total=4, done=4,
        done_on_time=4, overperformed=2,
    )
    text = render.kpi_block("Неделя", res)
    assert "102 %" in text
    assert "Перевыполнено: 2" in text


async def test_history_and_user_lines(world, session, clock, user_factory) -> None:
    worker = world["worker"]
    history = await svc.evaluated_history(session, worker.id)
    assert history == [world["reviewed"]]
    text = render.history_text(worker, history, 0, 1)
    assert_message(text)
    assert "100 %" in text
    assert_message(render.history_text(worker, [], 0, 0))

    pending = await user_factory(2201, "<b>Новый</b>", status=UserStatus.PENDING)
    blocked = await user_factory(2202, "Блок <x>", status=UserStatus.BLOCKED)
    for user in (worker, world["manager"], pending, blocked):
        assert_message(render.user_line(user))


async def test_help_text(world) -> None:
    for user in (None, world["manager"], world["worker"]):
        text = render.help_text(user)
        assert_message(text)
        assert "ПОРУЧЕНИЕ" in text and "КОЭФФИЦИЕНТ ЭФФЕКТИВНОСТИ" in text
    assert texts.BTN_NEW_TASK in render.help_text(world["manager"])
    assert texts.BTN_MY_TASKS in render.help_text(world["worker"])


# --- keyboards ---------------------------------------------------------------------------------------


async def test_main_menu(world, user_factory) -> None:
    assert isinstance(kb.main_menu(None), ReplyKeyboardRemove)
    pending = await user_factory(2301, "Ждущий", status=UserStatus.PENDING)
    assert isinstance(kb.main_menu(pending), ReplyKeyboardRemove)
    for user, layout in ((world["manager"], texts.MANAGER_MENU_LAYOUT), (world["worker"], texts.EMPLOYEE_MENU_LAYOUT)):
        menu = kb.main_menu(user)
        assert isinstance(menu, ReplyKeyboardMarkup)
        assert [[button.text for button in row] for row in menu.keyboard] == layout
        assert menu.resize_keyboard


def test_dialog_keyboards() -> None:
    assert picks(kb.cancel_kb()) == [("cancel", "")]

    skip = picks(kb.skip_cancel_kb("plan"))
    assert skip[0] == ("plan", "skip") and skip[-1] == ("cancel", "")

    deadline = picks(kb.deadline_kb())
    assert deadline[-1] == ("cancel", "")
    assert all(field == "deadline" and len(value) == 10 for field, value in deadline[:-1])

    assert picks(kb.priority_kb())[:3] == [("prio", "high"), ("prio", "medium"), ("prio", "low")]

    weights = kb.weight_kb(80)
    values = [value for field, value in picks(weights) if field == "weight"]
    assert values == ["5", "10", "15", "20", "25", "30", "40", "50"]
    assert any("⚠️" in button.text for button in buttons(weights))
    assert not any("⚠️" in button.text for button in buttons(kb.weight_kb()))

    assert {value for field, value in picks(kb.ai_suggestion_kb()) if field == "ai"} == {
        "accept", "retry", "manual", "raw"
    }

    confirm = picks(kb.confirm_kb())
    assert ("confirm", "yes") in confirm and ("confirm", "edit") in confirm and ("cancel", "") in confirm
    send = kb.confirm_kb("📤 Отправить", edit=False)
    assert ("confirm", "edit") not in picks(send)
    assert next(buttons(send)).text == "📤 Отправить"

    fields = picks(kb.edit_fields_kb([("title", "Название"), ("deadline", "Срок"), ("weight", "Вес")]))
    assert fields[:3] == [("field", "title"), ("field", "deadline"), ("field", "weight")]

    score = kb.score_kb(105)
    scores = [value for field, value in picks(score) if field == "score"]
    assert {"50", "70", "80", "90", "100", "110", "120"} <= set(scores)
    assert "105" in scores

    assert ("files", "none") in picks(kb.files_kb(0))
    assert ("files", "done") in picks(kb.files_kb(3))
    for markup in (kb.cancel_kb(), kb.skip_cancel_kb(), kb.deadline_kb(), kb.priority_kb(), kb.weight_kb(),
                   kb.ai_suggestion_kb(), kb.confirm_kb(), score, kb.files_kb(2), kb.export_kb()):
        assert_keyboard(markup)


async def test_choose_user_kb(world, employee: User, employee2: User) -> None:
    markup = kb.choose_user_kb([world["worker"], employee, employee2])
    assert_keyboard(markup)
    assert all(len(row) <= 2 for row in markup.inline_keyboard)
    assert picks(markup)[:3] == [("assignee", str(u.id)) for u in (world["worker"], employee, employee2)]


def _task_actions(markup: InlineKeyboardMarkup) -> list[str]:
    return [TaskCB.unpack(data).action for data in callbacks(markup)]


async def test_task_actions_kb(world) -> None:
    manager, worker = world["manager"], world["worker"]
    assert _task_actions(kb.task_actions_kb(world["active"], manager)) == ["edit", "cancel", "history"]
    assert _task_actions(kb.task_actions_kb(world["on_review"], manager)) == ["review", "history"]
    assert _task_actions(kb.task_actions_kb(world["proposal"], manager)) == ["approve", "pedit", "reject", "history"]
    assert _task_actions(kb.task_actions_kb(world["reviewed"], manager)) == ["history"]
    assert _task_actions(kb.task_actions_kb(world["active"], worker)) == ["submit", "history"]  # уже принята
    assert _task_actions(kb.task_actions_kb(world["overdue"], worker)) == ["accept", "submit", "history"]
    assert _task_actions(kb.task_actions_kb(world["on_review"], worker)) == ["history"]

    assert _task_actions(kb.new_task_kb(world["active"])) == ["accept", "open"]
    assert _task_actions(kb.submit_kb(world["active"])) == ["submit", "open"]
    assert _task_actions(kb.proposal_kb(world["proposal"])) == ["approve", "pedit", "reject"]


async def test_review_kb(world) -> None:
    with_files = kb.review_kb(world["first"])
    assert [SubCB.unpack(d).action for d in callbacks(with_files)] == ["ok", "change", "rework", "files"]
    assert any(button.text == "✅ Подтвердить 60 %" for button in buttons(with_files))
    no_score = Submission(id=5, ai_score=None, attachments=[])
    assert [SubCB.unpack(d).action for d in callbacks(kb.review_kb(no_score))] == ["change", "rework"]


async def test_user_keyboards(world, user_factory) -> None:
    manager, worker = world["manager"], world["worker"]
    def actions(markup: InlineKeyboardMarkup) -> list[str]:
        return [UserCB.unpack(data).action for data in callbacks(markup)]

    assert actions(kb.registration_kb(worker)) == ["approve", "reject"]
    pending = await user_factory(2401, "Ждущий", status=UserStatus.PENDING)
    blocked = await user_factory(2402, "Блок", status=UserStatus.BLOCKED)
    other_manager = await user_factory(2403, "Второй Начальник", role=Role.MANAGER)
    assert actions(kb.user_manage_kb(pending, manager)) == ["approve", "reject"]
    assert actions(kb.user_manage_kb(blocked, manager)) == ["card", "unblock"]  # история оценок ушедшего
    assert set(actions(kb.user_manage_kb(worker, manager))) == {"card", "role_mgr", "block"}
    assert set(actions(kb.user_manage_kb(other_manager, manager))) == {"role_emp", "block"}
    # Себя заблокировать или понизить нельзя — таких кнопок нет.
    assert not {"block", "role_emp"} & set(actions(kb.user_manage_kb(manager, manager)))


def test_period_kb_hides_forward_at_current_period() -> None:
    current = kb.period_kb("team", "month", 0)
    assert_keyboard(current)
    assert not any("▶" in button.text for button in buttons(current))
    parsed = [PeriodCB.unpack(d) for d in callbacks(current)]
    assert all(cb.offset <= 0 for cb in parsed)
    assert [cb.kind for cb in parsed[:4]] == ["week", "month", "quarter", "year"]
    selected = [button.text for button in buttons(current) if button.text.startswith("•")]
    assert selected == ["• Месяц"]
    assert any(cb.offset == -1 and cb.kind == "month" for cb in parsed)

    past = kb.period_kb("emp", "week", -2, user_id=7)
    forward = [b for b in buttons(past) if "▶" in b.text]
    assert len(forward) == 1
    cb = PeriodCB.unpack(forward[0].callback_data)
    assert (cb.scope, cb.kind, cb.offset, cb.user_id) == ("emp", "week", -1, 7)


async def test_report_keyboards(world, session, clock) -> None:
    week = get_period("week", 0, clock.now)
    rows = await kpi_for_team(session, week, clock.now)
    team = kb.team_kb(rows, "week", 0)
    assert_keyboard(team)
    cards = [PeriodCB.unpack(d) for d in callbacks(team) if PeriodCB.unpack(d).scope == "emp"]
    assert {cb.user_id for cb in cards} == {user.id for user, _ in rows}
    # Кнопка сотрудника открывает его карточку за тот же период, что выбран на дашборде.
    past = kb.team_kb(rows, "month", -2)
    past_cards = [PeriodCB.unpack(d) for d in callbacks(past) if PeriodCB.unpack(d).scope == "emp"]
    assert past_cards and all((cb.kind, cb.offset) == ("month", -2) for cb in past_cards)

    card_kb = kb.employee_card_kb(world["worker"], "week", -1, back_to_team=True)
    assert_keyboard(card_kb)
    data = callbacks(card_kb)
    assert any(d.startswith("u:history") for d in data)
    assert ListCB(scope="emp", status="all", user_id=world["worker"].id).pack() in data
    assert PeriodCB(scope="team", kind="week", offset=-1).pack() in data
    assert PeriodCB(scope="team", kind="week", offset=-1).pack() not in callbacks(
        kb.employee_card_kb(world["worker"], "week", -1, back_to_team=False)
    )

    export = [PeriodCB.unpack(d) for d in callbacks(kb.export_kb())]
    assert {(cb.kind, cb.offset) for cb in export} >= {("week", 0), ("week", -1), ("month", 0), ("month", -1)}
    assert all(cb.scope == "export" for cb in export)


async def test_task_list_kb_pagination(world) -> None:
    tasks = [world["active"], world["overdue"]]
    first_page = kb.task_list_kb(tasks, "all", "open", 0, 20)
    assert_keyboard(first_page)
    data = callbacks(first_page)
    assert TaskCB(action="open", task_id=world["active"].id).pack() in data
    assert ListCB(scope="all", status="open", page=1).pack() in data
    assert ListCB(scope="all", status="open", page=-1).pack() not in data
    assert any(b.text.startswith("•") and "В работе" in b.text for b in buttons(first_page))

    middle = callbacks(kb.task_list_kb(tasks, "emp", "done", 1, 20, user_id=5))
    assert ListCB(scope="emp", status="done", page=0, user_id=5).pack() in middle
    assert ListCB(scope="emp", status="done", page=2, user_id=5).pack() in middle

    last = callbacks(kb.task_list_kb(tasks, "my", "all", 2, 20, status_tabs=False))
    assert ListCB(scope="my", status="all", page=3).pack() not in last
    assert not any(ListCB.unpack(d).status != "all" for d in last if d.startswith("l:"))

    review = callbacks(kb.task_list_kb([world["on_review"]], "review", "review", 0, 1, status_tabs=False))
    assert TaskCB(action="review", task_id=world["on_review"].id).pack() in review

    history = kb.history_kb(world["worker"].id, 1, 25)
    assert_keyboard(history)
    pages = [UserCB.unpack(d) for d in callbacks(history)]
    assert {(cb.action, cb.page) for cb in pages} >= {("history", 0), ("history", 2), ("card", 0)}


def test_callbacks_fit_64_bytes_with_huge_ids() -> None:
    big = 10**12
    user = User(id=big, tg_id=big, full_name="Очень Длинная Фамилия Сотрудника Для Проверки Кнопок",
                role=Role.EMPLOYEE, status=UserStatus.ACTIVE)
    viewer = User(id=big - 1, tg_id=big - 1, full_name="Начальник", role=Role.MANAGER, status=UserStatus.ACTIVE)
    task = Task(id=big, title="Т" * 255, status=TaskStatus.ACTIVE, assignee_id=big, accepted_at=None,
                deadline=get_period("week").end, assignee=user, submissions=[])
    sub = Submission(id=big, ai_score=150, attachments=[Attachment(kind=AttachmentKind.DOCUMENT, file_id="x")])
    markups = [
        kb.task_actions_kb(task, viewer),
        kb.task_actions_kb(task, user),
        kb.new_task_kb(task),
        kb.submit_kb(task),
        kb.proposal_kb(task),
        kb.review_kb(sub),
        kb.registration_kb(user),
        kb.user_manage_kb(user, viewer),
        kb.choose_user_kb([user]),
        kb.period_kb("export", "quarter", -big, user_id=big),
        kb.employee_card_kb(user, "quarter", -big, back_to_team=True),
        kb.task_list_kb([task], "proposals", "overdue", big, big * 10, user_id=big),
        kb.history_kb(big, big, big * 100),
        kb.edit_fields_kb([("expected_result", "Ожидаемый результат")]),
    ]
    for markup in markups:
        assert_keyboard(markup)
