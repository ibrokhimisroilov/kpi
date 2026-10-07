"""Сдача результата через API (multipart): файлы в чат сотрудника, лимиты, ошибки Telegram, гонки,
временные файлы, фоновая оценка общим конвейером (docs/MINIAPP_SPEC.md §8.6, §9, §12.2 test_submit_api)."""

from __future__ import annotations

import asyncio
import os
import types
from collections.abc import Iterator
from typing import Any

import pytest
from aiogram import methods as m
from aiogram.exceptions import TelegramBadRequest
from aiohttp import FormData
from sqlalchemy import func, select

from bot.db.models import AttachmentKind, Submission, TaskStatus
from bot.services import submission_flow
from bot.services import tasks as tasks_svc
from bot.ui.texts import BTN_SUBMIT
from bot.webapp import api

from .conftest import EMP, EMP2, MGR, MiniApp

PDF = b"%PDF-1.4 test report " * 10
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg" * 50

FileSpec = tuple[str, bytes, str | None]


def form(
    fact: str | None = "Проверено 110 договоров, в 12 выявлены нарушения",
    *,
    result: str | None = None,
    value: str | None = None,
    materials: str | None = None,
    files: list[FileSpec] = (),  # type: ignore[assignment]
    extra: list[tuple[str, str]] = (),  # type: ignore[assignment]
) -> FormData:
    """Тело как у браузера: текстовые поля, затем files (имена в UTF-8, без %-кодирования)."""
    data = FormData(quote_fields=False, default_to_multipart=True)
    for name, text in (("fact_text", fact), ("result_text", result), ("fact_value", value), ("materials_text", materials)):
        if text is not None:
            data.add_field(name, text)
    for name, text in extra:
        data.add_field(name, text)
    for filename, content, content_type in files:
        data.add_field("files", content, filename=filename, content_type=content_type or "application/octet-stream")
    return data


