#!/usr/bin/env bash
# Установка systemd-сервиса на Linux-сервере.
# Запускать от root: sudo bash deploy/install.sh
set -euo pipefail

APP_USER="${APP_USER:-luzhniki-bot}"
APP_DIR="${APP_DIR:-/opt/luzhniki-tasks-bot}"
SERVICE_NAME="luzhniki-tasks-bot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите от root: sudo bash deploy/install.sh"
  exit 1
fi

echo "==> Пользователь: ${APP_USER}"
echo "==> Каталог:      ${APP_DIR}"

if ! id "${APP_USER}" &>/dev/null; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
  echo "Создан пользователь ${APP_USER}"
fi

mkdir -p "${APP_DIR}"

# Копируем код (если install запущен из репозитория на сервере)
rsync -a --delete \
  --exclude venv \
  --exclude __pycache__ \
  --exclude .git \
  --exclude .env \
  "${PROJECT_DIR}/" "${APP_DIR}/"

chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# venv
if [[ ! -x "${APP_DIR}/venv/bin/python" ]]; then
  echo "==> Создаю venv и ставлю зависимости..."
  sudo -u "${APP_USER}" python3 -m venv "${APP_DIR}/venv"
  sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install --upgrade pip
  sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
fi

# .env и credentials — положите вручную, если ещё нет
if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo ""
  echo "ВНИМАНИЕ: создайте ${APP_DIR}/.env и JSON-ключ Google:"
  echo "  nano ${APP_DIR}/.env"
  echo "  nano ${APP_DIR}/calm-photon-....json"
  echo "  chown ${APP_USER}:${APP_USER} ${APP_DIR}/.env ${APP_DIR}/*.json"
  echo ""
fi

# systemd unit с подстановкой путей
sed \
  -e "s|/opt/luzhniki-tasks-bot|${APP_DIR}|g" \
  -e "s|User=luzhniki-bot|User=${APP_USER}|g" \
  -e "s|Group=luzhniki-bot|Group=${APP_USER}|g" \
  "${SCRIPT_DIR}/luzhniki-tasks-bot.service" > "/etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo ""
echo "Готово. Дальше:"
echo "  1. Положите .env и credentials в ${APP_DIR}"
echo "  2. sudo systemctl start ${SERVICE_NAME}"
echo "  3. sudo systemctl status ${SERVICE_NAME}"
echo "  4. sudo journalctl -u ${SERVICE_NAME} -f"
