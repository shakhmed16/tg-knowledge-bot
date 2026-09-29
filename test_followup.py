# -*- coding: utf-8 -*-
"""Проверка дожима, напоминаний и памяти о клиенте.

    python test_followup.py

Telegram не нужен: бот подставной, время «прокручивается» правкой дат в базе.
Самое важное здесь — не то, что бот пишет, а то, что он НЕ пишет: повторно,
своим, ночью и тем, кто уже записался.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile

os.environ.setdefault("BOT_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config

config.LEAD_CAPTURE = True
config.FOLLOWUP_ENABLED = True
config.ADMIN_IDS = {555}
config.LEAD_CHAT_ID = ""
config.NUDGE_AFTER_HOURS = 3
config.NUDGE_WINDOW_HOURS = 48
config.LEAD_REMIND_AFTER_MIN = 120
config.QUIET_FROM_HOUR = config.QUIET_TO_HOUR = 0  # в тесте пишем круглосуточно

import db

config.DB_PATH = os.path.join(tempfile.mkdtemp(), "f.db")
db.init_db()

import bot


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def rewind(table: str, hours: float, where: str = "1=1") -> None:
    """Сдвинуть даты назад — как будто прошло время."""
    c = sqlite3.connect(config.DB_PATH)
    c.execute(
        f"UPDATE {table} SET created_at = "
        f"strftime('%Y-%m-%dT%H:%M:%S', 'now', '-{hours} hours')||'+00:00' WHERE {where}"
    )
    c.commit()
    c.close()


def check(label, cond):
    print(f"{'OK  ' if cond else 'FAIL'} {label}")
    return cond


async def main() -> int:
    ok = True

    # 101 — спросил и пропал; 102 — спросил и записался; 555 — админ (это мы)
    db.log_query(101, "vasya", "сколько керамика на камри", None, 0.0, True)
    db.log_query(102, "petya", "полировка на джипе", None, 0.0, True)
    db.log_query(555, "boss", "тест", None, 0.0, True)
    db.add_lead(102, "petya", "Пётр", "+79001112233", "Jeep", "полировка", "суббота")
    rewind("query_log", 4)
    rewind("leads", 3)

    print("=== дожим клиентов ===")
    fake = FakeBot()
    sent = await bot.nudge_clients(fake)
    ids = [c for c, _ in fake.sent]
    ok &= check("написали тому, кто пропал", 101 in ids)
    ok &= check("не написали тому, кто записался", 102 not in ids)
    ok &= check("не написали админу", 555 not in ids)
    ok &= check("ровно одно сообщение", sent == 1)

    print("\n=== повторно не пишем ===")
    fake2 = FakeBot()
    again = await bot.nudge_clients(fake2)
    ok &= check("второй раз молчим", again == 0)

    print("\n=== напоминание владельцу о висящей заявке ===")
    fake3 = FakeBot()
    reminded = await bot.remind_owner(fake3)
    ok &= check("напоминание ушло админу", [c for c, _ in fake3.sent] == [555])
    ok &= check("в тексте имя клиента", "Пётр" in fake3.sent[0][1] if fake3.sent else False)
    ok &= check("есть ссылка на клиента", "tg://user?id=102" in fake3.sent[0][1] if fake3.sent else False)

    print("\n=== «взял в работу» прекращает напоминания ===")
    fake4 = FakeBot()
    ok &= check("повторно не напоминаем", await bot.remind_owner(fake4) == 0)
    db.set_lead_status(1, "new")               # вернём как было
    c = sqlite3.connect(config.DB_PATH)        # и снимем отметку об отправке
    c.execute("DELETE FROM followups WHERE kind='lead_remind'")
    c.commit(); c.close()
    db.set_lead_status(1, "taken")
    fake5 = FakeBot()
    ok &= check("после отметки — тишина", await bot.remind_owner(fake5) == 0)

    print("\n=== вернувшийся клиент (тот самый баг) ===")
    # Порог дожима в тесте — 3 часа, поэтому все вопросы ставим старше него.
    def at(user_id: int, query: str, hours_ago: float) -> None:
        db.log_query(user_id, "u", query, None, 0.0, True)
        c = sqlite3.connect(config.DB_PATH)
        c.execute(
            "UPDATE query_log SET created_at = "
            "strftime('%Y-%m-%dT%H:%M:%S','now',?)||'+00:00'"
            " WHERE user_id=? AND query=?",
            (f"-{hours_ago} hours", user_id, query),
        )
        c.commit(); c.close()

    # 201 записался позавчера, а вчера снова спросил — это новый интерес
    db.add_lead(201, "ilya", "Илья", "+79005556677", "Opel", "керамика", "суббота")
    rewind("leads", 30, "user_id=201")
    at(201, "керамика опель", 6)
    fake6 = FakeBot()
    await bot.nudge_clients(fake6)
    ok &= check("дожали того, кто спросил ПОСЛЕ старой заявки",
                201 in [c for c, _ in fake6.sent])

    # спросил ДО заявки — значит записался, дожимать незачем
    at(202, "полировка", 5)
    db.add_lead(202, "oleg", "Олег", "+79001110000", "Kia", "полировка", "завтра")
    fake7 = FakeBot()
    await bot.nudge_clients(fake7)
    ok &= check("не дожали того, кто записался после вопроса",
                202 not in [c for c, _ in fake7.sent])

    # после нашего дожима новых вопросов нет — молчим
    fake8 = FakeBot()
    await bot.nudge_clients(fake8)
    ok &= check("после дожима без новых вопросов — тишина",
                201 not in [c for c, _ in fake8.sent])

    # написал снова уже после дожима — дожимаем во второй раз
    rewind("followups", 5, "user_id=201 AND kind='nudge'")
    at(201, "а сколько полировка", 4)
    fake9 = FakeBot()
    await bot.nudge_clients(fake9)
    ok &= check("новый вопрос после дожима — дожимаем снова",
                201 in [c for c, _ in fake9.sent])

    print("\n=== древние заявки не поднимаем ===")
    db.add_lead(203, "old", "Древний", "+79000000000", "", "", "")
    rewind("leads", 200, "user_id=203")   # 8 дней назад
    fake10 = FakeBot()
    await bot.remind_owner(fake10)
    ok &= check("заявка недельной давности не будит владельца",
                not any("Древний" in t for _, t in fake10.sent))

    print("\n=== тихие часы ===")
    config.QUIET_FROM_HOUR, config.QUIET_TO_HOUR = 0, 24
    ok &= check("ночью не пишем", bot.quiet_hours())
    config.QUIET_FROM_HOUR = config.QUIET_TO_HOUR = 0
    ok &= check("круглосуточно — пишем", not bot.quiet_hours())

    print("\n=== память о клиенте ===")
    known = db.get_client(102)
    ok &= check("клиент запомнен после заявки", known is not None)
    db.remember_client(102, "petya", name="Пётр", phone="+79001112233", car="Jeep")
    known = db.get_client(102)
    ok &= check("машина сохранена", known["car"] == "Jeep")
    db.remember_client(102, "petya")           # пустой вызов не должен затирать
    ok &= check("пустое обновление не стёрло машину", db.get_client(102)["car"] == "Jeep")

    print("\n" + "=" * 58)
    print("Все прошли." if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
