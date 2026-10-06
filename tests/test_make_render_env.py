"""deploy/make_render_env.py: файл deploy/render.env для Render из локального .env (только временные файлы)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "make_render_env.py"

TOKEN = "1234567890:AAHsecretTokenValue_abc-XYZ0123456789"
GEMINI = "AIzaSySecretGeminiKey0123456789"
POOLER_URL = "postgresql://postgres.abcdefghijklmnop:[YOUR-PASSWORD]@aws-1-eu-central-1.pooler.supabase.com:5432/postgres"
DB_PASSWORD = "Gen3ratedDbPassw0rd"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_render_env", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses ищут модуль по имени
    spec.loader.exec_module(module)
    return module


mre = _load()


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "# настройки\n"
        f"BOT_TOKEN={TOKEN}\n"
        "ADMIN_IDS=111111111, 222222222\n"
        f"GEMINI_API_KEY={GEMINI}\n"
        "DATABASE_URL=sqlite+aiosqlite:///data/bot.db\n"
        "TIMEZONE=Asia/Tashkent\n"
        "BACKUP_HOUR=22\n"
        "AI_READ_FILES=false\n"
        "# QUIET_HOURS_START=20\n",
        encoding="utf-8",
    )
    return path


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    code = mre.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _assert_no_secrets(output: str, *secrets: str) -> None:
    for secret in (TOKEN, GEMINI, "111111111", DB_PASSWORD, "abcdefghijklmnop", *secrets):
        assert secret not in output


def test_placeholders_when_database_not_given(tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "deploy" / "render.env"
    code, output = _run(capsys, "--env", str(env_file), "--out", str(out))
    assert code == 0
    assert _lines(out) == [
        f"BOT_TOKEN={TOKEN}",
        "ADMIN_IDS=111111111,222222222",
        f"GEMINI_API_KEY={GEMINI}",
        "RUN_MODE=webhook",
        f"DATABASE_URL={mre.URL_PLACEHOLDER}",
        f"DATABASE_PASSWORD={mre.PASSWORD_PLACEHOLDER}",
    ]
    _assert_no_secrets(output)
    for key in mre.OUTPUT_KEYS:
        assert key in output
    assert "НУЖНО ВПИСАТЬ" in output
    # Прочие настройки — только названия; совпадающий с render.yaml TIMEZONE и закомментированные — не упоминаются.
    assert "BACKUP_HOUR" in output and "AI_READ_FILES" in output
    assert "TIMEZONE" not in output and "QUIET_HOURS_START" not in output
    assert env_file.read_text(encoding="utf-8").startswith("# настройки\nBOT_TOKEN=")  # .env не тронут


def test_database_values_from_arguments(tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "render.env"
    code, output = _run(
        capsys,
        "--env", str(env_file), "--out", str(out),
        "--database-url", POOLER_URL, "--database-password", DB_PASSWORD,
    )  # fmt: skip
    assert code == 0
    lines = _lines(out)
    assert lines[4] == f"DATABASE_URL={POOLER_URL}"
    assert lines[5] == f"DATABASE_PASSWORD={DB_PASSWORD}"
    _assert_no_secrets(output, POOLER_URL)
    assert "НУЖНО ВПИСАТЬ" not in output and "Предупреждение" not in output


def test_dash_asks_without_echo(
    tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([POOLER_URL, DB_PASSWORD])
    monkeypatch.setattr(mre.getpass, "getpass", lambda prompt="": next(answers))
    out = tmp_path / "render.env"
    code, output = _run(
        capsys, "--env", str(env_file), "--out", str(out), "--database-url", "-", "--database-password", "-"
    )
    assert code == 0
    assert _lines(out)[4:] == [f"DATABASE_URL={POOLER_URL}", f"DATABASE_PASSWORD={DB_PASSWORD}"]
    _assert_no_secrets(output, POOLER_URL)


def test_previous_database_values_are_kept(tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "render.env"
    out.write_text(
        f"BOT_TOKEN=old\nDATABASE_URL={POOLER_URL}\nDATABASE_PASSWORD={DB_PASSWORD}\n", encoding="utf-8"
    )
    code, output = _run(capsys, "--env", str(env_file), "--out", str(out))
    assert code == 0
    lines = _lines(out)
    assert lines[0] == f"BOT_TOKEN={TOKEN}"
    assert lines[4:] == [f"DATABASE_URL={POOLER_URL}", f"DATABASE_PASSWORD={DB_PASSWORD}"]
    assert "из прежнего render.env" in output
    _assert_no_secrets(output, POOLER_URL)


def test_placeholders_in_previous_file_are_not_values(
    tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "render.env"
    assert _run(capsys, "--env", str(env_file), "--out", str(out))[0] == 0
    code, output = _run(capsys, "--env", str(env_file), "--out", str(out))
    assert code == 0
    assert _lines(out)[4:] == [f"DATABASE_URL={mre.URL_PLACEHOLDER}", f"DATABASE_PASSWORD={mre.PASSWORD_PLACEHOLDER}"]
    assert "НУЖНО ВПИСАТЬ" in output


def test_url_with_password_does_not_need_database_password(
    tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = POOLER_URL.replace("[YOUR-PASSWORD]", DB_PASSWORD)
    out = tmp_path / "render.env"
    code, output = _run(capsys, "--env", str(env_file), "--out", str(out), "--database-url", url)
    assert code == 0
    assert _lines(out)[4] == f"DATABASE_URL={url}"
    assert "не нужен" in output and "Осталось вписать" not in output
    _assert_no_secrets(output, url)


@pytest.mark.parametrize(
    ("url", "hint"),
    [
        ("postgresql://postgres:[YOUR-PASSWORD]@db.abcdefghijklmnop.supabase.co:5432/postgres", "Direct connection"),
        ("postgresql://postgres.abcdefghijklmnop:[YOUR-PASSWORD]@aws-0-eu-central-1.pooler.supabase.com:6543/postgres", "6543"),
    ],
)
def test_wrong_supabase_connection_is_warned(
    tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str], url: str, hint: str
) -> None:
    out = tmp_path / "render.env"
    code, output = _run(capsys, "--env", str(env_file), "--out", str(out), "--database-url", url)
    assert code == 0
    assert "Предупреждение" in output and hint in output
    _assert_no_secrets(output, url)


@pytest.mark.parametrize(
    ("env_text", "args", "message"),
    [
        ("ADMIN_IDS=1\n", (), "BOT_TOKEN"),
        ("BOT_TOKEN=not a token\n", (), "BOT_TOKEN"),
        (f"BOT_TOKEN={TOKEN}\nADMIN_IDS=12ab\n", (), "ADMIN_IDS"),
        (f"BOT_TOKEN={TOKEN}\n", ("--database-url", "sqlite+aiosqlite:///data/bot.db"), "postgresql://"),
    ],
)
def test_errors_create_no_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], env_text: str, args: tuple[str, ...], message: str
) -> None:
    env = tmp_path / ".env"
    env.write_text(env_text, encoding="utf-8")
    out = tmp_path / "render.env"
    code, output = _run(capsys, "--env", str(env), "--out", str(out), *args)
    assert code == 2
    assert message in output
    assert not out.exists()
    assert TOKEN not in output


def test_missing_env_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, output = _run(capsys, "--env", str(tmp_path / "nope.env"), "--out", str(tmp_path / "render.env"))
    assert code == 2
    assert "Не найден файл настроек" in output


def test_never_overwrites_env_itself(tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = env_file.read_bytes()
    code, output = _run(capsys, "--env", str(env_file), "--out", str(env_file))
    assert code == 2
    assert "Нельзя записывать" in output
    assert env_file.read_bytes() == before


def test_empty_admins_and_gemini_are_allowed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / ".env"
    env.write_text(f"BOT_TOKEN={TOKEN}\nADMIN_IDS=\nGEMINI_API_KEY=\n", encoding="utf-8")
    out = tmp_path / "render.env"
    code, output = _run(capsys, "--env", str(env), "--out", str(out))
    assert code == 0
    assert _lines(out)[1:3] == ["ADMIN_IDS=", "GEMINI_API_KEY="]
    assert "ADMIN_IDS пустой" in output and "без AI" in output


GROQ = "gsk_SecretGroqKey0123456789"
CF_TOKEN = "cfSecretToken0123456789"


def test_optional_ai_keys_are_copied_only_when_set(tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Ключи запасного AI из .env попадают в конец render.env (строки базы остаются на своих местах);
    незаданные — не пишутся; значения на экран не выводятся. AI_PROVIDER=gemini (прежнее название «auto»)
    не считается «настройкой, которую надо перенести»."""
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + f"GROQ_API_KEY={GROQ}\nCLOUDFLARE_API_TOKEN={CF_TOKEN}\nMISTRAL_API_KEY=\nAI_PROVIDER=gemini\n",
        encoding="utf-8",
    )
    out = tmp_path / "render.env"
    code, output = _run(capsys, "--env", str(env_file), "--out", str(out))
    assert code == 0
    lines = _lines(out)
    assert lines[4].startswith("DATABASE_URL=") and lines[5].startswith("DATABASE_PASSWORD=")
    assert lines[6:] == [f"GROQ_API_KEY={GROQ}", f"CLOUDFLARE_API_TOKEN={CF_TOKEN}"]
    _assert_no_secrets(output, GROQ, CF_TOKEN)
    assert "GROQ_API_KEY" in output and "CLOUDFLARE_API_TOKEN" in output
    assert "MISTRAL_API_KEY" not in output and "AI_PROVIDER" not in output


