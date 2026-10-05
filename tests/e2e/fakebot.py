"""Фейковый Telegram Bot API для e2e-тестов: настоящий aiogram ``Dispatcher``, никакой сети.

Как это устроено
================

``FakeSession``
    Подмена HTTP-сессии aiogram: ``Bot("42:TEST", session=FakeSession(), ...)``. Каждый вызов
    Bot API (``send_message``, ``edit_message_text``, ``answer_callback_query``, ``send_document``…)
    не уходит в сеть, а записывается в ``session.requests`` (объекты методов) и
    ``session.calls`` (метод + чат + текст + результат/ошибка) и получает правдоподобный ответ:
    ``Message`` с растущим ``message_id`` (привязан к боту — можно звать ``msg.edit_text()``),
    ``True``, ``File`` и т. д. ``bot.download(file_id)`` отдаёт байты из ``session.files``.

    Сессия ведёт «экран» каждого чата: ``session.messages[(chat_id, message_id)]`` —
    ``StoredMessage`` с текущим текстом (как его увидит пользователь: HTML разобран в текст +
    entities) и inline-клавиатурой, с учётом правок и удалений.

    Как настоящий Telegram, сессия отвечает ``TelegramBadRequest`` на: невалидный HTML
    («can't parse entities»), текст > 4096 / подпись > 1024 символов, пустой текст,
    callback_data > 64 байт, кнопку без действия, повторный ``answerCallbackQuery``, текст
    ответа на callback > 200 символов, неверный альбом (не 2–10 элементов, документы вперемешку
    с фото), отправку file_id не того типа, «message is not modified», правку/удаление
    несуществующего сообщения. ``session.blocked_chats`` — чаты, где пользователь
    «заблокировал бота» (``TelegramForbiddenError``).

``Harness``
    «Пользователи» бота: строит апдейты и скармливает их ``dp.feed_update`` — текст, команды,
    нажатия inline-кнопок, документы, фото, видео, стикеры. Каждый такой вызов возвращает
    ``RequestLog`` — список запросов бота к API, сделанных во время обработки апдейта
    (``log.text``, ``log.texts``, ``log.to(chat_id)``, ``log.alert``, ``log.documents``…).
    Кнопки: ``press_button(uid, "подстрока текста")`` (inline), ``press(uid, callback_data)``,
    ``press_menu(uid, BTN_...)`` (reply-меню; проверяет, что кнопка сейчас показана).
    Запросы к «экрану»: ``last_text``, ``last_markup``, ``buttons``, ``find_button``,
    ``find_message``, ``reply_keyboard``, ``sent_to``, ``outputs``, ``documents_sent``,
    ``alerts``, ``transcript`` (переписка чата для отладки), ``get_state`` (состояние FSM).

    Строгий режим (по умолчанию). После каждого апдейта проверяется, что:

    * в хендлерах не было исключений, кроме ожидаемых (``expected_errors``; для бота —
      ``DomainError``, его показывает пользователю обработчик ошибок main.py). Обработчик
      ошибок main.py «проглатывает» исключения — harness их всё равно видит;
    * бот не делал заведомо ошибочных запросов (битый HTML, длинный текст, …), даже если
      ошибку кто-то поймал и залогировал (в проде такое сообщение просто не дойдёт);
    * на каждое нажатие кнопки ответили ``callback.answer()`` (иначе «висят часики»).

    Нарушение -> ``HarnessError`` (это ``AssertionError``) с описанием и хвостом переписки;
    исходное исключение — в ``__cause__``. Отключить на время: ``with h.relaxed(): ...``.

``BotHarness``
    То же + доступ к БД бота: ``h.db()`` (``async with h.db() as s``), ``h.scalar(stmt)``,
    ``h.get_user(tg_id)``, ``h.get_task(task_id)``, ``h.seed_user(...)`` (создать
    пользователя напрямую в БД, без диалога регистрации), ``h.capture(coro)`` (записать запросы,
    сделанные не из апдейта, например ``run_reminders``).

Пример сценарного теста (фикстура ``app`` из tests/e2e/conftest.py)
===================================================================

.. code-block:: python

    import pytest

    from bot.ui.callbacks import TaskCB
    from bot.ui.texts import BTN_NEW_TASK

    from .fakebot import MANAGER_TG_ID

    pytestmark = pytest.mark.asyncio


    async def test_employee_registration(app):
        h = app
        # Руководитель: tg_id 1001 указан в ADMIN_IDS -> сразу активный руководитель.
        await h.send_command(MANAGER_TG_ID, "start", first_name="Анна")
        assert BTN_NEW_TASK in h.reply_keyboard(MANAGER_TG_ID)

        # Сотрудник регистрируется сам: /start -> ФИО -> должность (пропустить).
        emp = 2001
        await h.send_command(emp, "start", first_name="Иван")
        await h.send_text(emp, "Иванов Иван Иванович")
        await h.press_button(emp, "Пропустить")          # ищет кнопку по подстроке текста
        assert "заявк" in h.last_text(emp).lower()

        # Руководителю пришла заявка с inline-кнопками -> подтверждаем.
        log = await h.press_button(MANAGER_TG_ID, "Подтвердить")
        assert log.answers                                 # callback.answer() был
        assert log.to(emp).texts                           # сотруднику пришло уведомление
        user = await h.get_user(emp)
        assert user.status == "active"

        # Нажатие по callback_data (например, «поддельный» callback от чужого пользователя):
        log = await h.press(emp, TaskCB(action="open", task_id=999))
        print(log.alert)                                   # текст alert/toast ответа


    async def test_fast_setup(app):
        h = app
        mgr = await h.seed_user(MANAGER_TG_ID, "Петрова Анна Сергеевна", role="manager")
        emp = await h.seed_user(2002, "Сидоров Пётр Ильич")  # активный сотрудник
        await h.send_text(2002, "📋 Мои задачи")
        print(h.transcript(2002))                          # переписка чата для отладки
"""

from __future__ import annotations

import contextlib
import itertools
import mimetypes
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Dispatcher
from aiogram import methods as m
from aiogram.client.default import Default
from aiogram.client.session.base import BaseSession
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import TelegramMethod
from aiogram.types import (
    Animation,
    Audio,
    CallbackQuery,
    Chat,
    Document,
    File,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    MessageEntity,
    MessageId,
    PhotoSize,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Sticker,
    Update,
    Video,
    Voice,
)
from aiogram.types import User as TgUser

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "MANAGER_TG_ID",
    "ApiCall",
    "BotHarness",
    "FakeSession",
    "FileInfo",
    "Harness",
    "HarnessError",
    "HtmlParseError",
    "RequestLog",
    "SentFile",
    "StoredMessage",
    "parse_html",
]

# Совпадает с ADMIN_IDS в tests/e2e/conftest.py: этот пользователь после /start — руководитель.
MANAGER_TG_ID = 1001

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
CALLBACK_DATA_LIMIT = 64
CALLBACK_ANSWER_LIMIT = 200
GET_FILE_LIMIT = 20 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _u16(text: str) -> int:
    """Длина в UTF-16 code units — так Telegram считает offset/length у entities."""
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------------------------
# HTML parse mode — повторяет правила Telegram (tdlib parse_html)
# ---------------------------------------------------------------------------------------------


class HtmlParseError(ValueError):
    """Текст не разбирается как Telegram-HTML (Telegram ответил бы «can't parse entities»)."""


_ALLOWED_TAGS = frozenset(
    {
        "a", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler",
        "tg-emoji", "tg-time", "code", "pre", "blockquote",
    }
)
_ENTITY_TYPES = {
    "b": "bold",
    "strong": "bold",
    "i": "italic",
    "em": "italic",
    "u": "underline",
    "ins": "underline",
    "s": "strikethrough",
    "strike": "strikethrough",
    "del": "strikethrough",
    "span": "spoiler",
    "tg-spoiler": "spoiler",
    "code": "code",
    "pre": "pre",
    "blockquote": "blockquote",
    "a": "text_link",
}
_HTML_ENTITY_RE = re.compile(r"&(?:#x([0-9a-fA-F]{1,8})|#([0-9]{1,8})|(lt|gt|amp|quot)(?![A-Za-z]));?")
_NAMED = {"lt": "<", "gt": ">", "amp": "&", "quot": '"'}
_ATTR_RE = re.compile(r"""([^\s=>"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+)))?""")


