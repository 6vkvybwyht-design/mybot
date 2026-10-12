#!/usr/bin/env bash
# Оновлення: git pull + залежності + перезапуск. Запускати від root: sudo /opt/usousdt-bot/deploy/update.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/usousdt-bot}"
APP_USER="${APP_USER:-usousdt}"

cd "$APP_DIR"
sudo -u "$APP_USER" git pull --ff-only
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r requirements.txt
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m bot.main --check-config
install -m 644 deploy/usousdt-bot.service /etc/systemd/system/usousdt-bot.service
systemctl daemon-reload
systemctl restart usousdt-bot
sleep 3
systemctl --no-pager --lines=5 status usousdt-bot
