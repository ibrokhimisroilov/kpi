#!/usr/bin/env bash
# Первичная настройка сервера Ubuntu 22.04/24.04 для бота (запускается один раз, повторный запуск безопасен).
# Ставит Docker с compose, включает автообновления безопасности и swap 1 ГБ (для серверов с 1 ГБ памяти).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите от root, например: ssh ubuntu@IP 'sudo bash -s' < deploy/setup_server.sh" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl unattended-upgrades

if ! command -v docker >/dev/null 2>&1; then
    # Официальный скрипт установки Docker Engine + compose plugin.
    curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

# Пользователь, под которым мы заходим по SSH, может управлять Docker без sudo.
if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    usermod -aG docker "${SUDO_USER}"
fi

# Swap 1 ГБ — запас памяти на маленьких бесплатных серверах (сборка образа, пики).
if ! swapon --show | grep -q '/swapfile'; then
    if [ ! -f /swapfile ]; then
        fallocate -l 1G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile
    fi
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Автоматическая установка обновлений безопасности.
dpkg-reconfigure -f noninteractive unattended-upgrades || true

docker --version
docker compose version
echo "Сервер готов."
