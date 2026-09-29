"""Пробный период: дата окончания, напоминания и остановка.

Зачем это в боте, а не в голове. Семь дней бесплатно — это обещание, у
которого должен быть конец, и конец не должен зависеть от того, вспомнил
ли кто-то позвонить. Бот сам напоминает нам за несколько дней, а в день
окончания шлёт владельцу итоги и перестаёт отвечать клиентам. Разговор о
подписке тогда начинают они.

Дата берётся из .env (TRIAL_UNTIL), но командой /trial её можно сдвинуть —
это запоминается в базе и перекрывает .env. Так продление не требует
лезть в файл на сервере.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import config
import db

KEY = "trial_until"


def until() -> date | None:
    """Действующая дата окончания: из базы, если меняли командой, иначе из .env."""
    raw = db.get_setting(KEY)
    if raw is None:
        raw = config.TRIAL_UNTIL
    raw = (raw or "").strip()
    if not raw or raw.lower() in ("off", "none", "0"):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def today() -> date:
    tz = timezone(timedelta(hours=config.TIMEZONE_OFFSET))
    return datetime.now(tz).date()


def days_left() -> int | None:
    """Сколько дней осталось; 0 — сегодня последний; отрицательное — истёк."""
    u = until()
    return None if u is None else (u - today()).days


def active() -> bool:
    """Можно ли отвечать клиентам."""
    d = days_left()
    return d is None or d >= 0


def set_until(value: str | None) -> date | None:
    """Задать дату: 'YYYY-MM-DD', '+N' (дней от текущей даты окончания или
    от сегодня), 'off'. Возвращает новую дату или None."""
    if value is None or value.lower() in ("off", "none", "стоп"):
        db.set_setting(KEY, "off")
        return None
    value = value.strip()
    if value.startswith("+") and value[1:].isdigit():
        base = until() or today()
        if base < today():
            base = today()
        new = base + timedelta(days=int(value[1:]))
    else:
        new = date.fromisoformat(value)   # ValueError — пусть ловит вызывающий
    db.set_setting(KEY, new.isoformat())
    return new


def status_line() -> str:
    d = days_left()
    if d is None:
        return "Пробный период: не задан (бот работает без ограничения)"
    u = until().isoformat()
    if d > 1:
        return f"Пробный период: до {u}, осталось {d} дн."
    if d == 1:
        return f"Пробный период: до {u}, завтра последний день"
    if d == 0:
        return f"Пробный период: до {u}, сегодня последний день"
    return f"Пробный период: истёк {u} ({-d} дн. назад) — клиентам не отвечаем"


def pending_warning() -> str | None:
    """Ключ напоминания на сегодня, если его ещё не отправляли: 'warn:3',
    'warn:1', 'over'. None — сегодня ничего не нужно."""
    d = days_left()
    if d is None:
        return None
    if d < 0:
        key = "over"
    elif d in config.TRIAL_WARN_DAYS:
        key = f"warn:{d}"
    else:
        return None
    if db.get_setting(f"trial_sent:{key}") == until().isoformat():
        return None
    return key


def mark_sent(key: str) -> None:
    db.set_setting(f"trial_sent:{key}", until().isoformat())
