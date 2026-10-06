"""Готовит файл deploy/render.env — настройки бота для Render (docs/DEPLOY_RENDER.md, шаги 2–3).

Запуск из папки бота:

    .venv\\Scripts\\python deploy\\make_render_env.py
    .venv\\Scripts\\python deploy\\make_render_env.py --database-url - --database-password -

Скрипт берёт из локального файла .env токен бота, ID руководителей и ключ Gemini и пишет в
deploy/render.env ровно те строки, которые руководитель вставляет в Render (поля Blueprint или
«Add from .env»):

    BOT_TOKEN=...
    ADMIN_IDS=...
    GEMINI_API_KEY=...
    RUN_MODE=webhook
    DATABASE_URL=...        строка Supabase «Session pooler» (или заглушка — её заменят)
    DATABASE_PASSWORD=...   пароль базы Supabase (или заглушка — его заменят)
    GROQ_API_KEY=...        ключи запасных бесплатных AI-провайдеров — только те, что заданы в .env
    CLOUDFLARE_ACCOUNT_ID=..., CLOUDFLARE_API_TOKEN=..., MISTRAL_API_KEY=..., OPENROUTER_API_KEY=...

Значения секретов на экран НЕ выводятся — только названия настроек. Читается только файл .env
(переменные окружения не учитываются). Сам .env не меняется. deploy/render.env внесён в .gitignore
и не попадает в образ Docker (.dockerignore исключает папку deploy).

Если deploy/render.env уже есть и в нём вписаны DATABASE_URL / DATABASE_PASSWORD, а в командной
строке они не заданы, — прежние значения сохраняются (руководитель мог вписать их сам в Блокноте).

Только стандартная библиотека Python (python-dotenv используется, если установлен, — как у бота).
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = ROOT / ".env"
DEFAULT_OUT = ROOT / "deploy" / "render.env"

URL_PLACEHOLDER = "ВСТАВЬТЕ_СЮДА_СТРОКУ_SESSION_POOLER_ИЗ_SUPABASE"
PASSWORD_PLACEHOLDER = "ВСТАВЬТЕ_СЮДА_ПАРОЛЬ_БАЗЫ_SUPABASE"
SUPABASE_PASSWORD_MARK = "[YOUR-PASSWORD]"

# Порядок строк в deploy/render.env.
OUTPUT_KEYS = ("BOT_TOKEN", "ADMIN_IDS", "GEMINI_API_KEY", "RUN_MODE", "DATABASE_URL", "DATABASE_PASSWORD")
# Ключи запасных бесплатных AI-провайдеров (bot/config.py): необязательные — в файл попадают (в конец)
# только заданные в .env. В render.yaml они тоже есть (sync: false), их поля можно оставить пустыми.
OPTIONAL_AI_KEYS = (
    "GROQ_API_KEY",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "MISTRAL_API_KEY",
    "OPENROUTER_API_KEY",
)
# Значения, которые уже заданы в render.yaml: переносить их нужно, только если в .env они другие.
RENDER_YAML_VALUES = {"RUN_MODE": "webhook", "TIMEZONE": "Asia/Tashkent", "AI_PROVIDER": "auto"}
# Значения из .env, равносильные значениям render.yaml (AI_PROVIDER=gemini — прежнее название «auto»).
_SAME_AS_RENDER_YAML = {"AI_PROVIDER": frozenset({"auto", "gemini"})}
# Настройки .env, которые на Render не нужны или задаются иначе (о них не напоминаем).
_NOT_FOR_RENDER = frozenset(
    {"DATABASE_URL", "DATABASE_PASSWORD", "RUN_MODE", "PUBLIC_URL", "PORT", "RENDER_EXTERNAL_URL", "TAKEOVER_WEBHOOK"}
)

_TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]+")
_LINE_RE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")
_QUOTED_RE = re.compile(r"""(["'])(.*?)\1(?:\s+#.*)?""", re.DOTALL)
_TRANSACTION_POOLER_PORT = "6543"
# Символы, с которыми вставка «KEY=значение» в чужой разборщик .env может прочитаться иначе.
_RISKY_CHARS = ("#", '"', "'", "\\", "$", "`")


class SetupError(Exception):
    """Ошибка, после которой файл не создаётся (текст — по-русски для разработчика)."""


@dataclass
class Report:
    """Что вывести на экран: только названия настроек и пояснения, без значений."""

    status: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)


