#!/usr/bin/env bash
# Встановлення на чистий Ubuntu VPS. Запуск від root:
#   sudo bash deploy/install.sh https://github.com/<owner>/<repo>.git [гілка]
# Повторний запуск безпечний.
set -euo pipefail

REPO_URL="${1:?вкажіть URL репозиторію}"
BRANCH="${2:-main}"
APP_DIR="/opt/usousdt-bot"
APP_USER="usousdt"

[ "$(id -u)" -eq 0 ] || { echo "Запустіть від root (sudo)"; exit 1; }

echo "== Пакети"
apt-get update -q
apt-get install -y -q git python3 python3-venv python3-pip ca-certificates cron

PYV=$(python3 -c 'import sys; print("%d%02d" % sys.version_info[:2])')
[ "$PYV" -ge 311 ] || { echo "Потрібен Python 3.11+ (є $(python3 --version))"; exit 1; }

echo "== Час сервера — UTC"
timedatectl set-timezone UTC || true

echo "== Користувач $APP_USER"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/$APP_USER --shell /usr/sbin/nologin "$APP_USER"

echo "== Код у $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch -q origin "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout -q "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" pull -q --ff-only
else
    mkdir -p "$APP_DIR"
    chown "$APP_USER:$APP_USER" "$APP_DIR"
    sudo -u "$APP_USER" git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
sudo -u "$APP_USER" mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/backups"

echo "== Віртуальне середовище"
[ -x "$APP_DIR/.venv/bin/python" ] || sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
    install -m 600 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/.env.example" "$APP_DIR/.env"
    NEED_ENV=1
fi
chmod 600 "$APP_DIR/.env"

echo "== systemd"
install -m 644 "$APP_DIR/deploy/usousdt-bot.service" /etc/systemd/system/usousdt-bot.service
systemctl daemon-reload
systemctl enable -q usousdt-bot

echo "== Щоденна резервна копія бази (03:17 UTC)"
chmod +x "$APP_DIR/deploy/"*.sh
echo "17 3 * * * $APP_USER $APP_DIR/deploy/backup.sh >> $APP_DIR/logs/backup.log 2>&1" > /etc/cron.d/usousdt-bot
chmod 644 /etc/cron.d/usousdt-bot

if [ "${NEED_ENV:-0}" = 1 ]; then
    echo
    echo "Готово. Заповніть секрети і запустіть бота:"
    echo "  sudo nano $APP_DIR/.env"
    echo "  sudo systemctl start usousdt-bot"
else
    systemctl restart usousdt-bot
    echo "Бот перезапущено: sudo systemctl status usousdt-bot"
fi
