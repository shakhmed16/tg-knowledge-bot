# -*- coding: utf-8 -*-
"""Проверка памяти диалога: переживает ли контекст перезапуск бота.

    python test_dialog.py

Главная проверка — последняя: уточняющий вопрос «а если ещё химчистку»
должен пониматься после того, как процесс бота был перезапущен. Раньше
история жила в словаре в памяти и терялась вместе с процессом.
"""
from __future__ import annotations

import asyncio
import csv
import os
import sqlite3
import sys
import tempfile

os.environ.setdefault("BOT_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config

config.STRICT_CATALOG = True
config.DIALOG_TTL_HOURS = 6
config.FALLBACK_CONTACT = "Менеджер: +7 926 107-21-20"
config.BUSINESS_INFO = "детейлинг-студия"

import db

config.DB_PATH = os.path.join(tempfile.mkdtemp(), "dlg.db")
db.init_db()

import ai

USER = 900


def check(label, cond):
    print(f"{'OK  ' if cond else 'FAIL'} {label}")
    return cond


async def main() -> int:
    ok = True

    print("=== хранение и подрезка ===")
    for i in range(10):
        db.add_dialog(USER, "user", f"вопрос {i}")
        db.add_dialog(USER, "assistant", f"ответ {i}")
    rows = db.dialog(USER)
    ok &= check("держим ровно 6 последних реплик", len(rows) == 6)
    ok &= check("порядок хронологический", rows[0]["content"] == "вопрос 7")
    n = sqlite3.connect(config.DB_PATH).execute(
        "SELECT COUNT(*) FROM dialog"
    ).fetchone()[0]
    ok &= check("таблица не растёт (хвост режется при записи)", n == 6)
    ok &= check("последний вопрос находится", db.last_question(USER) == "вопрос 9")

    print("\n=== протухание контекста ===")
    c = sqlite3.connect(config.DB_PATH)
    c.execute(
        "UPDATE dialog SET created_at = "
        "strftime('%Y-%m-%dT%H:%M:%S','now','-10 hours')||'+00:00'"
    )
    c.commit(); c.close()
    ok &= check("разговор 10-часовой давности не берётся в контекст",
                db.dialog(USER, ttl_hours=6) == [])
    ok &= check("но для карточки заявки он ещё виден (сутки)",
                db.last_question(USER, ttl_hours=24) == "вопрос 9")

    print("\n=== пустое и мусорное не пишем ===")
    before = sqlite3.connect(config.DB_PATH).execute(
        "SELECT COUNT(*) FROM dialog"
    ).fetchone()[0]
    db.add_dialog(USER, "user", "")
    db.add_dialog(USER, "user", "   ")
    after = sqlite3.connect(config.DB_PATH).execute(
        "SELECT COUNT(*) FROM dialog"
    ).fetchone()[0]
    ok &= check("пустые реплики не сохраняются", before == after)

    print("\n=== главное: уточнение после «перезапуска» ===")
    if not config.AI_API_KEY:
        print("SKIP AI_API_KEY не задан — пропускаю живую проверку")
        print("\n" + "=" * 58)
        print("Все прошли." if ok else "ЕСТЬ ПРОВАЛЫ")
        return 0 if ok else 1

    # чистая база диалога, грузим прайс
    sqlite3.connect(config.DB_PATH).execute("DELETE FROM dialog").connection.commit()
    with open("price_detailing_demo.csv", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            db.add_qa(r["question"].strip(), r["answer"].strip(), "import")
    catalog = [(r["question"], r["answer"]) for r in db.catalog(200)]

    q1 = "у меня камри, сколько керамика"
    a1 = await ai.ask(q1, None, db.dialog(USER), catalog)
    db.add_dialog(USER, "user", q1)
    db.add_dialog(USER, "assistant", a1)
    print(f"\nВ: {q1}\nО: {a1.strip()[:200]}")

    # здесь процесс бота «перезапустился»: ничего в памяти не осталось,
    # историю берём заново из базы
    history = db.dialog(USER)
    q2 = "а если ещё химчистку салона"
    a2 = await ai.ask(q2, None, history, catalog)
    print(f"\nВ: {q2}\nО: {a2.strip()[:300]}")

    def norm(t):
        for ch in (" ", " ", " "):
            t = t.replace(ch, " ")
        return t

    body = norm(a2)
    ok &= check("помнит машину: посчитал по классу 2 (16 000 за химчистку)",
                "16 000" in body)
    ok &= check("не переспрашивает марку",
                "какая у вас" not in body.lower() and "назовите марку" not in body.lower())

    await ai.close()
    print("\n" + "=" * 58)
    print("Все прошли." if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