# --- Чтение .env ---------------------------------------------------------------------------------


def parse_env_text(text: str) -> dict[str, str]:
    """Простой разбор .env (как python-dotenv для обычных строк): KEY=value, «export», кавычки,
    комментарии «#» в начале строки и « #» после значения без кавычек. Ключи — в верхнем регистре."""
    values: dict[str, str] = {}
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.fullmatch(line)
        if match is None:
            continue
        key, value = match.group(1).upper(), match.group(2).strip()
        quoted = _QUOTED_RE.fullmatch(value)
        if quoted is not None:
            value = quoted.group(2)
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def read_env_file(path: Path) -> dict[str, str]:
    """Настройки из файла .env. Как у бота (pydantic-settings → python-dotenv), если он установлен."""
    if not path.is_file():
        raise SetupError(
            f"Не найден файл настроек {path}. Запустите скрипт из папки бота или укажите путь: --env ПУТЬ_К_.env"
        )
    try:
        from dotenv import dotenv_values
    except ImportError:
        return parse_env_text(path.read_text(encoding="utf-8-sig"))
    values: dict[str, str] = {}
    for key, value in dotenv_values(path, encoding="utf-8").items():
        values[key.lstrip("﻿").strip().upper()] = (value or "").strip()
    return values


# --- Проверки --------------------------------------------------------------------------------------


