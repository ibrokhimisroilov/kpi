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


# Бесплатные AI-провайдеры, которые знает бот (bot.ai.provider). Порядок по умолчанию — AI_PROVIDERS.
AI_PROVIDER_NAMES = ("gemini", "groq", "cloudflare", "mistral", "openrouter")
# Переменные с ключами (для подсказок в журнале и документации).
AI_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "cloudflare": "CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID",
    "mistral": "MISTRAL_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# Для чего запрос к AI (bot.ai.provider.ai_purpose): «formulate» — подсказка измеримой формулировки
# (короткий ответ, важна скорость), «evaluate» — предварительная оценка сдачи (важно качество суждения).
AI_PURPOSES = ("formulate", "evaluate")
# В каком порядке спрашивать модели Gemini для каждой задачи, если GEMINI_FORMULATE_MODELS /
# GEMINI_EVALUATE_MODELS не заданы: модели из GEMINI_MODELS переставляются в этом порядке (моделей, которых
# здесь нет, — после, в порядке GEMINI_MODELS; моделей не из GEMINI_MODELS бот не спрашивает).
# Замеры (10.2026): 3.5-flash-lite отвечает за ~0,7–1 с, 3.6-flash — за ~1,7–2 с, 3.5-flash — за 1,7–12 с,
# 3.8-flash часто отвечает 503 «high demand» через несколько секунд.
GEMINI_PURPOSE_ORDER: dict[str, tuple[str, ...]] = {
    # Короткая переформулировка — сначала самые быстрые.
    "formulate": (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.7-flash",
        "gemini-3.8-flash",
        "gemma-4-31b-it",
    ),
    # Суждение о результате — сначала сильные и при этом быстрые, облегчённые — запасом.
    "evaluate": (
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemma-4-31b-it",
    ),
}


