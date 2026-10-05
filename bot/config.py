"""Настройки бота. Читаются из переменных окружения и файла .env."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: object) -> object:
    """Позволяет задавать списки в .env через запятую: ADMIN_IDS=123,456."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, int):
        return [value]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram ---
    bot_token: str = ""
    # Telegram ID руководителей, которые получают права сразу после /start.
    admin_ids: Annotated[list[int], NoDecode] = []

    # --- Хранилище ---
    # SQLite-файл (по умолчанию) или PostgreSQL: «postgresql://user:pass@host:5432/db»
    # (схема postgres:// / postgresql:// приводится к postgresql+asyncpg:// автоматически).
    database_url: str = "sqlite+aiosqlite:///data/bot.db"
    # Пароль PostgreSQL отдельно от адреса (DATABASE_PASSWORD): строку Supabase можно вставить как есть,
    # с «[YOUR-PASSWORD]» вместо пароля или без пароля, — бот подставит его сам (bot.db.base.make_engine).
    # Спецсимволы экранировать не нужно. Пароль из адреса важнее. В лог и repr настроек не попадает.
    database_password: str = Field(default="", repr=False)
    timezone: str = "Asia/Tashkent"

    # --- AI (бесплатный тариф Google Gemini) ---
    # "gemini" — подсказки и оценки через Gemini; "none" — только правила (без AI).
    ai_provider: Literal["gemini", "none"] = "gemini"
    gemini_api_key: str = ""
    # Модели пробуются по порядку: если у первой исчерпан бесплатный лимит, берётся следующая.
    gemini_models: Annotated[list[str], NoDecode] = [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ]
    ai_timeout_sec: int = 60
    # Передавать ли AI содержимое приложенных файлов (PDF, фото, Word, Excel, текст).
    ai_read_files: bool = True
    ai_max_file_mb: int = 10

    # --- Оценка и KPI ---
    max_score: int = 150                 # максимальная оценка задачи, %
    late_penalty_per_day: float = 2.0    # рекомендуемый штраф за день просрочки, п.п.
    late_penalty_max: float = 20.0       # максимальный штраф за просрочку, п.п.
    overdue_counts_as_zero: bool = True  # просроченная несданная задача входит в KPI как 0 %

    # --- Сроки и напоминания ---
    default_deadline_time: str = "18:00"  # время дедлайна, если указана только дата
    reminder_days_before: Annotated[list[int], NoDecode] = [3, 1]
    reminder_hours_before: int = 3
    overdue_reminder_hour: int = 10       # во сколько напоминать о просроченных задачах
    quiet_hours_start: int = 21           # не беспокоить с 21:00 ...
    quiet_hours_end: int = 8              # ... до 08:00 (по местному времени)
    digest_weekday: int = 0               # еженедельная сводка руководителю: 0 = понедельник
    digest_hour: int = 9
    review_reminder_days: int = 2         # напомнить руководителю о непроверенном результате
    scheduler_interval_min: int = 15

    # --- Резервная копия базы ---
    backup_enabled: bool = True           # каждый день присылать руководителям файл с копией базы
    backup_hour: int = 23                 # в котором часу (местное время)

    # --- Режим работы ---
    # "polling" — бот сам забирает сообщения у Telegram (свой компьютер, VPS);
    # "webhook" — Telegram присылает сообщения на веб-адрес бота (Render и другие веб-хостинги).
    run_mode: Literal["polling", "webhook"] = "polling"
    # Публичный адрес бота для webhook, например https://kpi-bot.onrender.com.
    # На Render можно не задавать: берётся из RENDER_EXTERNAL_URL, которую Render ставит сам.
    public_url: str = ""
    render_external_url: str = ""
    port: int = 8080                      # порт веб-сервера в режиме webhook (Render задаёт PORT сам)
    # Секреты webhook и /tick. Если пусто — выводятся из BOT_TOKEN (стабильны, пока не сменён токен).
    webhook_secret: str = ""
    tick_secret: str = ""
    # Polling: если у бота включён webhook (он уже работает в облаке), запуск останавливается, чтобы
    # не увести бота из облака. TAKEOVER_WEBHOOK=1 — снять webhook и работать здесь (облачную копию
    # перед этим нужно остановить, иначе она вернёт webhook себе).
    takeover_webhook: bool = False

    log_level: str = "INFO"

    @field_validator("admin_ids", "gemini_models", "reminder_days_before", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        return _split_csv(value)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def ai_enabled(self) -> bool:
        return self.ai_provider == "gemini" and bool(self.gemini_api_key)

    @property
    def base_url(self) -> str:
        """Публичный адрес бота без «/» в конце (для webhook)."""
        return (self.public_url or self.render_external_url).rstrip("/")

    def _derived_secret(self, purpose: str) -> str:
        digest = hashlib.sha256(f"{purpose}:{self.bot_token}".encode()).hexdigest()
        return digest[:32]

    @property
    def webhook_secret_value(self) -> str:
        """Секрет для заголовка X-Telegram-Bot-Api-Secret-Token и пути webhook (A-Z, a-z, 0-9)."""
        return self.webhook_secret or self._derived_secret("webhook")

    @property
    def tick_secret_value(self) -> str:
        """Ключ для /tick?key=... — только для необязательного внешнего резервного планировщика:
        в режиме webhook бот сам запускает задания по времени и сам не даёт хостингу себя усыпить."""
        return self.tick_secret or self._derived_secret("tick")


@lru_cache
def get_settings() -> Settings:
    return Settings()
