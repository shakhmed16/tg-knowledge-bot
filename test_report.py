# -*- coding: utf-8 -*-
"""Сводка владельцу за неделю.

    python test_report.py

Набиваем журнал похожей на настоящую неделей и смотрим, что отчёт считает
людей и заявки правильно, вытаскивает темы, показывает «чего нет в прайсе»
и не падает на пустой базе.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("BOT_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config
import db

config.DB_PATH = os.path.join(tempfile.mkdtemp(), "report.db")
db.init_db()

import report


def check(label, cond):
    print(f"{'OK  ' if cond else 'FAIL'} {label}")
    return cond


def at(days_ago: float, hour_local: int) -> str:
    """created_at в UTC для «столько-то дней назад, в такой-то местный час»."""
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    t = t.replace(hour=(hour_local - config.TIMEZONE_OFFSET) % 24, minute=13, second=0)
    return t.isoformat(timespec="seconds")


def log(uid, q, outcome, days_ago=1, hour=14):
    with db.connect() as c:
        c.execute(
            "INSERT INTO query_log (user_id, username, query, qa_id, score, used_ai,"
            " outcome, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, f"u{uid}", q, 1 if outcome == "base" else None, 0.9,
             int(outcome in ("ai", "noanswer")), outcome, at(days_ago, hour)),
        )
        c.execute(
            "INSERT OR IGNORE INTO clients (user_id, username, first_seen, last_seen)"
            " VALUES (?,?,?,?)", (uid, f"u{uid}", at(days_ago, hour), at(days_ago, hour)),
        )


def main() -> int:
    ok = True

    print("=== пустая база ===")
    text = report.build(7, 3, True)
    ok &= check("не падает и говорит, что писем не было", "никто не писал" in text)

    print("\n=== неделя как настоящая ===")
    week = [
        (1, "сколько стоит керамика на камри", "ai", 1, 20),
        (1, "а полировка перед керамикой", "ai", 1, 20),
        (2, "керамика на крету цена", "base", 2, 12),
        (3, "химчистка салона", "base", 2, 23),
        (4, "тонировка задних стёкол", "noanswer", 3, 19),
        (5, "тонировка задних стёкол сколько", "noanswer", 3, 11),
        (6, "оклейка фар плёнкой", "noanswer", 4, 2),
        (7, "керамика и химчистка на джипе", "ai", 4, 15),
        (8, "сколько стоит шаурма", "offtopic", 5, 13),
        (9, "привет", "smalltalk", 5, 9),
        (10, "полировка кузова", "base", 6, 18),
        (11, "[фото]", "media", 6, 18),
        (12, "[голосовое]", "voice", 6, 21),
        (12, "керамика на солярис", "ai", 6, 21),
        (13, "во сколько работаете", "ai", 0.5, 8),
    ]
    for uid, q, o, d, h in week:
        log(uid, q, o, d, h)
    # прошлая неделя — в отчёт попасть не должна
    log(99, "старый вопрос про керамику", "ai", 12, 12)
    # заявки: три, одна взята
    for i in range(3):
        lid = db.add_lead(i + 1, f"u{i+1}", "Имя", "+79000000000", "Camry", "керамика")
    db.set_lead_status(lid, "taken")

    text = report.build(7, 3, True)
    print(text)
    print()
    ok &= check("13 человек", "13 человек" in text)
    ok &= check("3 заявки, 1 взята", "Заявок на осмотр: 3" in text and "взяты в работу: 1" in text)
    ok &= check("керамика — главная тема", "керамика — 5" in text or "керамика — 6" in text)
    ok &= check("тонировка в «не смог ответить» с ×2", "тонировка" in text and "×2" in text)
    ok &= check("оклейка фар тоже там", "оклейка фар" in text)
    ok &= check("отсечено не по теме", "Не по теме и спам: 1" in text)
    ok &= check("голосовые посчитаны", "Голосовых и кружков: 1" in text)
    ok &= check("прошлая неделя не попала", "старый вопрос" not in text)
    ok &= check("вечерние/ночные посчитаны", "администратор не на месте" in text)

    print("\n=== топ тем без мусора ===")
    tp = dict(report.topics([
        "сколько стоит керамика", "керамику на камри хочу", "керамика цена",
        "здравствуйте, подскажите пожалуйста полировку", "полировка",
    ]))
    ok &= check("керамика 3, полировка 2", tp.get("керамика") == 3 and tp.get("полировка") == 2)
    ok &= check("«сколько», «стоит», «подскажите» не темы",
                not any(k in tp for k in ("сколько", "стоит", "подскажите", "пожалуйста")))

    print("\n=== детектор «нет в прайсе» ===")
    import bot
    ok &= check("ответ с «уточните у менеджера» — noanswer",
                bot.is_noanswer("Тонировка в каталоге отсутствует — уточните у менеджера."))
    ok &= check("обычная цена — не noanswer",
                not bot.is_noanswer("Toyota Camry — класс 2. Керамика — 27 000 ₽."))

    print("\n=== старый журнал без колонки outcome ===")
    old = os.path.join(tempfile.mkdtemp(), "old.db")
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE query_log (id INTEGER PRIMARY KEY, user_id INTEGER, username TEXT,"
                " query TEXT NOT NULL, qa_id INTEGER, score REAL, used_ai INTEGER NOT NULL DEFAULT 0,"
                " created_at TEXT NOT NULL)")
    con.execute("INSERT INTO query_log (user_id, query, used_ai, created_at) VALUES (1,'x',1,?)",
                (at(1, 12),))
    con.commit(); con.close()
    config.DB_PATH = old
    db.init_db()
    with db.connect() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(query_log)")}
    ok &= check("колонка outcome добавилась", "outcome" in cols)
    with db.connect() as c:
        c.execute("INSERT INTO query_log (user_id, query, qa_id, used_ai, created_at)"
                  " VALUES (2,'из базы',5,0,?)", (at(1, 12),))
    text = report.build(7, 3, True)
    ok &= check("отчёт по старой базе не падает", "Отчёт" in text)
    ok &= check("старые записи посчитаны как вопросы", "2 вопроса" in text)
    ok &= check("исход восстановлен из старых полей", "напрямую: 1 · через ИИ: 1" in text)

    print("\nИТОГ:", "всё зелёное" if ok else "есть падения")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
