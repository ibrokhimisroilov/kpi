# Образ Telegram-бота «Эффективность». Один образ для обоих режимов работы:
#   * polling — свой сервер/VPS: docker compose up -d (база SQLite в папке ./data на сервере, см. README.md);
#   * webhook — Render и другие веб-хостинги (RUN_MODE=webhook, база PostgreSQL, см. docs/DEPLOY_RENDER.md).
# Секретов в образе нет: BOT_TOKEN, ключи и DATABASE_URL передаются переменными окружения
# (.env через docker compose или настройки сервиса на хостинге).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала зависимости — так пересборка после правки кода проходит быстро.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot

# Папка для базы SQLite (режим polling). docker compose подключает сюда папку ./data сервера
# (volumes в docker-compose.yml), поэтому база переживает пересборку и перезапуск контейнера.
# Инструкции VOLUME здесь нет намеренно: часть облачных хостингов отклоняет образы с ней,
# а в режиме webhook база — внешний PostgreSQL (диск облачного сервера всё равно не сохраняется).
RUN mkdir -p /app/data

# Порт веб-сервера в режиме webhook. Хостинг может задать свой через переменную PORT (Render так и делает).
EXPOSE 8080

CMD ["python", "-m", "bot"]