def _decode_entity(match: re.Match[str]) -> str | None:
    hex_code, dec_code, name = match.group(1), match.group(2), match.group(3)
    if name:
        return _NAMED[name]
    code = int(hex_code, 16) if hex_code else int(dec_code)
    if code == 0 or code >= 0x10FFFF:
        return None
    return chr(code)


def _unescape(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        decoded = _decode_entity(match)
        return match.group(0) if decoded is None else decoded

    return _HTML_ENTITY_RE.sub(repl, value)


def parse_html(source: str) -> tuple[str, list[MessageEntity]]:
    """Разбирает текст в parse_mode=HTML так же, как Telegram: -> (видимый текст, entities).

    Поддерживаемые теги и сущности — как в Bot API (b, i, u, s, a, code, pre, blockquote,
    tg-spoiler, span class="tg-spoiler", tg-emoji; &lt; &gt; &amp; &quot; и числовые).
    Неподдерживаемый тег, «голый» ``<``, незакрытый/лишний закрывающий тег -> HtmlParseError
    с текстом, как у Telegram.
    """
    out: list[str] = []
    length = 0  # текущая длина результата в UTF-16
    stack: list[tuple[str, int, dict[str, str]]] = []
    entities: list[MessageEntity] = []
    i, n = 0, len(source)

    def emit(chunk: str) -> None:
        nonlocal length
        out.append(chunk)
        length += _u16(chunk)

    def byte_offset(pos: int) -> int:
        return len(source[:pos].encode("utf-8"))

    while i < n:
        ch = source[i]
        if ch == "&":
            match = _HTML_ENTITY_RE.match(source, i)
            decoded = _decode_entity(match) if match else None
            if match and decoded is not None:
                emit(decoded)
                i = match.end()
            else:
                emit("&")
                i += 1
            continue
        if ch != "<":
            j = i
            while j < n and source[j] not in "<&":
                j += 1
            emit(source[i:j])
            i = j
            continue

        begin = i
        if i + 1 < n and source[i + 1] == "/":
            # Закрывающий тег.
            j = i + 2
            while j < n and not source[j].isspace() and source[j] != ">":
                j += 1
            end_name = source[i + 2 : j].lower()
            while j < n and source[j].isspace():
                j += 1
            if j >= n or source[j] != ">":
                raise HtmlParseError(f"Unclosed end tag at byte offset {byte_offset(begin)}")
            if not stack:
                raise HtmlParseError(f"Unexpected end tag at byte offset {byte_offset(begin)}")
            tag, start, attrs = stack.pop()
            if end_name and end_name != tag:
                raise HtmlParseError(f'End tag "{end_name}" doesn\'t match start tag "{tag}"')
            entity = _make_entity(tag, start, length - start, attrs, stack)
            if entity is not None:
                entities.append(entity)
            i = j + 1
            continue

        # Открывающий тег.
        j = i + 1
        while j < n and not source[j].isspace() and source[j] != ">":
            j += 1
        if j >= n:
            raise HtmlParseError(f"Unclosed start tag at byte offset {byte_offset(begin)}")
        name = source[i + 1 : j].lower()
        if name not in _ALLOWED_TAGS:
            raise HtmlParseError(f'Unsupported start tag "{name}" at byte offset {byte_offset(begin)}')
        attrs: dict[str, str] = {}
        k = j
        while True:
            while k < n and source[k].isspace():
                k += 1
            if k >= n:
                raise HtmlParseError(f"Unclosed start tag at byte offset {byte_offset(begin)}")
            if source[k] == ">":
                k += 1
                break
            match = _ATTR_RE.match(source, k)
            if not match:
                raise HtmlParseError(f'Unexpected character in tag "{name}" at byte offset {byte_offset(k)}')
            value = next((g for g in match.groups()[1:] if g is not None), "")
            attrs[match.group(1).lower()] = _unescape(value)
            k = match.end()
        if name == "span" and attrs.get("class") != "tg-spoiler":
            raise HtmlParseError('Tag "span" must have class "tg-spoiler"')
        stack.append((name, length, attrs))
        i = k

    if stack:
        raise HtmlParseError(f'Can\'t find end tag corresponding to start tag "{stack[-1][0]}"')
    entities.sort(key=lambda e: (e.offset, -e.length))
    return "".join(out), entities


def _make_entity(
    tag: str, offset: int, size: int, attrs: dict[str, str], stack: list[tuple[str, int, dict[str, str]]]
) -> MessageEntity | None:
    if tag == "code" and stack and stack[-1][0] == "pre":
        # <pre><code class="language-x"> — Telegram делает одну сущность pre с language.
        cls = attrs.get("class", "")
        if cls.startswith("language-"):
            stack[-1][2]["_language"] = cls[len("language-") :]
        return None
    entity_type = _ENTITY_TYPES.get(tag)
    if entity_type is None or size <= 0:
        return None
    if tag == "a":
        url = attrs.get("href", "")
        if not url:
            return None
        return MessageEntity(type="text_link", offset=offset, length=size, url=url)
    if tag == "pre":
        return MessageEntity(type="pre", offset=offset, length=size, language=attrs.get("_language"))
    if tag == "blockquote" and "expandable" in attrs:
        entity_type = "expandable_blockquote"
    return MessageEntity(type=entity_type, offset=offset, length=size)


# ---------------------------------------------------------------------------------------------
# Хранилище «экрана» и файлов
# ---------------------------------------------------------------------------------------------


@dataclass
class FileInfo:
    """Файл, «лежащий на серверах Telegram» (по file_id)."""

    file_id: str
    file_unique_id: str
    kind: str  # document | photo | video | audio | voice | animation | sticker
    content: bytes
    file_name: str | None = None
    mime_type: str | None = None

    @property
    def file_path(self) -> str:
        return f"files/{self.file_id}"


@dataclass
class SentFile:
    """Файл, который бот отправил в чат."""

    chat_id: int
    message_id: int
    kind: str
    file_id: str
    file_name: str | None
    mime_type: str | None
    content: bytes | None
    caption: str | None


@dataclass
class StoredMessage:
    """Сообщение в чате (бота или пользователя) в его текущем виде."""

    chat_id: int
    message_id: int
    kind: str  # text | document | photo | video | audio | voice | animation | sticker | other
    from_bot: bool = True
    text: str | None = None  # видимый текст (HTML разобран); None у медиа
    entities: list[MessageEntity] = field(default_factory=list)
    caption: str | None = None
    caption_entities: list[MessageEntity] = field(default_factory=list)
    html: str | None = None  # текст/подпись ровно как их прислал бот
    reply_markup: InlineKeyboardMarkup | None = None
    media: dict[str, Any] = field(default_factory=dict)  # document=..., photo=[...], ...
    media_group_id: str | None = None
    file: FileInfo | None = None
    from_user: TgUser | None = None
    sent_text: str | None = None  # текст/подпись на момент отправки
    date: datetime = field(default_factory=_now)
    edit_date: int | None = None  # unix time, как в Bot API
    edits: int = 0
    deleted: bool = False

    @property
    def content(self) -> str:
        """Текст или подпись (пустая строка, если нет ни того, ни другого)."""
        if self.text is not None:
            return self.text
        return self.caption or ""

    @property
    def buttons(self) -> list[InlineKeyboardButton]:
        if self.reply_markup is None:
            return []
        return [button for row in self.reply_markup.inline_keyboard for button in row]

    @property
    def button_texts(self) -> list[str]:
        return [button.text for button in self.buttons]


@dataclass
class ApiCall:
    """Один вызов Bot API: метод, чат, видимый текст, результат или ошибка."""

    method: TelegramMethod[Any]
    chat_id: int | None
    text: str | None = None
    result: Any = None
    error: TelegramAPIError | None = None
    message_ids: list[int] = field(default_factory=list)
    files: list[SentFile] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


_MEDIA_METHODS: dict[type, tuple[str, str]] = {
    m.SendDocument: ("document", "document"),
    m.SendPhoto: ("photo", "photo"),
    m.SendVideo: ("video", "video"),
    m.SendAudio: ("audio", "audio"),
    m.SendVoice: ("voice", "voice"),
    m.SendAnimation: ("animation", "animation"),
    m.SendSticker: ("sticker", "sticker"),
}
_INPUT_MEDIA_KINDS: dict[type, str] = {
    InputMediaPhoto: "photo",
    InputMediaVideo: "video",
    InputMediaDocument: "document",
    InputMediaAudio: "audio",
    InputMediaAnimation: "animation",
}
_DEFAULT_NAMES = {
    "document": "document.bin",
    "photo": "photo.jpg",
    "video": "video.mp4",
    "audio": "audio.mp3",
    "voice": "voice.ogg",
    "animation": "animation.mp4",
    "sticker": "sticker.webp",
}
_MEDIA_FIELDS = frozenset({"document", "photo", "video", "audio", "voice", "animation", "sticker"})
# Типы, file_id которых нельзя отправить «чужим» методом (Telegram: type of file mismatch).
_STRICT_KINDS = frozenset({"document", "photo", "video"})
_BUTTON_ACTIONS = (
    "url",
    "callback_data",
    "web_app",
    "login_url",
    "switch_inline_query",
    "switch_inline_query_current_chat",
    "switch_inline_query_chosen_chat",
    "copy_text",
    "callback_game",
    "pay",
)


def media_payload(info: FileInfo) -> dict[str, Any]:
    """Поля Message (document=..., photo=[...], ...) для файла."""
    size = len(info.content)
    common = {"file_id": info.file_id, "file_unique_id": info.file_unique_id, "file_size": size}
    if info.kind == "photo":
        thumb = PhotoSize(
            file_id=f"{info.file_id}_s",
            file_unique_id=f"{info.file_unique_id}_s",
            width=90,
            height=67,
            file_size=min(size, 1000),
        )
        return {"photo": [thumb, PhotoSize(width=1280, height=960, **common)]}
    if info.kind == "video":
        return {
            "video": Video(
                width=1280, height=720, duration=3, file_name=info.file_name, mime_type=info.mime_type, **common
            )
        }
    if info.kind == "audio":
        return {"audio": Audio(duration=3, file_name=info.file_name, mime_type=info.mime_type, **common)}
    if info.kind == "voice":
        return {"voice": Voice(duration=3, mime_type=info.mime_type, **common)}
    if info.kind == "animation":
        return {
            "animation": Animation(
                width=320, height=240, duration=2, file_name=info.file_name, mime_type=info.mime_type, **common
            )
        }
    if info.kind == "sticker":
        return {
            "sticker": Sticker(
                type="regular", width=512, height=512, is_animated=False, is_video=False, emoji="👍", **common
            )
        }
    return {"document": Document(file_name=info.file_name, mime_type=info.mime_type, **common)}


# ---------------------------------------------------------------------------------------------
# FakeSession
# ---------------------------------------------------------------------------------------------


class FakeSession(BaseSession):
    """Сессия aiogram без сети: записывает запросы и отвечает как Telegram.

    :param check_html: проверять HTML/длину/клавиатуры как Telegram (по умолчанию да).
    """

    def __init__(self, *, check_html: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.check_html = check_html
        self.requests: list[TelegramMethod[Any]] = []
        self.calls: list[ApiCall] = []
        # «Экран»: сообщения бота и входящие сообщения пользователей.
        self.messages: dict[tuple[int, int], StoredMessage] = {}
        self.incoming: dict[tuple[int, int], StoredMessage] = {}
        # Файлы: file_path -> байты (для bot.download) и file_id -> описание.
        self.files: dict[str, bytes] = {}
        self.file_info: dict[str, FileInfo] = {}
        self.sent_files: list[SentFile] = []
        self.downloads: list[str] = []
        # Текущая reply-клавиатура чата (None — убрана/не было).
        self.reply_keyboards: dict[int, ReplyKeyboardMarkup | None] = {}
        # Пользователи, «заблокировавшие бота»: любые отправки им -> TelegramForbiddenError.
        self.blocked_chats: set[int] = set()
        # Профили Telegram-пользователей (заполняет Harness) — для Chat.first_name и т. п.
        self.profiles: dict[int, TgUser] = {}
        # callback_query_id -> user_id и ответы на них.
        self.callback_users: dict[str, int] = {}
        self.callback_answers: dict[str, m.AnswerCallbackQuery] = {}
        # Все ошибки API, которые вернула сессия, и подмножество «программных» (баг в коде бота).
        self.errors: list[TelegramAPIError] = []
        self.bug_errors: list[TelegramAPIError] = []
        self.closed = False
        self._last_ids: dict[int, int] = {}
        self._touched: dict[int, list[int]] = {}
        self._file_seq = itertools.count(1)
        self._group_seq = itertools.count(1)
        self._bot_user: TgUser | None = None

    # --- BaseSession -------------------------------------------------------------------------

    async def close(self) -> None:
        self.closed = True

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        self.requests.append(method)
        call = ApiCall(method=method, chat_id=_chat_id_of(method))
        self.calls.append(call)
        try:
            result = await self._handle(bot, method, call)
        except TelegramAPIError as exc:
            call.error = exc
            self.errors.append(exc)
            raise
        call.result = result
        return result

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        path = url.split("/file/bot", 1)[1].split("/", 1)[-1] if "/file/bot" in url else url
        self.downloads.append(path)
        data = self.files.get(path, b"test")
        for start in range(0, len(data), max(chunk_size, 1)):
            yield data[start : start + chunk_size]

    # --- Публичные хелперы -------------------------------------------------------------------

    def next_message_id(self, chat_id: int) -> int:
        """Следующий message_id в чате (общий счётчик для сообщений бота и пользователя)."""
        value = self._last_ids.get(chat_id, 0) + 1
        self._last_ids[chat_id] = value
        return value

    def register_file(
        self,
        kind: str,
        content: bytes = b"test",
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
        file_id: str | None = None,
    ) -> FileInfo:
        """Положить файл «на сервер Telegram» (его можно скачать и переслать по file_id)."""
        seq = next(self._file_seq)
        file_id = file_id or f"{kind}-{seq}"
        if mime_type is None and file_name:
            mime_type = mimetypes.guess_type(file_name)[0]
        info = FileInfo(
            file_id=file_id,
            file_unique_id=f"u-{file_id}",
            kind=kind,
            content=content,
            file_name=file_name,
            mime_type=mime_type,
        )
        self.file_info[file_id] = info
        self.files[info.file_path] = content
        if kind == "photo":
            thumb = FileInfo(f"{file_id}_s", f"u-{file_id}_s", "photo", b"thumb", None, "image/jpeg")
            self.file_info[thumb.file_id] = thumb
            self.files[thumb.file_path] = thumb.content
        return info

    def bot_user(self, bot: Bot) -> TgUser:
        if self._bot_user is None or self._bot_user.id != bot.id:
            self._bot_user = TgUser(id=bot.id, is_bot=True, first_name="Test Bot", username="test_bot")
        return self._bot_user

    def chat(self, chat_id: int) -> Chat:
        if chat_id < 0:
            return Chat(id=chat_id, type="supergroup", title=f"Group {chat_id}")
        profile = self.profiles.get(chat_id)
        if profile is None:
            return Chat(id=chat_id, type="private")
        return Chat(
            id=chat_id,
            type="private",
            first_name=profile.first_name,
            last_name=profile.last_name,
            username=profile.username,
        )

    def to_message(self, bot: Bot, stored: StoredMessage) -> Message:
        """Сообщение из хранилища как объект aiogram, привязанный к боту."""
        message = Message(
            message_id=stored.message_id,
            date=stored.date,
            edit_date=stored.edit_date,
            chat=self.chat(stored.chat_id),
            from_user=self.bot_user(bot) if stored.from_bot else stored.from_user,
            text=stored.text,
            entities=stored.entities or None,
            caption=stored.caption,
            caption_entities=stored.caption_entities or None,
            reply_markup=stored.reply_markup,
            media_group_id=stored.media_group_id,
            **stored.media,
        )
        return message.as_(bot)

    def chat_messages(self, chat_id: int, *, include_deleted: bool = False) -> list[StoredMessage]:
        """Сообщения бота в чате в порядке отправки."""
        return [
            msg
            for (cid, _), msg in self.messages.items()
            if cid == chat_id and (include_deleted or not msg.deleted)
        ]

    def recent_messages(self, chat_id: int) -> Iterator[StoredMessage]:
        """Сообщения бота в чате, от последнего отправленного/изменённого к первому (без удалённых)."""
        for message_id in reversed(self._touched.get(chat_id, [])):
            stored = self.messages[(chat_id, message_id)]
            if not stored.deleted:
                yield stored

    def last_message(self, chat_id: int) -> StoredMessage | None:
        return next(self.recent_messages(chat_id), None)

    def add_incoming(self, stored: StoredMessage) -> None:
        self.incoming[(stored.chat_id, stored.message_id)] = stored

    # --- Внутреннее --------------------------------------------------------------------------

    def _bad(self, method: TelegramMethod[Any], message: str, *, bug: bool = False) -> TelegramBadRequest:
        exc = TelegramBadRequest(method=method, message=message)
        if bug:
            self.bug_errors.append(exc)
        return exc

    def _touch(self, chat_id: int, message_id: int) -> None:
        touched = self._touched.setdefault(chat_id, [])
        if message_id in touched:
            touched.remove(message_id)
        touched.append(message_id)

    def _store(self, chat_id: int, **fields: Any) -> StoredMessage:
        markup = fields.pop("reply_markup", None)
        stored = StoredMessage(
            chat_id=chat_id,
            message_id=self.next_message_id(chat_id),
            reply_markup=markup if isinstance(markup, InlineKeyboardMarkup) else None,
            **fields,
        )
        stored.sent_text = stored.content
        self.messages[(chat_id, stored.message_id)] = stored
        self._touch(chat_id, stored.message_id)
        return stored

    def _render(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        text: str,
        parse_mode: Any,
        entities: list[MessageEntity] | None,
        *,
        caption: bool = False,
    ) -> tuple[str, list[MessageEntity]]:
        """Текст, как его покажет Telegram (с проверками Telegram)."""
        if isinstance(parse_mode, Default):
            parse_mode = bot.default[parse_mode.name]
        if parse_mode is not None and str(getattr(parse_mode, "value", parse_mode)).upper() == "HTML" and not entities:
            try:
                plain, parsed = parse_html(text)
            except HtmlParseError as exc:
                if self.check_html:
                    raise self._bad(method, f"Bad Request: can't parse entities: {exc}", bug=True) from None
                plain, parsed = text, []
        else:
            plain, parsed = text, list(entities or [])
        if self.check_html:
            if caption and len(plain) > CAPTION_LIMIT:
                raise self._bad(method, "Bad Request: message caption is too long", bug=True)
            if not caption and len(plain) > TEXT_LIMIT:
                raise self._bad(method, "Bad Request: message is too long", bug=True)
            if not caption and not plain.strip():
                raise self._bad(method, "Bad Request: message text is empty", bug=True)
        return plain, parsed

    def _check_markup(self, method: TelegramMethod[Any], markup: Any) -> None:
        if not self.check_html or not isinstance(markup, InlineKeyboardMarkup):
            return
        for row in markup.inline_keyboard:
            for button in row:
                if not button.text:
                    raise self._bad(method, "Bad Request: inline keyboard button text is empty", bug=True)
                if not any(getattr(button, name, None) is not None for name in _BUTTON_ACTIONS):
                    raise self._bad(
                        method, "Bad Request: text buttons are unallowed in the inline keyboard", bug=True
                    )
                data = button.callback_data
                if data is not None and not 1 <= len(data.encode("utf-8")) <= CALLBACK_DATA_LIMIT:
                    raise self._bad(method, "Bad Request: BUTTON_DATA_INVALID", bug=True)

    def _apply_reply_keyboard(self, chat_id: int, markup: Any) -> None:
        if isinstance(markup, ReplyKeyboardMarkup):
            self.reply_keyboards[chat_id] = markup
        elif isinstance(markup, ReplyKeyboardRemove):
            self.reply_keyboards[chat_id] = None

    def _check_blocked(self, method: TelegramMethod[Any], chat_id: int | None) -> None:
        if chat_id is not None and chat_id in self.blocked_chats:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")

    def _get_editable(self, method: TelegramMethod[Any], chat_id: int, message_id: int) -> StoredMessage:
        stored = self.messages.get((chat_id, message_id))
        if stored is None or stored.deleted:
            if (chat_id, message_id) in self.incoming:
                raise self._bad(method, "Bad Request: message can't be edited")
            raise self._bad(method, "Bad Request: message to edit not found")
        return stored

    async def _input_file(self, bot: Bot, method: TelegramMethod[Any], value: Any, kind: str) -> FileInfo:
        if isinstance(value, InputFile):
            chunks = [chunk async for chunk in value.read(bot)]
            return self.register_file(kind, b"".join(chunks), file_name=value.filename or _DEFAULT_NAMES[kind])
        if isinstance(value, str):
            info = self.file_info.get(value)
            if info is None:
                # Неизвестный file_id/URL (например, засеян прямо в БД) — считаем валидным.
                return self.register_file(kind, b"test", file_id=value)
            if self.check_html and kind != info.kind and {kind, info.kind} <= _STRICT_KINDS:
                raise self._bad(method, "Bad Request: type of file mismatch", bug=True)
            if kind == info.kind:
                return info
            return FileInfo(info.file_id, info.file_unique_id, kind, info.content, info.file_name, info.mime_type)
        raise self._bad(method, f"Bad Request: unsupported file value {type(value).__name__}", bug=True)

    async def _handle(self, bot: Bot, method: TelegramMethod[Any], call: ApiCall) -> Any:
        if not isinstance(method, m.AnswerCallbackQuery | m.GetMe | m.GetFile):
            self._check_blocked(method, call.chat_id)

        if isinstance(method, m.SendMessage):
            return self._send_message(bot, method, call)
        if type(method) in _MEDIA_METHODS:
            return await self._send_media(bot, method, call)
        if isinstance(method, m.SendMediaGroup):
            return await self._send_media_group(bot, method, call)
        if isinstance(method, m.EditMessageText):
            return self._edit_text(bot, method, call)
        if isinstance(method, m.EditMessageReplyMarkup):
            return self._edit_markup(bot, method, call)
        if isinstance(method, m.EditMessageCaption):
            return self._edit_caption(bot, method, call)
        if isinstance(method, m.CopyMessage | m.ForwardMessage):
            return self._copy(bot, method, call)
        if isinstance(method, m.DeleteMessage):
            return self._delete(method, call.chat_id or 0, [method.message_id])
        if isinstance(method, m.DeleteMessages):
            return self._delete(method, call.chat_id or 0, list(method.message_ids))
        if isinstance(method, m.AnswerCallbackQuery):
            return self._answer_callback(method)
        if isinstance(method, m.GetMe):
            return self.bot_user(bot)
        if isinstance(method, m.GetFile):
            return self._get_file(method)
        if isinstance(method, m.GetMyCommands):
            return []
        if isinstance(method, m.GetUpdates):
            return []
        if getattr(method, "__returning__", None) is bool:
            # SendChatAction, SetMyCommands, DeleteWebhook, SetChatMenuButton, ...
            return True
        raise NotImplementedError(
            f"FakeSession: метод {type(method).__name__} не эмулируется — добавьте его в tests/e2e/fakebot.py"
        )

    def _send_message(self, bot: Bot, method: m.SendMessage, call: ApiCall) -> Message:
        chat_id = _require_chat(call)
        plain, entities = self._render(bot, method, method.text, method.parse_mode, method.entities)
        self._check_markup(method, method.reply_markup)
        self._apply_reply_keyboard(chat_id, method.reply_markup)
        stored = self._store(
            chat_id,
            kind="text",
            text=plain,
            entities=entities,
            html=method.text,
            reply_markup=method.reply_markup,
        )
        call.text = plain
        call.message_ids.append(stored.message_id)
        return self.to_message(bot, stored)

    async def _send_media(self, bot: Bot, method: TelegramMethod[Any], call: ApiCall) -> Message:
        chat_id = _require_chat(call)
        kind, attr = _MEDIA_METHODS[type(method)]
        info = await self._input_file(bot, method, getattr(method, attr), kind)
        raw_caption = getattr(method, "caption", None)
        caption, caption_entities = None, []
        if raw_caption:
            caption, caption_entities = self._render(
                bot,
                method,
                raw_caption,
                getattr(method, "parse_mode", None),
                getattr(method, "caption_entities", None),
                caption=True,
            )
        markup = getattr(method, "reply_markup", None)
        self._check_markup(method, markup)
        self._apply_reply_keyboard(chat_id, markup)
        stored = self._store(
            chat_id,
            kind=kind,
            caption=caption,
            caption_entities=caption_entities,
            html=raw_caption,
            reply_markup=markup,
            media=media_payload(info),
            file=info,
        )
        sent = SentFile(
            chat_id, stored.message_id, kind, info.file_id, info.file_name, info.mime_type, info.content, caption
        )
        self.sent_files.append(sent)
        call.files.append(sent)
        call.text = caption
        call.message_ids.append(stored.message_id)
        return self.to_message(bot, stored)

    async def _send_media_group(self, bot: Bot, method: m.SendMediaGroup, call: ApiCall) -> list[Message]:
        chat_id = _require_chat(call)
        items = list(method.media)
        if self.check_html:
            if not 2 <= len(items) <= 10:
                raise self._bad(method, "Bad Request: media group must include 2-10 items", bug=True)
            kinds = {_INPUT_MEDIA_KINDS.get(type(item), "other") for item in items}
            if len(kinds) > 1 and kinds & {"document", "audio"}:
                raise self._bad(
                    method,
                    "Bad Request: documents and audio can be grouped only with media of the same type",
                    bug=True,
                )
        group_id = f"album-{next(self._group_seq)}"
        result: list[Message] = []
        captions: list[str] = []
        for item in items:
            kind = _INPUT_MEDIA_KINDS.get(type(item), "document")
            info = await self._input_file(bot, method, item.media, kind)
            caption, caption_entities = None, []
            if item.caption:
                caption, caption_entities = self._render(
                    bot, method, item.caption, item.parse_mode, item.caption_entities, caption=True
                )
                captions.append(caption)
            stored = self._store(
                chat_id,
                kind=kind,
                caption=caption,
                caption_entities=caption_entities,
                html=item.caption,
                media=media_payload(info),
                media_group_id=group_id,
                file=info,
            )
            sent = SentFile(
                chat_id, stored.message_id, kind, info.file_id, info.file_name, info.mime_type, info.content, caption
            )
            self.sent_files.append(sent)
            call.files.append(sent)
            call.message_ids.append(stored.message_id)
            result.append(self.to_message(bot, stored))
        call.text = "\n".join(captions) if captions else None
        return result

    def _edit_text(self, bot: Bot, method: m.EditMessageText, call: ApiCall) -> Message | bool:
        if method.inline_message_id:
            return True
        chat_id = _require_chat(call)
        stored = self._get_editable(method, chat_id, int(method.message_id or 0))
        if stored.text is None:
            raise self._bad(method, "Bad Request: there is no text in the message to edit")
        plain, entities = self._render(bot, method, method.text, method.parse_mode, method.entities)
        self._check_markup(method, method.reply_markup)
        markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
        if (
            plain == stored.text
            and _dump(entities) == _dump(stored.entities)
            and _dump(markup) == _dump(stored.reply_markup)
        ):
            raise self._bad(
                method,
                "Bad Request: message is not modified: specified new message content and reply markup "
                "are exactly the same as a current content and reply markup of the message",
            )
        stored.text, stored.entities, stored.html, stored.reply_markup = plain, entities, method.text, markup
        self._mark_edited(stored)
        call.text = plain
        call.message_ids.append(stored.message_id)
        return self.to_message(bot, stored)

    def _edit_markup(self, bot: Bot, method: m.EditMessageReplyMarkup, call: ApiCall) -> Message | bool:
        if method.inline_message_id:
            return True
        chat_id = _require_chat(call)
        stored = self._get_editable(method, chat_id, int(method.message_id or 0))
        self._check_markup(method, method.reply_markup)
        markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
        if _dump(markup) == _dump(stored.reply_markup):
            raise self._bad(
                method,
                "Bad Request: message is not modified: specified new message content and reply markup "
                "are exactly the same as a current content and reply markup of the message",
            )
        stored.reply_markup = markup
        self._mark_edited(stored)
        call.message_ids.append(stored.message_id)
        return self.to_message(bot, stored)

    def _edit_caption(self, bot: Bot, method: m.EditMessageCaption, call: ApiCall) -> Message | bool:
        if method.inline_message_id:
            return True
        chat_id = _require_chat(call)
        stored = self._get_editable(method, chat_id, int(method.message_id or 0))
        if stored.kind == "text":
            raise self._bad(method, "Bad Request: there is no caption in the message to edit")
        caption, caption_entities = None, []
        if method.caption:
            caption, caption_entities = self._render(
                bot, method, method.caption, method.parse_mode, method.caption_entities, caption=True
            )
        self._check_markup(method, method.reply_markup)
        markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
        if caption == stored.caption and _dump(markup) == _dump(stored.reply_markup):
            raise self._bad(method, "Bad Request: message is not modified")
        stored.caption, stored.caption_entities, stored.html, stored.reply_markup = (
            caption,
            caption_entities,
            method.caption,
            markup,
        )
        self._mark_edited(stored)
        call.text = caption
        call.message_ids.append(stored.message_id)
        return self.to_message(bot, stored)

    def _mark_edited(self, stored: StoredMessage) -> None:
        stored.edits += 1
        stored.edit_date = int(_now().timestamp())
        self._touch(stored.chat_id, stored.message_id)

    def _copy(self, bot: Bot, method: m.CopyMessage | m.ForwardMessage, call: ApiCall) -> Message | MessageId:
        chat_id = _require_chat(call)
        key = (int(method.from_chat_id), method.message_id)
        source = self.messages.get(key) or self.incoming.get(key)
        if source is None or source.deleted:
            raise self._bad(method, "Bad Request: message to copy not found")
        caption, caption_entities, markup = source.caption, source.caption_entities, None
        if isinstance(method, m.CopyMessage):
            markup = method.reply_markup
            self._check_markup(method, markup)
            if method.caption is not None:
                caption, caption_entities = self._render(
                    bot, method, method.caption, method.parse_mode, method.caption_entities, caption=True
                )
        stored = self._store(
            chat_id,
            kind=source.kind,
            text=source.text,
            entities=list(source.entities),
            caption=caption,
            caption_entities=list(caption_entities),
            html=source.html,
            reply_markup=markup,
            media=dict(source.media),
            file=source.file,
        )
        if source.file is not None:
            sent = SentFile(
                chat_id,
                stored.message_id,
                source.kind,
                source.file.file_id,
                source.file.file_name,
                source.file.mime_type,
                source.file.content,
                caption,
            )
            self.sent_files.append(sent)
            call.files.append(sent)
        call.text = stored.content or None
        call.message_ids.append(stored.message_id)
        if isinstance(method, m.CopyMessage):
            return MessageId(message_id=stored.message_id)
        return self.to_message(bot, stored)

    def _delete(self, method: TelegramMethod[Any], chat_id: int, message_ids: list[int]) -> bool:
        found = False
        for message_id in message_ids:
            stored = self.messages.get((chat_id, message_id)) or self.incoming.get((chat_id, message_id))
            if stored is not None and not stored.deleted:
                stored.deleted = True
                found = True
        if not found:
            raise self._bad(method, "Bad Request: message to delete not found")
        return True

    def _answer_callback(self, method: m.AnswerCallbackQuery) -> bool:
        query_id = method.callback_query_id
        if query_id in self.callback_answers:
            raise self._bad(
                method,
                "Bad Request: query is too old and response timeout expired or query ID is invalid",
                bug=True,
            )
        if self.check_html and method.text and len(method.text) > CALLBACK_ANSWER_LIMIT:
            raise self._bad(method, "Bad Request: MESSAGE_TOO_LONG", bug=True)
        self.callback_answers[query_id] = method
        return True

    def _get_file(self, method: m.GetFile) -> File:
        info = self.file_info.get(method.file_id)
        if info is None:
            info = self.register_file("document", b"test", file_id=method.file_id)
        if len(info.content) > GET_FILE_LIMIT:
            raise self._bad(method, "Bad Request: file is too big")
        return File(
            file_id=info.file_id,
            file_unique_id=info.file_unique_id,
            file_size=len(info.content),
            file_path=info.file_path,
        )


def _chat_id_of(method: TelegramMethod[Any]) -> int | None:
    value = getattr(method, "chat_id", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _require_chat(call: ApiCall) -> int:
    if call.chat_id is None:
        raise NotImplementedError(f"FakeSession: {type(call.method).__name__} без числового chat_id не эмулируется")
    return call.chat_id


def _dump(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return [_dump(item) for item in value]
    return value.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------------------------
# Результат апдейта
# ---------------------------------------------------------------------------------------------


class RequestLog(list):  # list[TelegramMethod]
    """Запросы бота к API, сделанные во время обработки одного апдейта (или ``capture``).

    Это список объектов методов (``SendMessage``, ``AnswerCallbackQuery``…) + удобные выборки.
    Тексты — в том виде, как их увидит пользователь (HTML-теги разобраны).
    """

    def __init__(self, calls: Iterable[ApiCall] = (), *, handled: bool = True, result: Any = None) -> None:
        self.calls: list[ApiCall] = list(calls)
        super().__init__(call.method for call in self.calls)
        self.handled = handled
        self.result = result

    @property
    def texts(self) -> list[str]:
        """Тексты отправленных/отредактированных сообщений и подписи к файлам (успешные вызовы)."""
        return [call.text for call in self.calls if call.ok and call.text is not None]

    @property
    def text(self) -> str:
        """Все тексты одной строкой — удобно для ``assert "..." in log.text``."""
        return "\n".join(self.texts)

    def to(self, chat_id: int) -> RequestLog:
        """Только запросы в указанный чат."""
        return RequestLog(
            [call for call in self.calls if call.chat_id == chat_id], handled=self.handled, result=self.result
        )

    def of(self, *method_types: type) -> list[Any]:
        """Запросы указанных типов, например ``log.of(SendMessage, EditMessageText)``."""
        return [method for method in self if isinstance(method, method_types)]

    @property
    def answers(self) -> list[m.AnswerCallbackQuery]:
        return [call.method for call in self.calls if call.ok and isinstance(call.method, m.AnswerCallbackQuery)]

    @property
    def alert(self) -> str | None:
        """Текст первого ответа на callback с текстом (alert или всплывашка), иначе None."""
        return next((answer.text for answer in self.answers if answer.text), None)

    @property
    def documents(self) -> list[SentFile]:
        return [sent for call in self.calls if call.ok for sent in call.files if sent.kind == "document"]

    @property
    def files(self) -> list[SentFile]:
        return [sent for call in self.calls if call.ok for sent in call.files]

    @property
    def errors(self) -> list[TelegramAPIError]:
        return [call.error for call in self.calls if call.error is not None]

    @property
    def chats(self) -> set[int]:
        return {call.chat_id for call in self.calls if call.chat_id is not None and call.ok}


class HarnessError(AssertionError):
    """Строгий режим Harness обнаружил проблему (исключение в хендлере, битый запрос, …)."""


# ---------------------------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------------------------


class Harness:
    """Управляет «пользователями» бота и даёт запросы к тому, что бот им показал.

    :param dp: Dispatcher с роутерами (bot.main.build_dispatcher или свой).
    :param bot: Bot с ``session=FakeSession()``.
    :param expected_errors: исключения, которые хендлеры могут бросать штатно (DomainError).
    :param strict: строгий режим (см. модульную документацию).
    """

    def __init__(
        self,
        dp: Dispatcher,
        bot: Bot,
        *,
        expected_errors: tuple[type[BaseException], ...] = (),
        strict: bool = True,
    ) -> None:
        if not isinstance(bot.session, FakeSession):
            raise TypeError("Harness требует Bot(..., session=FakeSession())")
        self.dp = dp
        self.bot = bot
        self.api: FakeSession = bot.session
        self.expected_errors = tuple(expected_errors)
        self.strict = strict
        # Все исключения из хендлеров (в т.ч. «проглоченные» обработчиком ошибок dp.errors).
        self.errors: list[BaseException] = []
        self.last_log: RequestLog | None = None
        self._update_seq = itertools.count(1)
        self._query_seq = itertools.count(1)
        self._group_seq = itertools.count(1)
        dp.errors.outer_middleware(self._capture_error)

    async def _capture_error(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        exception = getattr(event, "exception", None)
        if exception is not None:
            self.errors.append(exception)
        return await handler(event, data)

    @contextlib.contextmanager
    def relaxed(self) -> Iterator[Harness]:
        """Временно выключить строгие проверки (для тестов, где ошибка — ожидаемый исход)."""
        previous, self.strict = self.strict, False
        try:
            yield self
        finally:
            self.strict = previous

    # --- Пользователи ------------------------------------------------------------------------

    def profile(
        self,
        user_id: int,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        username: str | None = None,
    ) -> TgUser:
        """Telegram-профиль пользователя (запоминается; переданные поля обновляют его)."""
        current = self.api.profiles.get(user_id)
        data: dict[str, Any] = (
            current.model_dump()
            if current is not None
            else {
                "id": user_id,
                "is_bot": False,
                "first_name": f"User{user_id}",
                "username": f"user{user_id}",
                "language_code": "ru",
            }
        )
        if first_name is not None:
            data["first_name"] = first_name
        if last_name is not None:
            data["last_name"] = last_name
        if username is not None:
            data["username"] = username
        user = TgUser(**data)
        self.api.profiles[user_id] = user
        return user

    def new_media_group_id(self) -> str:
        """Идентификатор альбома для send_photo/send_document(..., media_group_id=...)."""
        return f"in-album-{next(self._group_seq)}"

    # --- Входящие апдейты --------------------------------------------------------------------

    async def send_text(
        self,
        user_id: int,
        text: str,
        *,
        chat_id: int | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        username: str | None = None,
    ) -> RequestLog:
        """Пользователь пишет текст (или нажимает кнопку reply-клавиатуры — это тоже текст)."""
        extra: dict[str, Any] = {}
        if text.startswith("/"):
            command = text.split(maxsplit=1)[0]
            extra["entities"] = [MessageEntity(type="bot_command", offset=0, length=_u16(command))]
        return await self._send(
            user_id,
            chat_id,
            dict(first_name=first_name, last_name=last_name, username=username),
            kind="text",
            text=text,
            **extra,
        )

    async def send_command(
        self,
        user_id: int,
        command: str,
        args: str | None = None,
        *,
        chat_id: int | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        username: str | None = None,
    ) -> RequestLog:
        """``/command [args]``, например ``send_command(1001, "start")``."""
        text = "/" + command.lstrip("/") + (f" {args}" if args else "")
        return await self.send_text(
            user_id, text, chat_id=chat_id, first_name=first_name, last_name=last_name, username=username
        )

    async def press_menu(self, user_id: int, text: str, *, chat_id: int | None = None) -> RequestLog:
        """Нажать кнопку reply-клавиатуры (главного меню): проверяет, что она сейчас показана."""
        keyboard = self.reply_keyboard(chat_id if chat_id is not None else user_id) or []
        if text not in keyboard:
            raise LookupError(f"В меню чата нет кнопки {text!r}; сейчас меню: {keyboard or 'нет'}")
        return await self.send_text(user_id, text, chat_id=chat_id)

    async def send_document(
        self,
        user_id: int,
        file_name: str = "document.pdf",
        mime_type: str | None = None,
        content: bytes = b"test",
        *,
        caption: str | None = None,
        media_group_id: str | None = None,
        chat_id: int | None = None,
    ) -> RequestLog:
        """Пользователь присылает файл-документ. Его можно скачать ``bot.download(file_id)``."""
        info = self.api.register_file("document", content, file_name=file_name, mime_type=mime_type)
        return await self._send_file(user_id, chat_id, info, caption, media_group_id)

    async def send_photo(
        self,
        user_id: int,
        content: bytes = b"test",
        *,
        caption: str | None = None,
        media_group_id: str | None = None,
        chat_id: int | None = None,
    ) -> RequestLog:
        """Пользователь присылает фото (два размера, как в Telegram; крупный — последний)."""
        info = self.api.register_file("photo", content, mime_type="image/jpeg")
        return await self._send_file(user_id, chat_id, info, caption, media_group_id)

    async def send_video(
        self,
        user_id: int,
        content: bytes = b"test",
        *,
        file_name: str = "video.mp4",
        caption: str | None = None,
        media_group_id: str | None = None,
        chat_id: int | None = None,
    ) -> RequestLog:
        info = self.api.register_file("video", content, file_name=file_name, mime_type="video/mp4")
        return await self._send_file(user_id, chat_id, info, caption, media_group_id)

    async def send_sticker(self, user_id: int, *, chat_id: int | None = None) -> RequestLog:
        """Нетекстовый ввод — проверить, что в текстовом шаге диалога бот подсказывает."""
        info = self.api.register_file("sticker", b"sticker", mime_type="image/webp")
        return await self._send_file(user_id, chat_id, info, None, None)

    async def send_message(self, user_id: int, *, chat_id: int | None = None, **content: Any) -> RequestLog:
        """Произвольное сообщение: ``send_message(uid, location=Location(...))`` и т. п."""
        return await self._send(user_id, chat_id, {}, kind="other", **content)

    async def press(
        self,
        user_id: int,
        data: str | CallbackData,
        message_id: int | None = None,
        *,
        chat_id: int | None = None,
    ) -> RequestLog:
        """Нажатие inline-кнопки с callback_data ``data`` (строка или объект CallbackData).

        Сообщение callback'а: ``message_id``, если указан; иначе последнее сообщение бота в чате,
        где есть кнопка с такими данными; иначе последнее сообщение бота (так можно
        «подделать» callback). Удалённое сообщение приходит как ``InaccessibleMessage``.
        """
        data_str = data.pack() if isinstance(data, CallbackData) else str(data)
        chat_id = chat_id if chat_id is not None else user_id
        if message_id is not None:
            stored = self.api.messages.get((chat_id, message_id))
            if stored is None:
                raise LookupError(f"В чате {chat_id} нет сообщения бота #{message_id}")
        else:
            stored = next(
                (
                    msg
                    for msg in self.api.recent_messages(chat_id)
                    if any(button.callback_data == data_str for button in msg.buttons)
                ),
                None,
            ) or self.api.last_message(chat_id)

        message: Message | InaccessibleMessage | None
        if stored is None:
            message = None
        elif stored.deleted:
            message = InaccessibleMessage(chat=self.api.chat(chat_id), message_id=stored.message_id)
        else:
            message = self.api.to_message(self.bot, stored)
        query_id = f"cbq-{next(self._query_seq)}"
        self.api.callback_users[query_id] = user_id
        query = CallbackQuery(
            id=query_id,
            from_user=self.profile(user_id),
            chat_instance=f"ci-{chat_id}",
            message=message,
            data=data_str,
        )
        return await self._feed(
            {"callback_query": query},
            description=f"нажатие {data_str!r} от {user_id}",
            chat_id=chat_id,
            callback_id=query_id,
        )

    async def press_button(
        self,
        user_id: int,
        text: str,
        message_id: int | None = None,
        *,
        chat_id: int | None = None,
    ) -> RequestLog:
        """Найти inline-кнопку по подстроке текста (без учёта регистра) и нажать её.

        Ищет от последнего отправленного/изменённого сообщения бота к первому
        (или только в ``message_id``). Нет такой кнопки -> LookupError со списком кнопок.
        """
        chat_id = chat_id if chat_id is not None else user_id
        stored, button = self._locate_button(chat_id, text, message_id)
        if button.callback_data is None:
            raise LookupError(f"У кнопки {button.text!r} нет callback_data (url={button.url!r})")
        return await self.press(user_id, button.callback_data, stored.message_id, chat_id=chat_id)

    async def _send_file(
        self,
        user_id: int,
        chat_id: int | None,
        info: FileInfo,
        caption: str | None,
        media_group_id: str | None,
    ) -> RequestLog:
        fields: dict[str, Any] = dict(media_payload(info))
        if caption is not None:
            fields["caption"] = caption
        if media_group_id is not None:
            fields["media_group_id"] = media_group_id
        return await self._send(user_id, chat_id, {}, kind=info.kind, file=info, **fields)

    async def _send(
        self,
        user_id: int,
        chat_id: int | None,
        profile: dict[str, Any],
        *,
        kind: str,
        file: FileInfo | None = None,
        **content: Any,
    ) -> RequestLog:
        chat_id = chat_id if chat_id is not None else user_id
        tg_user = self.profile(user_id, **profile)
        message_id = self.api.next_message_id(chat_id)
        message = Message(
            message_id=message_id,
            date=_now(),
            chat=self.api.chat(chat_id),
            from_user=tg_user,
            **content,
        )
        media = {key: value for key, value in content.items() if key in _MEDIA_FIELDS}
        stored = StoredMessage(
            chat_id=chat_id,
            message_id=message_id,
            kind=kind,
            from_bot=False,
            text=message.text,
            caption=message.caption,
            html=message.text if message.text is not None else message.caption,
            media=media,
            media_group_id=message.media_group_id,
            file=file,
            from_user=tg_user,
        )
        stored.sent_text = stored.content
        self.api.add_incoming(stored)
        if message.text is not None:
            what = f"текст {message.text!r}"
        else:
            what = f"{kind}" + (f" {file.file_name!r}" if file and file.file_name else "")
        return await self._feed({"message": message}, description=f"{what} от {user_id}", chat_id=chat_id)

    async def _feed(
        self,
        payload: dict[str, Any],
        *,
        description: str,
        chat_id: int,
        callback_id: str | None = None,
    ) -> RequestLog:
        update = Update(update_id=next(self._update_seq), **payload)
        calls_before = len(self.api.calls)
        errors_before = len(self.errors)
        bugs_before = len(self.api.bug_errors)
        result = await self.dp.feed_update(self.bot, update)
        log = RequestLog(self.api.calls[calls_before:], handled=result is not UNHANDLED, result=result)
        self.last_log = log
        if self.strict:
            self._check(log, description, chat_id, callback_id, errors_before, bugs_before)
        return log

    def _check(
        self,
        log: RequestLog,
        description: str,
        chat_id: int,
        callback_id: str | None,
        errors_before: int,
        bugs_before: int,
    ) -> None:
        problems: list[str] = []
        cause: BaseException | None = None
        bug_errors = self.api.bug_errors[bugs_before:]
        for exc in bug_errors:
            problems.append(f"ошибочный запрос {type(exc.method).__name__}: {exc.message}")
            cause = cause or exc
        for exc in self.errors[errors_before:]:
            if any(exc is bug for bug in bug_errors) or isinstance(exc, self.expected_errors):
                continue
            problems.append(f"исключение в хендлере: {type(exc).__name__}: {exc}")
            cause = cause or exc
        if callback_id is not None:
            # Неудачная попытка ответа (например, слишком длинный текст) уже учтена выше как ошибка.
            attempted = any(
                isinstance(call.method, m.AnswerCallbackQuery) and call.method.callback_query_id == callback_id
                for call in log.calls
            )
            if not attempted:
                reason = (
                    "ни один хендлер не обработал callback"
                    if not log.handled
                    else "хендлер не вызвал callback.answer()"
                )
                problems.append(f"на нажатие не ответили callback.answer() — у пользователя «висят часики» ({reason})")
        if problems:
            lines = [f"Проблемы при обработке апдейта ({description}):"]
            lines += [f"  - {problem}" for problem in problems]
            lines.append(f"Переписка чата {chat_id} (последние сообщения):")
            lines.append(self.transcript(chat_id, limit=8))
            lines.append("Отключить проверки для ожидаемых ошибок: `with h.relaxed(): ...`")
            raise HarnessError("\n".join(lines)) from cause

    # --- Запросы к «экрану» ------------------------------------------------------------------

    @property
    def requests(self) -> list[TelegramMethod[Any]]:
        """Все запросы бота к API с начала теста."""
        return self.api.requests

    def clear(self) -> None:
        """Забыть записанные запросы (экран чатов, файлы и FSM не трогаются)."""
        self.api.requests.clear()
        self.api.calls.clear()

    async def capture(self, awaitable: Awaitable[Any]) -> RequestLog:
        """Выполнить корутину (например ``run_reminders(h.bot, sm)``) и вернуть её запросы к API.

        Результат корутины — в ``log.result``.
        """
        calls_before = len(self.api.calls)
        result = await awaitable
        return RequestLog(self.api.calls[calls_before:], result=result)

    def messages(self, chat_id: int) -> list[StoredMessage]:
        """Сообщения бота в чате в порядке отправки (без удалённых), в текущем виде."""
        return self.api.chat_messages(chat_id)

    def sent_to(self, chat_id: int) -> list[str]:
        """Тексты (или подписи) всех сообщений, отправленных ботом в чат, — как при отправке."""
        return [msg.sent_text or "" for msg in self.api.chat_messages(chat_id, include_deleted=True)]

    def outputs(self, chat_id: int) -> list[str]:
        """Всё, что пользователь видел появляющимся: тексты отправок и правок, по порядку."""
        return [
            call.text
            for call in self.api.calls
            if call.ok and call.chat_id == chat_id and call.text is not None
        ]

    def last_message(self, chat_id: int) -> StoredMessage | None:
        """Последнее отправленное ИЛИ изменённое сообщение бота в чате (удалённые пропускаются)."""
        return self.api.last_message(chat_id)

    def find_message(self, chat_id: int, text: str) -> StoredMessage:
        """Последнее (отправленное/изменённое) сообщение бота, текст которого содержит ``text``."""
        needle = text.casefold()
        for stored in self.api.recent_messages(chat_id):
            if needle in stored.content.casefold():
                return stored
        raise LookupError(f"В чате {chat_id} нет сообщения бота с текстом {text!r}")

    def last_text(self, chat_id: int) -> str | None:
        """Текущий текст (или подпись) последнего отправленного/изменённого сообщения бота."""
        stored = self.api.last_message(chat_id)
        return stored.content if stored is not None else None

    def last_html(self, chat_id: int) -> str | None:
        stored = self.api.last_message(chat_id)
        return stored.html if stored is not None else None

    def last_markup(self, chat_id: int) -> InlineKeyboardMarkup | None:
        stored = self.api.last_message(chat_id)
        return stored.reply_markup if stored is not None else None

    def buttons(self, chat_id: int, message_id: int | None = None) -> list[str]:
        """Тексты inline-кнопок последнего сообщения бота (или сообщения ``message_id``)."""
        if message_id is not None:
            stored = self.api.messages.get((chat_id, message_id))
        else:
            stored = self.api.last_message(chat_id)
        return stored.button_texts if stored is not None else []

    def find_button(self, chat_id: int, text: str, message_id: int | None = None) -> str:
        """callback_data кнопки, текст которой содержит ``text`` (без учёта регистра)."""
        _, button = self._locate_button(chat_id, text, message_id)
        if button.callback_data is None:
            raise LookupError(f"У кнопки {button.text!r} нет callback_data")
        return button.callback_data

    def has_button(self, chat_id: int, text: str, message_id: int | None = None) -> bool:
        try:
            self._locate_button(chat_id, text, message_id)
        except LookupError:
            return False
        return True

    def _locate_button(
        self, chat_id: int, text: str, message_id: int | None
    ) -> tuple[StoredMessage, InlineKeyboardButton]:
        needle = text.casefold()
        if message_id is not None:
            stored = self.api.messages.get((chat_id, message_id))
            candidates = [stored] if stored is not None and not stored.deleted else []
        else:
            candidates = list(self.api.recent_messages(chat_id))
        for stored in candidates:
            for button in stored.buttons:
                if needle in button.text.casefold():
                    return stored, button
        available = [f"#{msg.message_id}: {msg.button_texts}" for msg in candidates[:5] if msg.buttons]
        raise LookupError(
            f"В чате {chat_id} нет inline-кнопки, содержащей {text!r}. Кнопки последних сообщений: "
            + ("; ".join(available) or "нет")
        )

    def reply_keyboard(self, chat_id: int) -> list[str] | None:
        """Кнопки текущей reply-клавиатуры (главного меню) чата; None — клавиатуры нет."""
        markup = self.api.reply_keyboards.get(chat_id)
        if markup is None:
            return None
        return [button.text for row in markup.keyboard for button in row]

    def documents_sent(self, chat_id: int) -> list[SentFile]:
        """Документы, отправленные ботом в чат (с содержимым ``content``)."""
        return [sent for sent in self.api.sent_files if sent.chat_id == chat_id and sent.kind == "document"]

    def files_sent(self, chat_id: int) -> list[SentFile]:
        """Все файлы (документы, фото, видео…), отправленные ботом в чат."""
        return [sent for sent in self.api.sent_files if sent.chat_id == chat_id]

    @property
    def callback_answers(self) -> list[m.AnswerCallbackQuery]:
        return list(self.api.callback_answers.values())

    @property
    def alerts(self) -> list[m.AnswerCallbackQuery]:
        """Ответы на callback с текстом (alert при ``show_alert=True`` или всплывашка)."""
        return [answer for answer in self.api.callback_answers.values() if answer.text or answer.show_alert]

    def alerts_for(self, user_id: int) -> list[str]:
        """Тексты ответов на callback'и пользователя ``user_id``."""
        return [
            answer.text or ""
            for query_id, answer in self.api.callback_answers.items()
            if self.api.callback_users.get(query_id) == user_id and (answer.text or answer.show_alert)
        ]

    async def get_state(self, user_id: int, chat_id: int | None = None) -> str | None:
        """Текущее состояние FSM пользователя (``"CreateTaskSG:title"`` или None)."""
        context = self.dp.fsm.get_context(self.bot, chat_id=chat_id or user_id, user_id=user_id)
        return await context.get_state()

    async def get_data(self, user_id: int, chat_id: int | None = None) -> dict[str, Any]:
        context = self.dp.fsm.get_context(self.bot, chat_id=chat_id or user_id, user_id=user_id)
        return await context.get_data()

    def transcript(self, chat_id: int, limit: int | None = 30) -> str:
        """Переписка чата в текущем виде (для отладки: ``print(h.transcript(2001))``)."""
        items = [msg for (cid, _), msg in self.api.messages.items() if cid == chat_id]
        items += [msg for (cid, _), msg in self.api.incoming.items() if cid == chat_id]
        items.sort(key=lambda msg: msg.message_id)
        if limit is not None:
            items = items[-limit:]
        lines: list[str] = []
        for msg in items:
            who = "бот →" if msg.from_bot else "польз ←"
            body = msg.content.replace("\n", " ⏎ ")
            if msg.kind != "text":
                name = msg.file.file_name if msg.file and msg.file.file_name else ""
                body = f"[{msg.kind} {name}] {body}".rstrip()
            marks = []
            if msg.edits:
                marks.append("изм.")
            if msg.deleted:
                marks.append("удалено")
            mark = f" ({', '.join(marks)})" if marks else ""
            line = f"  #{msg.message_id} {who}{mark} {body}"
            if msg.buttons:
                line += f"  {msg.button_texts}"
            lines.append(line)
        keyboard = self.reply_keyboard(chat_id)
        if keyboard:
            lines.append(f"  [меню: {', '.join(keyboard)}]")
        return "\n".join(lines) or "  (пусто)"


class BotHarness(Harness):
    """Harness для нашего бота: + доступ к БД (sessionmaker из фикстуры ``app``)."""

    def __init__(
        self,
        dp: Dispatcher,
        bot: Bot,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        expected_errors: tuple[type[BaseException], ...] | None = None,
        strict: bool = True,
    ) -> None:
        if expected_errors is None:
            from bot.services.errors import DomainError

            expected_errors = (DomainError,)
        super().__init__(dp, bot, expected_errors=expected_errors, strict=strict)
        self.sessionmaker = sessionmaker

    def db(self) -> AsyncSession:
        """Новая сессия БД: ``async with h.db() as s: ...`` (всегда свежие данные)."""
        return self.sessionmaker()

    async def scalar(self, statement: Any) -> Any:
        async with self.db() as session:
            return await session.scalar(statement)

    async def scalars(self, statement: Any) -> list[Any]:
        async with self.db() as session:
            return list(await session.scalars(statement))

    async def get_user(self, tg_id: int) -> Any:
        """bot.db.models.User по Telegram ID (или None)."""
        from sqlalchemy import select

        from bot.db.models import User

        return await self.scalar(select(User).where(User.tg_id == tg_id))

    async def get_task(self, task_id: int) -> Any:
        """bot.db.models.Task по id (со связями: assignee, submissions, attachments…)."""
        from bot.db.models import Task

        async with self.db() as session:
            return await session.get(Task, task_id)

    async def seed_user(
        self,
        tg_id: int,
        full_name: str,
        *,
        role: str = "employee",
        status: str = "active",
        position: str | None = None,
        username: str | None = None,
    ) -> Any:
        """Создать пользователя прямо в БД (без диалога регистрации). Возвращает User."""
        from bot.db.models import Role, User, UserStatus

        async with self.db() as session:
            user = User(
                tg_id=tg_id,
                full_name=full_name,
                username=username,
                position=position,
                role=Role(role),
                status=UserStatus(status),
            )
            session.add(user)
            await session.commit()
        parts = full_name.split()
        first = parts[1] if len(parts) > 1 else full_name
        last = parts[0] if len(parts) > 1 else None
        self.profile(tg_id, first_name=first, last_name=last, username=username)
        return user