def test_only_backup_ai_key_still_means_ai_on(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / ".env"
    env.write_text(f"BOT_TOKEN={TOKEN}\nADMIN_IDS=1\nGEMINI_API_KEY=\nGROQ_API_KEY={GROQ}\n", encoding="utf-8")
    out = tmp_path / "render.env"
    code, output = _run(capsys, "--env", str(env), "--out", str(out))
    assert code == 0
    assert "запасных провайдеров" in output and "без AI" not in output
    assert _lines(out)[-1] == f"GROQ_API_KEY={GROQ}"
    _assert_no_secrets(output, GROQ)


def test_risky_password_is_warned(tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "render.env"
    code, output = _run(capsys, "--env", str(env_file), "--out", str(out), "--database-password", "pa#ss$word")
    assert code == 0
    assert _lines(out)[5] == "DATABASE_PASSWORD=pa#ss$word"
    assert "сверьте это поле" in output and "pa#ss$word" not in output


ENV_TEXT = (
    "﻿# comment\n"
    f'export BOT_TOKEN="{TOKEN}"\n'
    "ADMIN_IDS='1,2' # руководители\n"
    f"gemini_api_key={GEMINI}   # ключ\n"
    "BROKEN LINE\n"
    "EMPTY=\n"
)


def test_fallback_parser_matches_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"BOT_TOKEN": TOKEN, "ADMIN_IDS": "1,2", "GEMINI_API_KEY": GEMINI, "EMPTY": ""}
    assert mre.parse_env_text(ENV_TEXT) == expected
    env = tmp_path / ".env"
    env.write_text(ENV_TEXT, encoding="utf-8")
    via_dotenv = mre.read_env_file(env)  # python-dotenv установлен вместе с pydantic-settings
    assert {key: via_dotenv[key] for key in expected} == expected
    monkeypatch.setitem(sys.modules, "dotenv", None)  # как будто python-dotenv нет
    assert mre.read_env_file(env) == expected


def test_render_env_is_git_and_docker_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "deploy/render.env" in gitignore
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "deploy" in dockerignore
    assert mre._gitignore_covers(mre.DEFAULT_OUT) is True


def test_keys_match_render_yaml() -> None:
    """Секреты, которые Render спрашивает (sync: false), — ровно обязательные строки скрипта (кроме RUN_MODE).

    Ключи запасного AI в Blueprint намеренно не входят (при создании — только 5 полей); их добавляют
    потом в Render → Environment.
    """
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    secret_keys = set(re.findall(r"- key: (\w+)\s*\n\s*sync: false", text))
    fixed = dict(re.findall(r"- key: (\w+)\s*\n\s*value: (\S+)", text))
    assert secret_keys == set(mre.OUTPUT_KEYS) - {"RUN_MODE"}
    assert not secret_keys & set(mre.OPTIONAL_AI_KEYS)
    assert fixed == mre.RENDER_YAML_VALUES
    assert re.search(r"^\s+plan: free$", text, re.MULTILINE)
    assert re.search(r"^\s+region: frankfurt$", text, re.MULTILINE)
    assert re.search(r"^\s+healthCheckPath: /health$", text, re.MULTILINE)


def test_bot_rejects_unreplaced_placeholders_and_accepts_filled_file(
    tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Значения из deploy/render.env так, как их получит бот на Render: заглушки «ВСТАВЬТЕ_СЮДА_…» —
    понятная ошибка запуска (bot.main.check_database), заполненный файл — строка Supabase с
    [YOUR-PASSWORD] + DATABASE_PASSWORD дают адрес с настоящим паролем."""
    from bot.config import Settings
    from bot.db.base import apply_password
    from bot.main import ConfigError, check_database

    def settings_from(path: Path) -> Settings:
        values = dict(line.split("=", 1) for line in _lines(path))
        return Settings(_env_file=None, database_url=values["DATABASE_URL"], database_password=values["DATABASE_PASSWORD"])

    out = tmp_path / "render.env"
    assert _run(capsys, "--env", str(env_file), "--out", str(out))[0] == 0
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        check_database(settings_from(out))
    assert _run(capsys, "--env", str(env_file), "--out", str(out), "--database-url", POOLER_URL)[0] == 0
    with pytest.raises(ConfigError, match="DATABASE_PASSWORD"):
        check_database(settings_from(out))
    assert _run(capsys, "--env", str(env_file), "--out", str(out), "--database-password", DB_PASSWORD)[0] == 0
    settings = settings_from(out)
    check_database(settings)
    assert apply_password(settings.database_url, settings.database_password).password == DB_PASSWORD

def test_url_with_password_and_placeholder_password_pass_bot_checks(
    tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Пароль уже в DATABASE_URL, --database-password не задан: скрипт пишет заглушку в DATABASE_PASSWORD
    и говорит «строку можно не заполнять». Бот на Render (bot.main.check_database) такой файл принимает —
    пароль из адреса важнее, заглушку он не использует — и подключается с паролем из адреса."""
    from bot.config import Settings
    from bot.db.base import apply_password
    from bot.main import check_database

    url = POOLER_URL.replace("[YOUR-PASSWORD]", DB_PASSWORD)
    out = tmp_path / "render.env"
    code, output = _run(capsys, "--env", str(env_file), "--out", str(out), "--database-url", url)
    assert code == 0 and "можно не заполнять" in output
    values = dict(line.split("=", 1) for line in _lines(out))
    assert values["DATABASE_PASSWORD"] == mre.PASSWORD_PLACEHOLDER
    for run_mode in ("webhook", "polling"):
        settings = Settings(
            _env_file=None,
            run_mode=run_mode,
            public_url="https://kpi-bot.onrender.com",
            database_url=values["DATABASE_URL"],
            database_password=values["DATABASE_PASSWORD"],
        )
        check_database(settings)
        assert apply_password(settings.database_url, settings.database_password).password == DB_PASSWORD