def _check_single_line(key: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise SetupError(f"{key}: значение занимает несколько строк — так его нельзя вставить в Render.")


def _check_token(values: dict[str, str]) -> str:
    token = values.get("BOT_TOKEN", "").strip()
    if not token:
        raise SetupError("В .env нет BOT_TOKEN (или он пустой). Впишите токен бота от @BotFather и запустите снова.")
    if not _TOKEN_RE.fullmatch(token):
        raise SetupError(
            "BOT_TOKEN в .env записан неверно: токен выглядит как 1234567890:AAH...xyz, без пробелов и кавычек."
        )
    return token


def _check_admin_ids(values: dict[str, str], report: Report) -> str:
    parts = [part.strip() for part in values.get("ADMIN_IDS", "").split(",") if part.strip()]
    if any(not re.fullmatch(r"-?\d+", part) for part in parts):
        raise SetupError("ADMIN_IDS в .env записан неверно: нужны числа через запятую, например 123456789,987654321.")
    if not parts:
        report.warnings.append(
            "ADMIN_IDS пустой: на Render никто не станет руководителем автоматически. "
            "Впишите Telegram ID руководителей в .env и запустите скрипт снова."
        )
        report.status["ADMIN_IDS"] = "ПУСТО — см. предупреждение"
    else:
        report.status["ADMIN_IDS"] = f"из .env ({len(parts)} ID)"
    return ",".join(parts)


def _check_gemini(values: dict[str, str], report: Report) -> str:
    key = values.get("GEMINI_API_KEY", "").strip()
    if key:
        report.status["GEMINI_API_KEY"] = "из .env"
    elif _optional_ai_keys(values):
        report.status["GEMINI_API_KEY"] = "пусто — AI на Render будет работать через запасных провайдеров"
    else:
        report.status["GEMINI_API_KEY"] = "пусто — бот на Render будет работать без AI (по правилам)"
    return key


def _optional_ai_keys(values: dict[str, str]) -> dict[str, str]:
    """Заданные в .env ключи запасных AI-провайдеров (в порядке OPTIONAL_AI_KEYS)."""
    found = {key: values.get(key, "").strip() for key in OPTIONAL_AI_KEYS}
    return {key: value for key, value in found.items() if value}


def _url_parts(url: str) -> tuple[str, str, str]:
    """(пароль, хост, порт) из адреса postgresql://user:pass@host:port/db — без разбора URL-библиотекой
    (в строке Supabase бывает «[YOUR-PASSWORD]» с квадратными скобками)."""
    rest = url.split("://", 1)[1]
    userinfo, _, hostinfo = rest.rpartition("@")
    password = userinfo.partition(":")[2]
    hostport = re.split(r"[/?#]", hostinfo, maxsplit=1)[0]
    host, sep, port = hostport.rpartition(":")
    if not sep or not port.isdigit():
        host, port = hostport, ""
    return password, host.lower(), port


def _check_database_url(url: str, report: Report) -> bool:
    """Проверить строку Supabase. -> True, если пароль в строке уже есть (DATABASE_PASSWORD не нужен)."""
    _check_single_line("DATABASE_URL", url)
    if not re.match(r"postgres(?:ql)?(?:\+asyncpg)?://", url, re.IGNORECASE):
        raise SetupError(
            "DATABASE_URL должен начинаться с postgresql:// — это строка «Session pooler» из Supabase → Connect. "
            "Файл SQLite (data/bot.db) на Render не годится: там файлы стираются при каждом перезапуске."
        )
    password, host, port = _url_parts(url)
    if host.startswith("db.") and host.endswith(".supabase.co"):
        report.warnings.append(
            "DATABASE_URL — это Direct connection (db.….supabase.co): с Render она не работает. "
            "Возьмите в Supabase → Connect строку «Session pooler» (…pooler.supabase.com:5432)."
        )
    elif port == _TRANSACTION_POOLER_PORT:
        report.warnings.append(
            "DATABASE_URL — Transaction pooler (порт 6543). Рекомендуется Session pooler (порт 5432) из Supabase → Connect."
        )
    has_password = bool(password) and password != SUPABASE_PASSWORD_MARK
    if has_password:
        report.status["DATABASE_URL"] = "задана (пароль уже в строке)"
    else:
        report.status["DATABASE_URL"] = "задана (пароль подставит бот из DATABASE_PASSWORD)"
    return has_password


def _warn_risky(key: str, value: str, report: Report) -> None:
    if value != value.strip() or any(char in value for char in _RISKY_CHARS):
        report.warnings.append(
            f"{key}: в значении есть пробелы по краям или знаки {' '.join(_RISKY_CHARS)}. "
            "После вставки в Render откройте Environment и сверьте это поле (при необходимости впишите вручную)."
        )


# --- Сборка файла -----------------------------------------------------------------------------------


def _is_filled(value: str | None) -> bool:
    return bool(value) and value not in (URL_PLACEHOLDER, PASSWORD_PLACEHOLDER)


def _other_settings(values: dict[str, str]) -> list[str]:
    """Названия прочих настроек из .env, которые не попадут на Render (их можно добавить в Environment)."""
    names = []
    for key, value in values.items():
        if not value or key in OUTPUT_KEYS or key in OPTIONAL_AI_KEYS or key in _NOT_FOR_RENDER:
            continue
        same = _SAME_AS_RENDER_YAML.get(key, frozenset({RENDER_YAML_VALUES.get(key, "").lower()}))
        if key in RENDER_YAML_VALUES and value.strip().lower() in same:
            continue
        names.append(key)
    return sorted(names)


def build_lines(
    values: dict[str, str],
    *,
    database_url: str | None = None,
    database_password: str | None = None,
    previous: dict[str, str] | None = None,
    report: Report | None = None,
) -> list[str]:
    """Строки deploy/render.env по настройкам .env и значениям базы. Ошибка — SetupError."""
    report = report if report is not None else Report()
    previous = previous or {}
    token = _check_token(values)
    report.status["BOT_TOKEN"] = "из .env"
    admin_ids = _check_admin_ids(values, report)
    gemini = _check_gemini(values, report)
    _check_single_line("GEMINI_API_KEY", gemini)
    report.status["RUN_MODE"] = "webhook"

    url = (database_url or "").strip().strip("\"'")
    from_previous_url = False
    if not url and _is_filled(previous.get("DATABASE_URL")):
        url, from_previous_url = previous["DATABASE_URL"], True
    url_has_password = False
    if url:
        url_has_password = _check_database_url(url, report)
        if from_previous_url:
            report.status["DATABASE_URL"] += ", из прежнего render.env"
    else:
        url = URL_PLACEHOLDER
        report.status["DATABASE_URL"] = "НУЖНО ВПИСАТЬ — строка Session pooler из Supabase"
        report.todo.append("DATABASE_URL")

    password = database_password.rstrip("\r\n") if database_password else ""
    from_previous_password = False
    if not password and _is_filled(previous.get("DATABASE_PASSWORD")):
        password, from_previous_password = previous["DATABASE_PASSWORD"], True
    if password:
        _check_single_line("DATABASE_PASSWORD", password)
        _warn_risky("DATABASE_PASSWORD", password, report)
        report.status["DATABASE_PASSWORD"] = "задан" + (", из прежнего render.env" if from_previous_password else "")
        if url_has_password:
            report.warnings.append("В DATABASE_URL уже есть пароль — бот возьмёт его, DATABASE_PASSWORD не понадобится.")
    elif url_has_password:
        password = PASSWORD_PLACEHOLDER
        report.status["DATABASE_PASSWORD"] = "не нужен (пароль уже в DATABASE_URL); строку можно не заполнять"
    else:
        password = PASSWORD_PLACEHOLDER
        report.status["DATABASE_PASSWORD"] = "НУЖНО ВПИСАТЬ — пароль базы Supabase"
        report.todo.append("DATABASE_PASSWORD")

    lines = {
        "BOT_TOKEN": token,
        "ADMIN_IDS": admin_ids,
        "GEMINI_API_KEY": gemini,
        "RUN_MODE": RENDER_YAML_VALUES["RUN_MODE"],
        "DATABASE_URL": url,
        "DATABASE_PASSWORD": password,
    }
    optional = _optional_ai_keys(values)
    for key, value in optional.items():
        _check_single_line(key, value)
        report.status[key] = "из .env"
    return [f"{key}={lines[key]}" for key in OUTPUT_KEYS] + [f"{key}={value}" for key, value in optional.items()]


def write_secret_file(path: Path, lines: Sequence[str]) -> None:
    """Записать файл целиком (через временный файл; на Linux/macOS — с правами только для владельца)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def _gitignore_covers(out: Path) -> bool | None:
    """Внесён ли файл в .gitignore проекта (None — файл вне проекта или .gitignore нет)."""
    gitignore = ROOT / ".gitignore"
    try:
        relative = out.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None
    if not gitignore.is_file():
        return None
    patterns = {line.strip().lstrip("/") for line in gitignore.read_text(encoding="utf-8").splitlines()}
    return relative in patterns or out.name in patterns


# --- Командная строка -------------------------------------------------------------------------------


class _RuFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):  # type: ignore[no-untyped-def]
        return super().add_usage(usage, actions, groups, prefix="Запуск: ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python deploy/make_render_env.py",
        description=(
            "Готовит файл deploy/render.env со строками для Render:\n"
            "  BOT_TOKEN, ADMIN_IDS, GEMINI_API_KEY — из .env;\n"
            "  RUN_MODE=webhook;\n"
            "  DATABASE_URL, DATABASE_PASSWORD — из параметров ниже или заглушки, которые\n"
            "  руководитель заменит сам (docs/DEPLOY_RENDER.md, шаг 2);\n"
            "  GROQ_API_KEY, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, MISTRAL_API_KEY,\n"
            "  OPENROUTER_API_KEY — запасной бесплатный AI, из .env, если заданы.\n"
            "Значения секретов на экран не выводятся. Файл .env не меняется."
        ),
        epilog=(
            "Примеры:\n"
            "  .venv\\Scripts\\python deploy\\make_render_env.py\n"
            "  .venv\\Scripts\\python deploy\\make_render_env.py --database-url - --database-password -\n"
            "Значение «-» — скрипт спросит его сам, ввод не отображается и не попадает в историю команд.\n"
            "Подробно — docs/DEPLOY_RENDER.md."
        ),
        formatter_class=_RuFormatter,
        add_help=False,
    )
    parser._optionals.title = "параметры"  # noqa: SLF001 - русский заголовок справки
    parser.add_argument("-h", "--help", action="help", help="показать эту справку и выйти")
    parser.add_argument(
        "--env", type=Path, default=DEFAULT_ENV, metavar="ПУТЬ", help="файл настроек бота (по умолчанию .env в папке бота)"
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, metavar="ПУТЬ", help="куда записать результат (по умолчанию deploy/render.env)"
    )
    parser.add_argument(
        "--database-url",
        metavar="СТРОКА",
        help="строка Supabase «Session pooler» (порт 5432) как есть, можно с [YOUR-PASSWORD]; «-» — спросить",
    )
    parser.add_argument(
        "--database-password", metavar="ПАРОЛЬ", help="пароль базы Supabase; «-» — спросить (ввод скрыт)"
    )
    return parser


def _ask(value: str | None, prompt: str) -> str | None:
    if value != "-":
        return value
    return getpass.getpass(prompt)


def _print_report(out: Path, report: Report, other: list[str], covered: bool | None) -> None:
    print(f"Готово: {out}")
    keys = [*OUTPUT_KEYS, *(key for key in OPTIONAL_AI_KEYS if key in report.status)]
    width = max(len(key) for key in keys)
    for key in keys:
        print(f"  {key.ljust(width)}  — {report.status.get(key, '')}")
    print("Значения секретов на экран не выводятся. Файл никому не пересылайте.")
    if covered is False:
        print(f"ВНИМАНИЕ: {out.name} не внесён в .gitignore — добавьте его, иначе пароли попадут в GitHub.")
    for warning in report.warnings:
        print(f"Предупреждение: {warning}")
    if other:
        print(
            "В .env заданы ещё настройки, которые в render.env не переносятся: "
            f"{', '.join(other)}. Если они нужны на Render — добавьте их в Render → сервис → Environment."
        )
    if report.todo:
        print(
            f"Осталось вписать в файл: {', '.join(report.todo)} — откройте {out.name} Блокнотом и замените "
            "текст «ВСТАВЬТЕ_СЮДА_…» (docs/DEPLOY_RENDER.md, шаг 2)."
        )
    print("Дальше: Render → New → Blueprint → значения из этого файла (docs/DEPLOY_RENDER.md, шаг 3).")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env_path: Path = args.env
    out: Path = args.out
    try:
        if out.resolve() == env_path.resolve():
            raise SetupError("Нельзя записывать результат в сам файл .env — укажите другой путь в --out.")
        values = read_env_file(env_path)
        previous = parse_env_text(out.read_text(encoding="utf-8-sig")) if out.is_file() else {}
        database_url = _ask(args.database_url, "Строка Session pooler из Supabase (ввод скрыт): ")
        database_password = _ask(args.database_password, "Пароль базы Supabase (ввод скрыт): ")
        report = Report()
        lines = build_lines(
            values,
            database_url=database_url,
            database_password=database_password,
            previous=previous,
            report=report,
        )
        write_secret_file(out, lines)
    except SetupError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Ошибка: не удалось прочитать или записать файл ({exc.strerror or type(exc).__name__}).", file=sys.stderr)
        return 2
    _print_report(out, report, _other_settings(values), _gitignore_covers(out))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    raise SystemExit(main())
