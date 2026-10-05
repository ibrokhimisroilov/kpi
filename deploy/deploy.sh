#!/usr/bin/env bash
# Выкладка бота на сервер (запуск с компьютера разработчика из Git Bash / Linux / macOS).
#
#   deploy/deploy.sh user@host [--key путь_к_ssh_ключу] [--first-run]
#
# Что делает:
#   1. копирует код (bot/, requirements.txt, Dockerfile, docker-compose.yml) и .env в ~/kpi-bot на сервере;
#   2. с флагом --first-run — переносит базу data/bot.db, только если на сервере базы ещё нет
#      (существующую базу на сервере скрипт НИКОГДА не перезаписывает);
#   3. пересобирает и перезапускает контейнер (docker compose up -d --build).
set -euo pipefail

usage() { echo "Использование: $0 user@host [--key ~/.ssh/key] [--first-run]" >&2; exit 2; }

[ $# -ge 1 ] || usage
TARGET="$1"; shift
KEY=""
FIRST_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --key) KEY="$2"; shift 2 ;;
        --first-run) FIRST_RUN=1; shift ;;
        *) usage ;;
    esac
done

cd "$(dirname "$0")/.."
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)
[ -n "$KEY" ] && SSH_OPTS+=(-i "$KEY")
REMOTE_DIR="kpi-bot"

[ -f .env ] || { echo "Нет файла .env — заполните его по образцу .env.example" >&2; exit 1; }

echo "→ Копирую код на $TARGET:~/$REMOTE_DIR"
tar czf - --exclude='__pycache__' bot requirements.txt Dockerfile docker-compose.yml .dockerignore \
    | ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p ~/$REMOTE_DIR/data && tar xzf - -C ~/$REMOTE_DIR"

echo "→ Копирую настройки .env"
scp "${SSH_OPTS[@]}" -q .env "$TARGET:~/$REMOTE_DIR/.env"
ssh "${SSH_OPTS[@]}" "$TARGET" "chmod 600 ~/$REMOTE_DIR/.env"

if [ "$FIRST_RUN" -eq 1 ]; then
    if ssh "${SSH_OPTS[@]}" "$TARGET" "test -s ~/$REMOTE_DIR/data/bot.db"; then
        echo "→ На сервере уже есть база data/bot.db — не трогаю её."
    elif [ -f data/bot.db ]; then
        # Снимок через backup API SQLite: в него попадают и изменения из bot.db-wal.
        PY=""
        for cand in .venv/Scripts/python.exe .venv/bin/python python3 python; do
            if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then PY="$cand"; break; fi
        done
        [ -n "$PY" ] || { echo "Не найден Python для снимка базы" >&2; exit 1; }
        SNAPSHOT="$(mktemp)"
        trap 'rm -f "$SNAPSHOT"' EXIT
        "$PY" -c "import sqlite3,sys; s=sqlite3.connect('file:data/bot.db?mode=ro', uri=True); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close(); s.close()" "$SNAPSHOT"
        echo "→ Переношу базу data/bot.db на сервер (согласованный снимок)"
        scp "${SSH_OPTS[@]}" -q "$SNAPSHOT" "$TARGET:~/$REMOTE_DIR/data/bot.db"
    else
        echo "→ Локальной базы нет — бот создаст новую."
    fi
fi

echo "→ Собираю и запускаю контейнер"
ssh "${SSH_OPTS[@]}" "$TARGET" "cd ~/$REMOTE_DIR && docker compose up -d --build && sleep 5 && docker compose ps && docker compose logs --tail 15"
echo "Готово."