def _by_preference(models: list[str], preferred: tuple[str, ...]) -> list[str]:
    """Модели в порядке ``preferred``; моделей не из него — после, в исходном порядке (сортировка устойчивая)."""
    rank = {model: index for index, model in enumerate(preferred)}
    return sorted(models, key=lambda model: rank.get(model, len(preferred)))


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

    # --- AI (только бесплатные тарифы; цепочка провайдеров — bot.ai.provider) ---
    # "auto" — AI включён: провайдеры из AI_PROVIDERS по порядку, у которых задан ключ;
    # "none" — только правила (без AI). "gemini" — прежнее значение, работает как "auto".
    ai_provider: Literal["auto", "gemini", "none"] = "auto"
    # Порядок провайдеров. Провайдер без ключа пропускается; не ответил никто — расчёт по правилам.
    ai_providers: Annotated[list[str], NoDecode] = list(AI_PROVIDER_NAMES)
    # Модели каждого провайдера пробуются по порядку: у первой исчерпан бесплатный лимит — берётся следующая.
    gemini_api_key: str = Field(default="", repr=False)
    gemini_models: Annotated[list[str], NoDecode] = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemma-4-31b-it",
    ]
    # Свой порядок моделей Gemini для подсказки формулировки / оценки сдачи (через запятую). Пусто — модели
    # GEMINI_MODELS в порядке GEMINI_PURPOSE_ORDER (для формулировки — сначала быстрые flash-lite).
    gemini_formulate_models: Annotated[list[str], NoDecode] = []
    gemini_evaluate_models: Annotated[list[str], NoDecode] = []
    # Groq (console.groq.com): бесплатно, без карты; данные не используются для обучения.
    groq_api_key: str = Field(default="", repr=False)
    groq_models: Annotated[list[str], NoDecode] = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
    # Cloudflare Workers AI (dash.cloudflare.com): бесплатно 10 000 «нейронов» в сутки, без карты.
    cloudflare_account_id: str = Field(default="", repr=False)
    cloudflare_api_token: str = Field(default="", repr=False)
    cloudflare_models: Annotated[list[str], NoDecode] = [
        "@cf/google/gemma-4-26b-a4b-it",
        "@cf/mistralai/mistral-small-3.1-24b-instruct",
        "@cf/openai/gpt-oss-120b",
    ]
    # Mistral (console.mistral.ai, бесплатный режим Free/Experiment).
    mistral_api_key: str = Field(default="", repr=False)
    mistral_models: Annotated[list[str], NoDecode] = ["mistral-medium-latest", "mistral-small-latest"]
    # OpenRouter (openrouter.ai): бесплатные модели «:free», до 50 запросов в сутки.
    openrouter_api_key: str = Field(default="", repr=False)
    openrouter_models: Annotated[list[str], NoDecode] = [
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "openrouter/free",
    ]
    # Модели Groq / Cloudflare / Mistral / OpenRouter, которым можно передавать изображения
    # (остальным — только текст и пометка «файл приложен»). Модели Gemini видят изображения все.
    ai_vision_models: Annotated[list[str], NoDecode] = [
        "qwen/qwen3.8-27b",
        "@cf/google/gemma-4-26b-a4b-it",
        "@cf/mistralai/mistral-small-3.1-24b-instruct",
        "@cf/meta/llama-4-scout-17b-16e-instruct",
        "mistral-medium-latest",
        "mistral-large-latest",
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
    ]
    # Сколько секунд ждать ответа одной модели (потолок для всех запросов); весь перебор — 2 × AI_TIMEOUT_SEC.
    ai_timeout_sec: int = 60
    # Подсказка формулировки — короткий ответ: модель, не ответившая за AI_FORMULATE_TIMEOUT_SEC, уступает
    # следующей; вся подсказка — не дольше AI_FORMULATE_BUDGET_SEC, потом формулировка по правилам.
    ai_formulate_timeout_sec: int = 9
    ai_formulate_budget_sec: int = 25
    # Оценка сдачи: одна модель — не дольше AI_EVALUATE_TIMEOUT_SEC (если приложены PDF или фото — до
    # AI_TIMEOUT_SEC + 5 с: чтение файлов дольше), весь перебор — 2 × AI_TIMEOUT_SEC.
    ai_evaluate_timeout_sec: int = 30
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

    @field_validator(
        "admin_ids",
        "gemini_models",
        "gemini_formulate_models",
        "gemini_evaluate_models",
        "groq_models",
        "cloudflare_models",
        "mistral_models",
        "openrouter_models",
        "ai_vision_models",
        "reminder_days_before",
        mode="before",
    )
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("ai_providers", mode="before")
    @classmethod
    def _parse_providers(cls, value: object) -> object:
        """«Gemini, groq» -> ["gemini", "groq"] (регистр и пробелы не важны)."""
        items = _split_csv(value)
        return [item.lower() for item in items if isinstance(item, str)] if isinstance(items, list) else items

    @field_validator("ai_provider", mode="before")
    @classmethod
    def _parse_ai_provider(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    # --- AI: какие провайдеры работают ---

    def ai_key_for(self, name: str) -> str:
        """Ключ провайдера ("" — не задан). У Cloudflare нужен ещё CLOUDFLARE_ACCOUNT_ID."""
        if name == "cloudflare":
            return self.cloudflare_api_token.strip() if self.cloudflare_account_id.strip() else ""
        keys = {
            "gemini": self.gemini_api_key,
            "groq": self.groq_api_key,
            "mistral": self.mistral_api_key,
            "openrouter": self.openrouter_api_key,
        }
        return keys.get(name, "").strip()

    def ai_models_for(self, name: str, purpose: str | None = None) -> list[str]:
        """Модели провайдера по порядку. purpose («formulate» / «evaluate», AI_PURPOSES) меняет порядок
        моделей Gemini: GEMINI_FORMULATE_MODELS / GEMINI_EVALUATE_MODELS, если заданы, иначе GEMINI_MODELS
        в порядке GEMINI_PURPOSE_ORDER. У остальных провайдеров порядок один для всех задач."""
        models = {
            "gemini": self.gemini_models,
            "groq": self.groq_models,
            "cloudflare": self.cloudflare_models,
            "mistral": self.mistral_models,
            "openrouter": self.openrouter_models,
        }.get(name, [])
        result = [model.strip() for model in models if model.strip()]
        if name != "gemini" or purpose not in AI_PURPOSES:
            return result
        explicit = self.gemini_formulate_models if purpose == "formulate" else self.gemini_evaluate_models
        own = list(dict.fromkeys(model.strip() for model in explicit if model.strip()))
        return own or _by_preference(result, GEMINI_PURPOSE_ORDER[purpose])

    @property
    def active_ai_providers(self) -> list[str]:
        """Провайдеры из AI_PROVIDERS (по порядку, без повторов), у которых задан ключ и есть модели."""
        result: list[str] = []
        for name in self.ai_providers:
            if name in AI_PROVIDER_NAMES and name not in result and self.ai_key_for(name) and self.ai_models_for(name):
                result.append(name)
        return result

    @property
    def ai_secrets(self) -> list[str]:
        """Все ключи AI (и номер аккаунта Cloudflare) — чтобы вырезать их из текстов для журнала."""
        values = (
            self.gemini_api_key,
            self.groq_api_key,
            self.cloudflare_api_token,
            self.cloudflare_account_id,
            self.mistral_api_key,
            self.openrouter_api_key,
        )
        return [value.strip() for value in values if len(value.strip()) >= 6]

    @property
    def ai_enabled(self) -> bool:
        """AI включён: AI_PROVIDER не none и хотя бы у одного провайдера задан ключ."""
        return self.ai_provider != "none" and bool(self.active_ai_providers)

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
