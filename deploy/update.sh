#!/usr/bin/env bash
# Обновление кода на сервере. Порядок такой:
#   1. закинули новые .py в /opt/kb-bot (WinSCP, scp, git pull — неважно)
#   2. bash /opt/kb-bot/deploy/update.sh
set -euo pipefail

APP_DIR="/opt/kb-bot"

if [[ $EUID -ne 0 ]]; then
    echo "Запустите от root: sudo bash $APP_DIR/deploy/update.sh" >&2
    exit 1
fi

echo "==> Бэкап базы перед обновлением"
sudo -u kbbot "$APP_DIR/deploy/backup.sh" || echo "  (базы ещё нет — пропускаю)"

echo "==> Зависимости"
"$APP_DIR/.venv/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"

echo "==> Права"
chown -R kbbot:kbbot "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Перезапуск"
systemctl restart kb-bot
sleep 2
systemctl --no-pager --lines=20 status kb-bot