@pytest.fixture
def temp_files(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Имена временных файлов, которые создал API (после запроса их не должно остаться)."""
    import tempfile

    created: list[str] = []

    def tracking(*args: Any, **kwargs: Any) -> Any:
        handle = tempfile.NamedTemporaryFile(*args, **kwargs)  # noqa: SIM115
        created.append(handle.name)
        return handle

    monkeypatch.setattr(api, "tempfile", types.SimpleNamespace(NamedTemporaryFile=tracking))
    yield created
    assert not [path for path in created if os.path.exists(path)], "временные файлы не удалены"


async def setup_task(ma: MiniApp, **kwargs: Any) -> tuple[Any, Any, int]:
    mgr, (emp, _) = await ma.seed_team(2)
    task_id = await ma.seed_task(emp, mgr, **kwargs)
    return mgr, emp, task_id


async def submissions_count(ma: MiniApp) -> int:
    return int(await ma.h.scalar(select(func.count(Submission.id))))


async def test_submit_with_photo_and_document(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch,
                                              temp_files: list[str]) -> None:
    _, emp, task_id = await setup_task(ma)
    release = asyncio.Event()
    original = submission_flow.run_after_submit

    async def delayed(*args: Any, **kwargs: Any) -> Any:
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(submission_flow, "run_after_submit", delayed)
    body = form(result="Подготовлен отчёт", value="110", files=[
        ("Фото склада.jpg", JPEG, "image/jpeg"),
        ("C:\\Users\\Иван\\Отчёт по договорам.pdf", PDF, "application/pdf"),
    ])
    resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=body)
    assert resp.status == 202, resp.data
    assert resp.data == {"task_id": task_id, "submission_id": resp["submission_id"], "attempt": 1, "files": 2,
                         "status": "submitted", "evaluation": "pending"}
    assert len(temp_files) == 2

    # 202 — до окончания оценки: руководителю ещё ничего не пришло.
    assert not any("Результат по задаче" in text for text in ma.h.sent_to(MGR))
    uploads = [r for r in ma.api.requests if isinstance(r, m.SendPhoto | m.SendDocument) and r.chat_id == EMP]
    assert sorted(type(r).__name__ for r in uploads) == ["SendDocument", "SendPhoto"]
    for request in uploads:
        assert request.caption == f"📎 К задаче #{task_id}" and request.disable_notification is True
    assert next(r for r in uploads if isinstance(r, m.SendDocument)).disable_content_type_detection is True

    task = await ma.task(task_id)
    assert task.status == TaskStatus.SUBMITTED
    sub = task.submissions[-1]
    assert sub.fact_value == 110 and sub.result_text == "Подготовлен отчёт"
    kinds = {att.kind: att for att in sub.attachments}
    photo, doc = kinds[AttachmentKind.PHOTO], kinds[AttachmentKind.DOCUMENT]
    sent = {f.kind: f for f in ma.h.files_sent(EMP)}
    assert photo.file_id == sent["photo"].file_id and photo.file_name == "Фото склада.jpg"
    assert photo.mime_type == "image/jpeg"
    assert doc.file_id == sent["document"].file_id and doc.file_name == "Отчёт по договорам.pdf"
    assert doc.mime_type == "application/pdf" and doc.file_size == len(PDF)
    assert sent["document"].content == PDF and sent["document"].file_name == "Отчёт по договорам.pdf"

    release.set()
    await ma.drain()
    review = ma.h.find_message(MGR, f"Результат по задаче #{task_id}")
    assert "📐 Расчёт по правилам (AI недоступен): 110 %" in review.content
    assert "✅ Подтвердить 110 %" in review.button_texts and "📎 Файлы (2)" in review.button_texts
    assert {f.file_id for f in ma.h.files_sent(MGR)} == {photo.file_id, doc.file_id}
    sub = (await ma.task(task_id)).submissions[-1]
    assert sub.ai_source == "rules" and sub.ai_rationale.startswith(submission_flow.RULES_PREFIX)
    # Сотруднику отдельного сообщения нет — его файлы уже в чате с подписью «📎 К задаче #N».
    assert [f.caption for f in ma.h.files_sent(EMP)] == [f"📎 К задаче #{task_id}"] * 2


async def test_materials_and_fact_value_like_chat(ma: MiniApp, frozen: Any) -> None:
    _, _, task_id = await setup_task(ma)
    resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form(
        result="Отчёт готов", value="1 200", materials="ссылка на папку: disk/abc"))
    assert resp.status == 202 and resp["files"] == 0
    await ma.drain()
    sub = (await ma.task(task_id)).submissions[-1]
    assert sub.fact_value == 1200
    assert sub.result_text == submission_flow.result_with_notes("Отчёт готов", ["ссылка на папку: disk/abc"])
    assert sub.result_text == "Отчёт готов\n\nПодтверждающие материалы: ссылка на папку: disk/abc"
    only_materials = await setup_second_task(ma)
    resp = await ma.post(f"/api/tasks/{only_materials}/submit", as_=EMP, data=form(materials="в почте"))
    assert resp.status == 202
    assert (await ma.task(only_materials)).submissions[-1].result_text == "Подтверждающие материалы: в почте"


async def setup_second_task(ma: MiniApp) -> int:
    mgr = await ma.h.get_user(MGR)
    emp = await ma.h.get_user(EMP)
    return await ma.seed_task(emp, mgr, title="Вторая задача")


@pytest.mark.parametrize(
    ("patch", "body_kwargs", "status", "message"),
    [
        ({"MAX_FILES": 2}, {"files": [("a.txt", b"1", None)] * 3}, 413, "Можно приложить не более 2 файлов."),
        ({"MAX_FILE_BYTES": 10}, {"files": [("большой.txt", b"x" * 11, "text/plain")]}, 413, "Файл «большой.txt» больше"),
        ({"MAX_TOTAL_BYTES": 15}, {"files": [("a.txt", b"x" * 10, None), ("b.txt", b"y" * 10, None)]}, 413,
         "Все файлы вместе — не больше"),
        ({}, {"files": [("пусто.txt", b"", "text/plain")]}, 400, "Пустой файл «пусто.txt»"),
        ({}, {"extra": [("hacker", "1")]}, 400, "Неизвестное поле «hacker»"),
        ({}, {"extra": [("fact_text", "второй раз")]}, 400, "указано дважды"),
        ({}, {"fact": None}, 400, api.FACT_TOO_SHORT),
        ({}, {"fact": " да "}, 400, api.FACT_TOO_SHORT),
        ({}, {"fact": "я" * 3001}, 400, "Слишком длинно: 3001 символов"),
        ({}, {"result": "я" * 3001}, 400, "Слишком длинно"),
        ({}, {"materials": "я" * 1501}, 400, "до 1500"),
        ({}, {"value": "много"}, 400, "«Фактическое значение»: ожидается число"),
        ({}, {"value": "-5"}, 400, "Фактическое значение не может быть отрицательным"),
        ({"MAX_TEXT_PART_BYTES": 100}, {"fact": "я" * 60}, 400, "слишком большое"),
    ],
    ids=["11-файлов", "файл-больше-лимита", "сумма-больше-лимита", "пустой-файл", "неизвестное-поле", "повтор-поля",
         "нет-факта", "короткий-факт", "длинный-факт", "длинный-результат", "длинные-материалы", "не-число",
         "отрицательное", "большое-текстовое-поле"],
)
async def test_limits_and_validation(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch, temp_files: list[str],
                                     patch: dict[str, int], body_kwargs: dict[str, Any], status: int, message: str) -> None:
    _, _, task_id = await setup_task(ma)
    for name, value in patch.items():
        monkeypatch.setattr(api, name, value)
    before_calls = len(ma.api.requests)
    resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form(**body_kwargs))
    assert resp.status == status, resp.data
    assert message in resp.error
    assert resp.code == ("too_large" if status == 413 else "bad_request")
    assert await submissions_count(ma) == 0
    assert not [r for r in ma.api.requests[before_calls:] if isinstance(r, m.SendPhoto | m.SendDocument)]
    assert not ma.ctx.gate.busy("submit", EMP)


async def test_not_multipart_and_wrong_task_states(ma: MiniApp, frozen: Any) -> None:
    mgr, emp, task_id = await setup_task(ma)
    resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, json={"fact_text": "Сделано всё"})
    assert resp.status == 400 and resp.error == api.NOT_MULTIPART
    resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP2, data=form())
    assert resp.status == 403 and resp.code == "forbidden"
    submitted = await ma.seed_task(emp, mgr, kind="submitted", title="Уже сдана")
    resp = await ma.post(f"/api/tasks/{submitted}/submit", as_=EMP, data=form())
    assert resp.status == 400 and resp.code == "domain"
    assert resp.error == "📝 Результат уже отправлен и ждёт проверки руководителя."
    cancelled = await ma.seed_task(emp, mgr, kind="cancelled", title="Отменена")
    resp = await ma.post(f"/api/tasks/{cancelled}/submit", as_=EMP, data=form())
    assert resp.error == "🚫 Задача отменена руководителем — сдавать результат не нужно."
    assert await submissions_count(ma) == 1  # только засеянная сдача


async def test_telegram_forbidden_gives_502_and_no_submission(ma: MiniApp, frozen: Any, temp_files: list[str]) -> None:
    _, _, task_id = await setup_task(ma)
    ma.api.blocked_chats.add(EMP)
    resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form(files=[("a.pdf", PDF, "application/pdf")]))
    assert resp.status == 502 and resp.code == "telegram_error" and resp.error == api.TG_BLOCKED
    assert await submissions_count(ma) == 0
    assert (await ma.task(task_id)).status == TaskStatus.ACTIVE


async def test_telegram_rejects_file(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch, temp_files: list[str]) -> None:
    _, _, task_id = await setup_task(ma)

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise TelegramBadRequest(method=m.SendDocument(chat_id=1, document="x"), message="Bad Request: file is too big")

    monkeypatch.setattr(ma.h.bot, "send_document", broken)
    resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form(files=[("Акт.docx", PDF, None)]))
    assert resp.status == 502 and resp.error == api.TG_FILE_FAILED.format(name="Акт.docx")
    assert await submissions_count(ma) == 0


async def test_photo_rejected_as_photo_goes_as_document(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, task_id = await setup_task(ma)

    async def bad_photo(*args: Any, **kwargs: Any) -> Any:
        raise TelegramBadRequest(method=m.SendPhoto(chat_id=1, photo="x"), message="Bad Request: PHOTO_INVALID_DIMENSIONS")

    monkeypatch.setattr(ma.h.bot, "send_photo", bad_photo)
    resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form(files=[("панорама.png", JPEG, "image/png")]))
    assert resp.status == 202, resp.data
    att = (await ma.task(task_id)).submissions[-1].attachments[0]
    assert att.kind == AttachmentKind.DOCUMENT and att.file_name == "панорама.png"


async def test_big_photo_goes_as_document(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, task_id = await setup_task(ma)
    monkeypatch.setattr(api, "PHOTO_MAX_BYTES", 10)
    resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form(files=[("большое.jpg", JPEG, "image/jpeg")]))
    assert resp.status == 202
    assert (await ma.task(task_id)).submissions[-1].attachments[0].kind == AttachmentKind.DOCUMENT


async def test_task_cancelled_while_uploading(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch,
                                              temp_files: list[str]) -> None:
    _, _, task_id = await setup_task(ma)
    original = api._upload_files

    async def cancel_meanwhile(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        async with ma.db() as session:
            manager = await ma.h.get_user(MGR)
            await tasks_svc.cancel_task(session, task_id, await session.get(type(manager), manager.id), "Не нужно")
            await session.commit()
        return result

    monkeypatch.setattr(api, "_upload_files", cancel_meanwhile)
    resp = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form(files=[("a.pdf", PDF, None)]))
    assert resp.status == 400 and resp.code == "domain"
    assert resp.error == "🚫 Задача отменена руководителем — сдавать результат не нужно."
    assert await submissions_count(ma) == 0
    assert len(ma.h.files_sent(EMP)) == 1  # уже загруженный файл остался в чате
    assert not ma.ctx.gate.busy("submit", EMP)


async def test_second_concurrent_submit_is_busy(ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch,
                                                temp_files: list[str]) -> None:
    _, _, task_id = await setup_task(ma)
    release = asyncio.Event()
    original = api._upload_one

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(api, "_upload_one", slow)
    first = asyncio.create_task(ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form(files=[("a.pdf", PDF, None)])))
    for _ in range(200):
        if ma.ctx.gate.busy("submit", EMP):
            break
        await asyncio.sleep(0.01)
    second = await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form())
    assert second.status == 429 and second.code == "busy" and second.error == "⏳ Результат уже отправляется…"
    release.set()
    resp = await first
    assert resp.status == 202
    assert await submissions_count(ma) == 1


async def test_employee_does_not_see_ai_after_submit(ma: MiniApp, frozen: Any) -> None:
    _, _, task_id = await setup_task(ma)
    assert (await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form(value="110"))).status == 202
    await ma.drain()
    card = await ma.get(f"/api/tasks/{task_id}", as_=EMP)
    assert card["submissions"][-1]["ai"] is None and card["submissions"][-1]["ai_hidden"] is True
    assert card["task"]["ai_score"] is None and card["task"]["status"] == "submitted"
    assert card["task"]["actions"]["submit"] is False


async def test_chat_dialog_started_before_api_submit_is_refused(ma: MiniApp, frozen: Any) -> None:
    _, _, task_id = await setup_task(ma)
    h = ma.h
    await h.send_command(EMP, "start")
    await h.press_menu(EMP, BTN_SUBMIT)
    await h.press_button(EMP, "Анализ договоров")
    await h.send_text(EMP, "Проверено 110 договоров")
    await h.send_text(EMP, "Отчёт готов")
    if "Фактическое значение" in (h.last_text(EMP) or ""):
        await h.send_text(EMP, "110")
    await h.press_button(EMP, "Без файлов")
    assert "Проверьте перед отправкой" in (h.last_text(EMP) or "")

    assert (await ma.post(f"/api/tasks/{task_id}/submit", as_=EMP, data=form())).status == 202
    await ma.drain()
    log = await h.press_button(EMP, "Отправить")
    assert "Результат уже отправлен" in (log.alert or "")
    assert await submissions_count(ma) == 1


def test_safe_file_name() -> None:
    assert api._safe_file_name("C:\\Users\\Иван\\Отчёт.pdf") == "Отчёт.pdf"
    assert api._safe_file_name("a/b/../c.txt") == "c.txt"
    assert api._safe_file_name("") == "file" and api._safe_file_name(None) == "file"
    assert api._safe_file_name("..") == "file"
    assert api._safe_file_name("\u202eevil\x07  name\tfile.pdf") == "evil name file.pdf"
    long_name = api._safe_file_name("я" * 300 + ".pdf")
    assert len(long_name) == 255 and long_name.endswith(".pdf")


# --- Тело сдачи: части формы и сроки чтения ------------------------------------------------------------

BOUNDARY = "kpi-test-boundary"


def raw_multipart(parts: list[tuple[str, str | None, bytes]]) -> tuple[bytes, dict[str, str]]:
    """Тело multipart «как есть» (name, filename или None, содержимое) и заголовок Content-Type."""
    body = b""
    for name, filename, content in parts:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename is not None else "")
        body += f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n\r\n".encode() + content + b"\r\n"
    body += f"--{BOUNDARY}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


FACT = ("fact_text", None, "Проверено 110 договоров".encode())


async def test_empty_file_inputs_do_not_touch_disk(ma: MiniApp, frozen: Any, temp_files: list[str]) -> None:
    """Пустые поля выбора файла (filename="", без содержимого) — не файлы: временные файлы не создаются."""
    _, _, task_id = await setup_task(ma)
    body, headers = raw_multipart([FACT, *[("files", "", b"")] * 3])
    resp = await ma.request("POST", f"/api/tasks/{task_id}/submit", as_=EMP, data=body, headers=headers)
    assert resp.status == 202, resp.data
    assert resp["files"] == 0
    assert temp_files == []


async def test_form_parts_are_capped(ma: MiniApp, frozen: Any, temp_files: list[str]) -> None:
    """Частей формы не больше MAX_FILES + текстовые поля + запас: бесконечный поток пустых частей — 413."""
    _, _, task_id = await setup_task(ma)
    body, headers = raw_multipart([FACT, *[("files", "", b"")] * (api.MAX_FILES + len(api._TEXT_PARTS) + api.EXTRA_PARTS)])
    resp = await ma.request("POST", f"/api/tasks/{task_id}/submit", as_=EMP, data=body, headers=headers)
    assert resp.status == 413 and resp.code == "too_large" and resp.error == api.TOO_MANY_PARTS
    assert await submissions_count(ma) == 0
    assert not ma.ctx.gate.busy("submit", EMP)


async def _stalled_upload(ma: MiniApp, task_id: int, *, trickle: float | None) -> tuple[int, bytes]:
    """Начать сдачу с файлом и не досылать тело (trickle — досылать по байту с этим интервалом)."""
    server = ma.client.server
    reader, writer = await asyncio.open_connection(server.host, server.port)
    start = (
        f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.pdf\"\r\n"
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + PDF * 2000  # больше одного куска чтения: временный файл успевает появиться
    writer.write(
        (
            f"POST /api/tasks/{task_id}/submit HTTP/1.1\r\nHost: {server.host}:{server.port}\r\n"
            f"X-Telegram-Init-Data: {ma.init_data(EMP)}\r\n"
            f"Content-Type: multipart/form-data; boundary={BOUNDARY}\r\nContent-Length: 10000000\r\n\r\n"
        ).encode()
        + start
    )
    await writer.drain()

    async def drip() -> None:
        while trickle is not None:
            await asyncio.sleep(trickle)
            writer.write(b"x")
            await writer.drain()

    dripping = asyncio.create_task(drip())
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        lines = head.decode("latin-1").split("\r\n")
        length = next(int(line.split(":", 1)[1]) for line in lines if line.lower().startswith("content-length:"))
        return int(lines[0].split()[1]), await asyncio.wait_for(reader.readexactly(length), timeout=10)
    finally:
        dripping.cancel()
        writer.close()


@pytest.mark.parametrize("trickle", [None, 0.05])
async def test_stalled_or_endless_upload_times_out(
    ma: MiniApp, frozen: Any, monkeypatch: pytest.MonkeyPatch, temp_files: list[str], trickle: float | None
) -> None:
    """Связь замолчала (UPLOAD_IDLE_TIMEOUT_SEC) или тело тянется дольше UPLOAD_MAX_SEC: 408, временные
    файлы удалены, «уже отправляется» снято, сдачи нет."""
    _, _, task_id = await setup_task(ma)
    monkeypatch.setattr(api, "UPLOAD_IDLE_TIMEOUT_SEC", 0.3 if trickle is None else 10.0)
    monkeypatch.setattr(api, "UPLOAD_MAX_SEC", 60.0 if trickle is None else 0.6)
    status, body = await _stalled_upload(ma, task_id, trickle=trickle)
    assert status == 408
    assert b'"code":"request_timeout"' in body and api.UPLOAD_TIMEOUT.encode() in body
    assert len(temp_files) == 1  # файл начал писаться — и удалён (проверка фикстуры temp_files)
    assert await submissions_count(ma) == 0
    assert not ma.ctx.gate.busy("submit", EMP)
    assert (await ma.task(task_id)).status == TaskStatus.ACTIVE
