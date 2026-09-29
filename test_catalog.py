# -*- coding: utf-8 -*-
"""Прогон диалога клиента по прайсу: что бот ответит на типовые вопросы.

    python test_catalog.py

Импортирует CSV во временную базу и проходит по списку вопросов так же, как
это делает бот: сначала поиск, потом переспрос, потом ИИ со всем каталогом.
"""
from __future__ import annotations

import asyncio
import csv
import os
import shutil
import tempfile

import ai
import config
import db

CSV = "price_detailing_demo.csv"

# (вопрос, что должно быть в ответе, чего быть НЕ должно)
CASES = [
    ("сколько стоит полная химчистка салона", ["12 000", "16 000", "22 000"], []),
    ("химчистка салона на кроссовере сколько", ["16 000"], []),
    ("сколько держится ваша керамика", [], ["года", "лет"]),
    ("какие материалы вы используете для керамики", [], []),
    ("сколько стоит керамика", ["22 000"], []),
    ("полировка и химчистка на джипе сколько выйдет", ["22 000", "44 000"], []),
    ("а вы моете двигатель отдельно?", [], ["₽"]),
    ("сколько стоит перетянуть салон в алькантару", [], ["₽"]),
    ("чем керамика отличается от воска", [], []),
    ("сколько стоит снять тонировку с лобового", ["2 000"], []),
    ("почем убрать вмятину", ["2 000"], []),
    ("сколько будет мойка и чернение шин на седане бизнес класса", ["1 200", "600"], []),
]


def load_csv(path: str) -> int:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    for r in rows:
        db.add_qa(r["question"].strip(), r["answer"].strip(), "import")
    return len(rows)


def norm(t: str) -> str:
    """Модели ставят между цифрами неразрывные и узкие пробелы."""
    for ch in ("\u00a0", "\u202f", "\u2009", "\u2007"):
        t = t.replace(ch, " ")
    return t


async def main() -> None:
    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "price.db")
    config.STRICT_CATALOG = True
    config.FALLBACK_CONTACT = "менеджер: +7 900 000-00-00"
    config.BUSINESS_INFO = "детейлинг-студия: мойка, химчистка, полировка, керамика, оклейка"
    db.init_db()
    n = load_csv(CSV)
    print(f"В базе {n} записей\n" + "=" * 70)

    rows = db.catalog(config.CATALOG_MAX_ROWS)
    catalog = [(r["question"], r["answer"]) for r in rows]

    problems = 0
    for query, must, must_not in CASES:
        hits = db.search(query, None, 3)
        best = hits[0] if hits else None
        source = "ИИ+каталог"
        answer = None

        direct_min = max(config.MATCH_THRESHOLD, config.STRICT_DIRECT_MIN)
        if best and best.score >= direct_min:
            answer, source = best.answer, f"база [{best.id}] {best.score:.2f}"
        if answer is None:
            answer = await ai.ask(query, None, None, catalog)

        na = norm(answer)
        miss = [m for m in must if norm(m) not in na]
        extra = [m for m in must_not if norm(m) in na]
        mark = "OK  "
        if miss or extra:
            mark = "!!  "
            problems += 1

        print(f"\n{mark}[{source}] {query}")
        print(f"    {answer.strip()[:400]}")
        if miss:
            print(f"    ^^ нет ожидаемого: {miss}")
        if extra:
            print(f"    ^^ есть лишнее: {extra}")

    print("\n" + "=" * 70)
    print(f"Проблемных ответов: {problems} из {len(CASES)}")
    await ai.close()
    shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(main())
