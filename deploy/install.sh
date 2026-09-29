#!/usr/bin/env bash
# Установка бота на чистый Ubuntu/Debian сервер. Запускать от root:
#
#     bash deploy/install.sh
#
# Скрипт идемпотентный: повторный запуск обновляет зависимости и юниты,
# не трогая .env и базу знаний.
set -euo pipefail

APP_DIR="/opt/kb-bot"
APP_USER="kbbot"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Запустите от root: sudo bash deploy/install.sh" >&2
    exit 1
fi

echo "==> Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sqlite3 ca-certificates

echo "==> Пользователь $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "==> Файлы в $APP_DIR"
mkdir -p "$APP_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
    # .env и базу не перезаписываем — они живут на сервере.
    # Копируем весь код и прайсы: список модулей растёт, перечислять их
    # по одному — верный способ забыть новый.
    for f in "$SRC_DIR"/*.py "$SRC_DIR"/*.csv "$SRC_DIR"/*.md \
             "$SRC_DIR/requirements.txt" "$SRC_DIR/.env.example"; do
        [[ -f "$f" ]] && install -m 0644 "$f" "$APP_DIR/$(basename "$f")"
    done
    # тестовые аудиофайлы — для test_voice.py
    if [[ -d "$SRC_DIR/voice" ]]; then
        mkdir -p "$APP_DIR/voice"
        install -m 0644 "$SRC_DIR/voice/"*.ogg "$SRC_DIR/voice/"*.mp4 "$APP_DIR/voice/" 2>/dev/null || true
    fi
    mkdir -p "$APP_DIR/deploy"
    install -m 0644 "$SRC_DIR/deploy/"*.service "$SRC_DIR/deploy/"*.timer "$APP_DIR/deploy/"
    for s in backup.sh update.sh install.sh; do
        [[ -f "$SRC_DIR/deploy/$s" ]] && install -m 0755 "$SRC_DIR/deploy/$s" "$APP_DIR/deploy/$s"
    done
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    NEED_ENV=1
fi
chmod 600 "$APP_DIR/.env"

echo "==> Виртуальное окружение"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip -q
"$APP_DIR/.venv/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"

mkdir -p "$APP_DIR/backups"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> systemd"
install -m 0644 "$APP_DIR/deploy/kb-bot.service" /etc/systemd/system/kb-bot.service
install -m 0644 "$APP_DIR/deploy/kb-bot-backup.service" /etc/systemd/system/kb-bot-backup.service
install -m 0644 "$APP_DIR/deploy/kb-bot-backup.timer" /etc/systemd/system/kb-bot-backup.timer
systemctl daemon-reload
systemctl enable --now kb-bot-backup.timer

if [[ "${NEED_ENV:-0}" == "1" ]]; then
    cat <<'MSG'

=====================================================================
 Почти готово. Осталось вписать токены:

     nano /opt/kb-bot/.env

 Затем запустить бота:

     systemctl enable --now kb-bot
     journalctl -u kb-bot -f
=====================================================================
MSG
else
    systemctl enable kb-bot
    systemctl restart kb-bot
    sleep 2
    systemctl --no-pager --lines=15 status kb-bot || true
    echo
    echo "Готово. Логи: journalctl -u kb-bot -f"
fi
