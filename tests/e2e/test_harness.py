"""Самопроверка e2e-harness (tests/e2e/fakebot.py) без настоящих хендлеров бота.

Собирается отдельный Dispatcher с маленьким локальным роутером (эхо с inline-кнопками,
правка сообщения + alert, отправка/скачивание файлов, FSM, намеренные ошибки) и проверяется,
что FakeSession отвечает как Telegram, а Harness правильно записывает и проверяет запросы.
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from contextlib import suppress

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .fakebot import (
    MANAGER_TG_ID,
    FakeSession,
    Harness,
    HarnessError,
    HtmlParseError,
    parse_html,
)

pytestmark = pytest.mark.asyncio

USER = 2001
BLOCKED_USER = 2002
XLSX = b"PK\x03\x04 fake xlsx content"
ERROR_TEXT = "⚠️ Произошла ошибка, попробуйте ещё раз"


class DemoCB(CallbackData, prefix="demo"):
    action: str
    n: int = 0


class DemoSG(StatesGroup):
    waiting = State()


class ExpectedError(Exception):
    """Аналог DomainError: штатная ошибка, текст показывается пользователю."""


def _kb(*buttons: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in buttons]
    )


def _echo_kb() -> InlineKeyboardMarkup:
    return _kb(
        ("👉 Нажми", DemoCB(action="press").pack()),
        ("🗑 Удалить", DemoCB(action="delete").pack()),
        ("🤐 Молчу", DemoCB(action="silent").pack()),
    )


def _pressed_kb() -> InlineKeyboardMarkup:
    return _kb(("🔁 Ещё раз", DemoCB(action="same").pack()))


def make_router() -> Router:
    """Новый роутер на каждый тест (роутер можно подключить только к одному Dispatcher)."""
    router = Router(name="harness_selftest")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        menu = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Меню 1"), KeyboardButton(text="Меню 2")]], resize_keyboard=True
        )
        name = message.from_user.first_name if message.from_user else "гость"
        await message.answer(f"Привет, <b>{html.escape(name)}</b>!", reply_markup=menu)

    @router.message(Command("doc"))
    async def send_doc(message: Message, bot: Bot) -> None:
        await bot.send_document(
            message.chat.id,
            BufferedInputFile(XLSX, filename="kpi_week.xlsx"),
            caption="📊 Отчёт: <i>неделя</i>",
        )

    @router.message(Command("boom"))
    async def boom(message: Message) -> None:
        raise RuntimeError("boom")

    @router.message(Command("domain"))
    async def domain(message: Message) -> None:
        raise ExpectedError("Задача не найдена.")

    @router.message(Command("badhtml"))
    async def bad_html(message: Message) -> None:
        try:
            await message.answer("План: 5 < 7")  # неэкранированный «<» — Telegram не примет
        except TelegramBadRequest:
            await message.answer("План: 5 &lt; 7")

    @router.message(Command("long"))
    async def long_text(message: Message) -> None:
        await message.answer("x" * 5000)

    @router.message(Command("bigbutton"))
    async def big_button(message: Message) -> None:
        await message.answer("Кнопка", reply_markup=_kb(("Длинная", "x" * 65)))

    @router.message(Command("notify"))
    async def notify(message: Message, bot: Bot) -> None:
        try:
            await bot.send_message(BLOCKED_USER, "Вам задача")
        except TelegramForbiddenError:
            await message.answer("Пользователь заблокировал бота")

    @router.message(Command("album"))
    async def album(message: Message) -> None:
        await message.answer_media_group(
            [
                InputMediaPhoto(media=BufferedInputFile(b"p1", filename="1.jpg"), caption="Фото <b>1</b>"),
                InputMediaPhoto(media=BufferedInputFile(b"p2", filename="2.jpg")),
            ]
        )

    @router.message(Command("mixed"))
    async def mixed_album(message: Message) -> None:
        await message.answer_media_group(
            [
                InputMediaPhoto(media=BufferedInputFile(b"p1", filename="1.jpg")),
                InputMediaDocument(media=BufferedInputFile(b"d1", filename="1.pdf")),
            ]
        )

    @router.message(Command("wait"))
    async def wait(message: Message, state: FSMContext) -> None:
        await state.set_state(DemoSG.waiting)
        await message.answer("Жду текст")

    @router.message(DemoSG.waiting, F.text)
    async def got_text(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(f"Получено: {html.escape(message.text or '')}")

    @router.message(F.document)
    async def got_document(message: Message, bot: Bot) -> None:
        assert message.document is not None
        data = await bot.download(message.document.file_id)
        assert data is not None
        await message.answer(f"Размер {html.escape(message.document.file_name or '')}: {len(data.read())} байт")

    @router.message(F.photo)
    async def got_photo(message: Message, bot: Bot) -> None:
        biggest = message.photo[-1]
        data = await bot.download(biggest)
        assert data is not None
        await message.answer(f"Фото {biggest.width}x{biggest.height}: {len(data.read())} байт")

    @router.message(F.text)
    async def echo(message: Message) -> None:
        await message.answer(f"Эхо: {html.escape(message.text or '')}", reply_markup=_echo_kb())

    @router.callback_query(DemoCB.filter(F.action == "press"))
    async def on_press(callback: CallbackQuery) -> None:
        assert isinstance(callback.message, Message)
        await callback.message.edit_text("Нажато ✅", reply_markup=_pressed_kb())
        await callback.answer("Готово", show_alert=True)

    @router.callback_query(DemoCB.filter(F.action == "same"))
    async def on_same(callback: CallbackQuery) -> None:
        assert isinstance(callback.message, Message)
        try:
            await callback.message.edit_text("Нажато ✅", reply_markup=_pressed_kb())
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
            await callback.answer("Без изменений")
            return
        await callback.answer("Изменено")

    @router.callback_query(DemoCB.filter(F.action == "delete"))
    async def on_delete(callback: CallbackQuery) -> None:
        assert isinstance(callback.message, Message)
        await callback.message.delete()
        await callback.answer("Удалено")

    @router.callback_query(DemoCB.filter(F.action == "check"))
    async def on_check(callback: CallbackQuery) -> None:
        if isinstance(callback.message, InaccessibleMessage):
            await callback.answer("Сообщение недоступно", show_alert=True)
        else:
            await callback.answer("ok")

    @router.callback_query(DemoCB.filter(F.action == "silent"))
    async def on_silent(callback: CallbackQuery) -> None:
        pass  # забыли callback.answer()

    @router.callback_query(DemoCB.filter(F.action == "twice"))
    async def on_twice(callback: CallbackQuery) -> None:
        await callback.answer("1")
        await callback.answer("2")

    @router.callback_query(DemoCB.filter(F.action == "num"))
    async def on_num(callback: CallbackQuery, callback_data: DemoCB) -> None:
        await callback.answer(f"n={callback_data.n}")

    return router


async def on_error(event: ErrorEvent) -> bool:
    """Как обработчик ошибок main.py: штатная ошибка — её текст, прочие — общее сообщение."""
    exc = event.exception
    text = str(exc) if isinstance(exc, ExpectedError) else ERROR_TEXT
    update = event.update
    with suppress(TelegramBadRequest):
        if update.callback_query is not None:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message is not None:
            await update.message.answer(text)
    return True


@pytest_asyncio.fixture
async def h() -> AsyncIterator[Harness]:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(make_router())
    dp.errors.register(on_error)
    bot = Bot("42:TEST", session=FakeSession(), default=DefaultBotProperties(parse_mode="HTML"))
    harness = Harness(dp, bot, expected_errors=(ExpectedError,))
    yield harness
    await bot.session.close()


# --- Сообщения и экран ---------------------------------------------------------------------


async def test_start_and_echo(h: Harness) -> None:
    log = await h.send_command(USER, "start", first_name="Иван <script>")
    assert log.handled
    assert log.texts == ["Привет, Иван <script>!"]  # как увидит пользователь
    assert h.last_html(USER) == "Привет, <b>Иван &lt;script&gt;</b>!"  # как прислал бот
    assert h.reply_keyboard(USER) == ["Меню 1", "Меню 2"]

    log = await h.send_text(USER, "Как дела?")
    assert len(log) == 1 and isinstance(log[0], SendMessage)
    assert log.text == "Эхо: Как дела?"
    assert h.buttons(USER) == ["👉 Нажми", "🗑 Удалить", "🤐 Молчу"]
    assert h.find_button(USER, "нажми") == DemoCB(action="press").pack()
    assert h.has_button(USER, "Удалить") and not h.has_button(USER, "Нет такой")
    assert h.sent_to(USER) == ["Привет, Иван <script>!", "Эхо: Как дела?"]
    # message_id общий для пользователя и бота: /start=1, ответ=2, текст=3, эхо=4.
    last = h.last_message(USER)
    assert last is not None and last.message_id == 4 and last.reply_markup is not None
    assert "Эхо: Как дела?" in h.transcript(USER)
    assert h.find_message(USER, "привет").message_id == 2
    with pytest.raises(LookupError, match="Нет такой"):
        h.find_button(USER, "Нет такой")

    # Кнопка reply-меню — это обычный текст; press_menu проверяет, что она показана.
    log = await h.press_menu(USER, "Меню 1")
    assert log.text == "Эхо: Меню 1"
    with pytest.raises(LookupError, match="Меню 3"):
        await h.press_menu(USER, "Меню 3")


async def test_press_edits_message_and_shows_alert(h: Harness) -> None:
    await h.send_text(USER, "hi")
    echo_id = h.last_message(USER).message_id  # type: ignore[union-attr]

    log = await h.press_button(USER, "Нажми")
    [edit] = log.of(EditMessageText)
    assert edit.message_id == echo_id
    assert h.last_text(USER) == "Нажато ✅"
    assert h.buttons(USER) == ["🔁 Ещё раз"]
    stored = h.last_message(USER)
    assert stored is not None and stored.message_id == echo_id and stored.edits == 1
    assert log.alert == "Готово" and log.answers[0].show_alert is True
    assert h.alerts_for(USER) == ["Готово"]
    assert h.outputs(USER)[-2:] == ["Эхо: hi", "Нажато ✅"]
    assert h.sent_to(USER) == ["Эхо: hi"]  # правка не новое сообщение

    # Та же правка ещё раз -> Telegram: «message is not modified»; хендлер это обработал — не баг.
    log = await h.press_button(USER, "Ещё раз")
    assert log.alert == "Без изменений"
    assert "message is not modified" in log.errors[0].message


async def test_press_with_callback_data_object(h: Harness) -> None:
    await h.send_text(USER, "hi")
    # Кнопки с такими данными нет — «поддельный» callback приходит на последнее сообщение.
    log = await h.press(USER, DemoCB(action="num", n=7))
    assert log.alert == "n=7"
    assert len(h.callback_answers) == 1


async def test_deleted_message_becomes_inaccessible(h: Harness) -> None:
    await h.send_text(USER, "hi")
    echo_id = h.last_message(USER).message_id  # type: ignore[union-attr]
    log = await h.press_button(USER, "Удалить")
    assert log.alert == "Удалено"
    assert h.last_message(USER) is None
    log = await h.press(USER, DemoCB(action="check"), message_id=echo_id)
    assert log.alert == "Сообщение недоступно"


async def test_fsm_state(h: Harness) -> None:
    await h.send_command(USER, "wait")
    assert await h.get_state(USER) == DemoSG.waiting.state
    log = await h.send_text(USER, "<b>жирный</b>")
    assert log.text == "Получено: <b>жирный</b>"  # экранировано ботом -> показано буквально
    assert await h.get_state(USER) is None
    assert await h.get_data(USER) == {}


async def test_unhandled_non_text_message(h: Harness) -> None:
    log = await h.send_sticker(USER)
    assert not log.handled and len(log) == 0


# --- Файлы ---------------------------------------------------------------------------------


async def test_bot_sends_document(h: Harness) -> None:
    log = await h.send_command(USER, "doc")
    [doc] = h.documents_sent(USER)
    assert doc.file_name == "kpi_week.xlsx"
    assert doc.content == XLSX
    assert doc.caption == "📊 Отчёт: неделя"
    assert doc.mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert log.documents == [doc]
    assert h.last_text(USER) == "📊 Отчёт: неделя"
    # Отправленный файл можно скачать по file_id.
    data = await h.bot.download(doc.file_id)
    assert data is not None and data.read() == XLSX


async def test_bot_downloads_user_files(h: Harness) -> None:
    log = await h.send_document(USER, "акт.pdf", "application/pdf", content=b"x" * 1234)
    assert log.text == "Размер акт.pdf: 1234 байт"
    log = await h.send_photo(USER, content=b"y" * 99)
    assert log.text == "Фото 1280x960: 99 байт"
    assert len(h.api.downloads) == 2


async def test_media_group(h: Harness) -> None:
    log = await h.send_command(USER, "album")
    assert [sent.kind for sent in log.files] == ["photo", "photo"]
    assert [sent.content for sent in h.files_sent(USER)] == [b"p1", b"p2"]
    messages = h.messages(USER)[-2:]
    assert messages[0].media_group_id is not None
    assert messages[0].media_group_id == messages[1].media_group_id
    assert log.texts == ["Фото 1"]
    # Документ вперемешку с фото Telegram не примет.
    with pytest.raises(HarnessError, match="grouped"):
        await h.send_command(USER, "mixed")


async def test_capture_outside_updates(h: Harness) -> None:
    log = await h.capture(h.bot.send_message(USER, "<b>Итог</b>: 5 &lt; 7"))
    assert log.texts == ["Итог: 5 < 7"]
    message = log.result
    assert isinstance(message, Message) and message.message_id == 1
    # Сущности восстановлены из HTML, как у настоящего Telegram.
    assert message.html_text == "<b>Итог</b>: 5 &lt; 7"


# --- Строгий режим -------------------------------------------------------------------------


async def test_strict_mode_reports_handler_exception(h: Harness) -> None:
    with pytest.raises(HarnessError, match="RuntimeError: boom") as info:
        await h.send_command(USER, "boom")
    assert isinstance(info.value.__cause__, RuntimeError)

    with h.relaxed():
        log = await h.send_command(USER, "boom")
    assert log.text == ERROR_TEXT


async def test_expected_error_is_not_a_failure(h: Harness) -> None:
    log = await h.send_command(USER, "domain")
    assert log.text == "Задача не найдена."
    assert isinstance(h.errors[-1], ExpectedError)


async def test_strict_mode_reports_bad_html_even_if_caught(h: Harness) -> None:
    with pytest.raises(HarnessError, match="can't parse entities"):
        await h.send_command(USER, "badhtml")
    with h.relaxed():
        log = await h.send_command(USER, "badhtml")
    assert log.texts == ["План: 5 < 7"]
    assert log.errors[0].message.startswith("Bad Request: can't parse entities: Unsupported start tag")


async def test_strict_mode_reports_telegram_limits(h: Harness) -> None:
    with pytest.raises(HarnessError, match="message is too long"):
        await h.send_command(USER, "long")
    with pytest.raises(HarnessError, match="BUTTON_DATA_INVALID"):
        await h.send_command(USER, "bigbutton")


async def test_strict_mode_reports_unanswered_callback(h: Harness) -> None:
    await h.send_text(USER, "hi")
    with pytest.raises(HarnessError, match="callback.answer"):
        await h.press_button(USER, "Молчу")
    with pytest.raises(HarnessError, match="ни один хендлер"):
        await h.press(USER, "unknown:data")


async def test_strict_mode_reports_double_answer(h: Harness) -> None:
    with pytest.raises(HarnessError, match="query is too old"):
        await h.press(USER, DemoCB(action="twice"))


async def test_blocked_chat(h: Harness) -> None:
    h.api.blocked_chats.add(BLOCKED_USER)
    log = await h.send_command(USER, "notify")
    assert log.text == "Пользователь заблокировал бота"
    assert isinstance(log.errors[0], TelegramForbiddenError)


# --- Разбор HTML ---------------------------------------------------------------------------


async def test_parse_html_entities() -> None:
    plain, entities = parse_html('<b>Жирный</b> &amp; <a href="https://ex.com/?a=1&amp;b=2">ссылка</a> 😀<i>x</i>')
    assert plain == "Жирный & ссылка 😀x"
    assert [(e.type, e.offset, e.length) for e in entities] == [
        ("bold", 0, 6),
        ("text_link", 9, 6),
        ("italic", 18, 1),  # эмодзи = 2 UTF-16 code units
    ]
    assert entities[1].url == "https://ex.com/?a=1&b=2"

    plain, entities = parse_html('<pre><code class="language-python">print(1)</code></pre>')
    assert plain == "print(1)"
    assert [(e.type, e.language) for e in entities] == [("pre", "python")]

    plain, entities = parse_html('<span class="tg-spoiler">тайна</span> a & b &nbsp; &#128512;')
    assert plain == "тайна a & b &nbsp; 😀"
    assert entities[0].type == "spoiler"


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("5 < 7", 'Unsupported start tag ""'),
        ("строка<br>строка", 'Unsupported start tag "br"'),
        ("<b>жирный", "Can't find end tag"),
        ("текст</b>", "Unexpected end tag"),
        ("<b>x</i>", 'End tag "i" doesn\'t match start tag "b"'),
        ("<span>x</span>", 'must have class "tg-spoiler"'),
        ("<b", "Unclosed start tag"),
    ],
)
async def test_parse_html_errors(source: str, error: str) -> None:
    with pytest.raises(HtmlParseError, match=re.escape(error)):
        parse_html(source)


# --- Фикстура app (настоящий бот) ----------------------------------------------------------


def _main_import_error() -> str | None:
    try:
        import bot.main  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - main.py может быть ещё не написан
        return f"{type(exc).__name__}: {exc}"
    return None


@pytest.fixture
def main_available() -> None:
    error = _main_import_error()
    if error is not None:
        pytest.skip(f"bot.main пока не импортируется: {error}")


@pytest.mark.parametrize("attempt", [1, 2])  # второй прогон проверяет повторную сборку Dispatcher
async def test_app_fixture_wiring(main_available: None, app: Harness, attempt: int) -> None:
    from bot.config import get_settings

    settings = get_settings()
    assert settings.admin_ids == [MANAGER_TG_ID]
    assert settings.ai_enabled is False

    user = await app.seed_user(3001, "Иванов Иван Иванович")  # type: ignore[attr-defined]
    loaded = await app.get_user(3001)  # type: ignore[attr-defined]
    assert loaded is not None and loaded.id == user.id and loaded.full_name == "Иванов Иван Иванович"

    # Апдейт проходит через middlewares настоящего Dispatcher (поведение хендлеров тут не проверяется).
    with app.relaxed():
        await app.send_command(MANAGER_TG_ID, "start", first_name="Анна")
