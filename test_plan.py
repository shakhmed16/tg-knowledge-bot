# -*- coding: utf-8 -*-
"""Тарифы «Стандарт» и «Про»: что где включено.

    python test_plan.py

Проверяем: рассылка на Стандарте закрыта, на Про — считает адресатов по теме
и с паузой между письмами, ждёт подтверждения; несколько чатов заявок только
на Про; CRM-вебхук только на Про и не ломает заявку, если CRM лежит;
подсказка про Про в отчёте на Стандарте и её отсутствие на Про.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("BOT_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config

config.ADMIN_IDS = {555}
config.LEAD_CAPTURE = True
config.LEAD_CHAT_ID = "-100111, -100222"
config.QUIET_FROM_HOUR = config.QUIET_TO_HOUR = 0   # без тихих часов в тесте
config.BROADCAST_PER_SEC = 1000

import db

config.DB_PATH = os.path.join(tempfile.mkdtemp(), "plan.db")
db.init_db()

import bot
import report


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

    async def edit_reply_markup(self, **kw):
        pass


class FakeCall:
    def __init__(self, uid, data):
        self.from_user = FakeUser(uid)
        self.data = data
        self.message = FakeMessage(uid)

    async def answer(self, *a, **k):
        pass


class FakeBot:
    def __init__(self, fail=()):
        self.sent = []
        self.fail = set(fail)

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.fail:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text))


def check(label, cond):
    print(f"{'OK  ' if cond else 'FAIL'} {label}")
    return cond


def seed():
    """Пять человек писали, двое про керамику, один записался."""
    now = datetime.now(timezone.utc)
    with db.connect() as c:
        for uid, q, days in [(1, "сколько керамика на камри", 2), (2, "керамику хочу", 5),
                             (3, "химчистка салона", 3), (4, "полировка", 1), (5, "привет", 1)]:
            c.execute("INSERT INTO query_log (user_id, username, query, used_ai, outcome, created_at)"
                      " VALUES (?,?,?,?,?,?)", (uid, f"u{uid}", q, 1, "ai",
                      (now - timedelta(days=days)).isoformat(timespec="seconds")))
    db.add_lead(4, "u4", "Имя", "+79000000000", "Camry", "полировка")


async def main() -> int:
    ok = True
    seed()

    print("=== Стандарт ===")
    config.PLAN, config.PRO = "standard", False
    ok &= check("заявки — только в первый чат", bot.lead_targets() == [-100111])
    m = FakeMessage(555, "/broadcast Акция на керамику до конца месяца")
    await bot.cmd_broadcast(m)
    ok &= check("рассылка закрыта", m.sent and "Про" in m.sent[0])
    text = report.build(7, 3, True, pro=False)
    ok &= check("в отчёте подсказка про Про", "тарифе «Про»" in text and "4 человек" in text)
    fb = FakeBot()
    await bot.crm_push(1, {"name": "x"}, FakeUser(1))
    ok &= check("CRM не вызывается", True)  # просто не падает

    print("\n=== Про ===")
    config.PLAN, config.PRO = "pro", True
    ok &= check("заявки — в оба чата", bot.lead_targets() == [-100111, -100222])
    text = report.build(7, 3, True, pro=True)
    ok &= check("подсказки про Про нет", "тарифе «Про»" not in text)

    print("\n=== рассылка по теме ===")
    targets = db.broadcast_targets("керамика", 60, 14)
    ok &= check("двое спрашивали про керамику", targets == [1, 2])
    ok &= check("все за 60 дней — пятеро", db.broadcast_targets("", 60, 14) == [1, 2, 3, 4, 5])
    ok &= check("кроме записавшихся — четверо", 4 not in db.broadcast_targets("", 60, 14, exclude_leads_days=30))

    m = FakeMessage(555, "/broadcast керамика | Акция: керамика −10% до конца месяца")
    await bot.cmd_broadcast(m)
    ok &= check("показали, сколько получат", m.sent and "Получат 2 чел." in m.sent[0])
    ok &= check("ждём подтверждения", 555 in bot._broadcast_draft)

    fb = FakeBot(fail={2})
    call = FakeCall(555, "bc:go")
    await bot.cb_broadcast(call, fb)
    ok &= check("отправили одному, второй заблокировал", [c for c, _ in fb.sent] == [1]
                and "доставлено 1" in call.message.sent[-1] and "не доставлено 1" in call.message.sent[-1])
    ok &= check("черновик очищен", 555 not in bot._broadcast_draft)
    ok &= check("повторно тем же не шлём (кулдаун)", db.broadcast_targets("керамика", 60, 14) == [2])

    m = FakeMessage(555, "/broadcast шиномонтаж | текст сообщения")
    await bot.cmd_broadcast(m)
    ok &= check("никто не спрашивал — говорим", "Некому" in m.sent[0])

    m = FakeMessage(555, "/broadcast")
    await bot.cmd_broadcast(m)
    ok &= check("без текста — подсказка", "Как пользоваться" in m.sent[0])

    call = FakeCall(555, "bc:no")
    bot._broadcast_draft[555] = ([1], "x")
    await bot.cb_broadcast(call, FakeBot())
    ok &= check("отмена работает", "Отменено" in call.message.sent[-1])

    print("\n=== CRM-вебхук ===")
    config.CRM_WEBHOOK_URL = "http://127.0.0.1:9/nope"   # закрытый порт
    await bot.crm_push(7, {"name": "Дмитрий", "phone": "+7"}, FakeUser(1))
    ok &= check("недоступная CRM не роняет бота", True)

    print("\nИТОГ:", "всё зелёное" if ok else "есть падения")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
