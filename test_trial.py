# -*- coding: utf-8 -*-
"""Пробный период и приветствие с именем студии.

    python test_trial.py

Проверяем: дата из .env и её продление командой, напоминания за N дней без
повторов, остановка ответов клиентам после даты (админ проходит), итоги
владельцу в день окончания, приветствие.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import timedelta

os.environ.setdefault("BOT_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config

config.ADMIN_IDS = {555}
config.LEAD_CAPTURE = True
config.STRICT_CATALOG = True
config.STUDIO_NAME = "Detailing Pro"
config.LEAD_CHAT_ID = "-100777"
config.TRIAL_WARN_DAYS = [3, 1]

import db

config.DB_PATH = os.path.join(tempfile.mkdtemp(), "trial.db")
db.init_db()

import bot
import trial


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = f"u{uid}"


class FakeMessage:
    def __init__(self, uid, text=""):
        self.from_user = FakeUser(uid)
        self.text = text
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append(text)
        return self


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def check(label, cond):
    print(f"{'OK  ' if cond else 'FAIL'} {label}")
    return cond


async def main() -> int:
    ok = True
    today = trial.today()

    print("=== приветствие ===")
    ok &= check("с именем студии", "студии Detailing Pro" in bot.greeting_text())
    config.STUDIO_NAME = ""
    ok &= check("без имени — нейтральное", "Здравствуйте! Я подскажу" in bot.greeting_text())

    print("\n=== без даты ===")
    config.TRIAL_UNTIL = ""
    ok &= check("активен", trial.active() and trial.days_left() is None)
    ok &= check("напоминаний нет", trial.pending_warning() is None)

    print("\n=== дата из .env ===")
    config.TRIAL_UNTIL = (today + timedelta(days=5)).isoformat()
    ok &= check("5 дней осталось", trial.days_left() == 5 and trial.active())
    ok &= check("за 5 дней не напоминаем", trial.pending_warning() is None)

    print("\n=== за 3 дня — напоминание админу, один раз ===")
    config.TRIAL_UNTIL = (today + timedelta(days=3)).isoformat()
    fb = FakeBot()
    n = await bot.trial_watch(fb)
    ok &= check("ушло админу", n == 1 and fb.sent and fb.sent[0][0] == 555 and "осталось 3" in fb.sent[0][1])
    ok &= check("владельцу не ушло", all(c != -100777 for c, _ in fb.sent))
    n2 = await bot.trial_watch(FakeBot())
    ok &= check("повторно не шлём", n2 == 0)

    print("\n=== продление командой ===")
    m = FakeMessage(555, "/trial +7")
    await bot.cmd_trial(m)
    ok &= check("продлили на 7 от даты окончания", trial.days_left() == 10 and "осталось 10" in m.sent[-1])
    m = FakeMessage(555, "/trial")
    await bot.cmd_trial(m)
    ok &= check("статус показывает базу, а не .env", "10 дн" in m.sent[-1])
    m = FakeMessage(777, "/trial +7")
    await bot.cmd_trial(m)
    ok &= check("не админу — молчим", not m.sent)
    m = FakeMessage(555, "/trial вчера")
    await bot.cmd_trial(m)
    ok &= check("кривую дату отбили", "Не понял" in m.sent[-1])

    print("\n=== истёк ===")
    trial.set_until((today - timedelta(days=1)).isoformat())
    ok &= check("не активен", not trial.active())
    fb = FakeBot()
    n = await bot.trial_watch(fb)
    owner = [t for c, t in fb.sent if c == -100777]
    admin = [t for c, t in fb.sent if c == 555]
    ok &= check("владельцу — итоги", owner and "завершён" in owner[0] and "Отчёт" in owner[0])
    ok &= check("админу — стоп и подсказка продлить", admin and "/trial +7" in admin[0])
    ok &= check("повторно не шлём", await bot.trial_watch(FakeBot()) == 0)

    print("\n=== заглушка клиентам ===")
    gate = bot.TrialGate()
    passed = []

    async def handler(event, data):
        passed.append(event.from_user.id)

    m = FakeMessage(1, "сколько керамика")
    await gate(handler, m, {})
    ok &= check("клиенту — заглушка, до обработчика не дошло", m.sent == [config.TRIAL_OVER_TEXT] and not passed)
    m2 = FakeMessage(1, "ещё раз")
    await gate(handler, m2, {})
    ok &= check("повторно в течение часа молчим", not m2.sent)
    a = FakeMessage(555, "/settings")
    await gate(handler, a, {})
    ok &= check("админ проходит", passed == [555] and not a.sent)

    print("\n=== продлили — снова работает ===")
    m = FakeMessage(555, "/trial +7")
    await bot.cmd_trial(m)
    ok &= check("7 дней от сегодня, а не от истёкшей даты", trial.days_left() == 7)
    passed.clear()
    m = FakeMessage(2, "сколько керамика")
    await gate(handler, m, {})
    ok &= check("клиент проходит", passed == [2] and not m.sent)
    m = FakeMessage(555, "/trial off")
    await bot.cmd_trial(m)
    ok &= check("снятие", trial.days_left() is None and "без ограничения" in m.sent[-1])

    print("\nИТОГ:", "всё зелёное" if ok else "есть падения")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
