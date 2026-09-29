"""Офлайн-проверка качества поиска.

Строит временную базу знаний, прогоняет набор запросов и показывает, что бот
сделал бы с каждым: ответил из базы, переспросил модель или ушёл в ИИ.
Нужен, чтобы менять пороги и формулу ранжирования, не гоняя живой бот.

    python test_search.py            # с переспросом (нужен AI_API_KEY)
    python test_search.py --offline  # только лексический поиск, без сети

Свои вопросы добавляйте в KB и TESTS ниже: во втором элементе TESTS — номер
записи из KB (нумерация с 1) или None, если бот ДОЛЖЕН уйти в ИИ.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import ai
import config
import db

# --- записи базы знаний -------------------------------------------------- #
KB = [
    ("сколько дней в году", "Обычно в году 365 дней, а в високосном — 366."),
    ("сколько месяцев в году?", "В году 12 месяцев."),
    ("Пью пиво El capulco сколько в нем оборотов?", "Зависит от вида, обычно 4-5%."),
    ("как вернуть товар", "Вернуть товар можно в течение 14 дней с чеком."),
    ("сколько км от Москвы до Питера", "Около 700 км по трассе М-11."),
    ("как оформить возврат денег", "Деньги возвращаются на карту за 10 рабочих дней."),
    ("какой график работы", "Пн-Пт с 9:00 до 18:00, выходные — суббота и воскресенье."),
    ("как связаться с поддержкой", "Напишите на support@example.com или 8-800-000-00-00."),
]

# --- (запрос, номер записи в KB или None = должен уйти в ИИ) -------------- #
TESTS = [
    ("сколько дней в году", 1),
    ("Сколько дней в году?", 1),
    ("сколько месяцев в году", 2),
    ("Сколько в году месяцев ?", 2),
    ("сколько км от мск до питера", 5),
    ("Сколько км от Москвы до Питера ?", 5),
    ("Ответь на вопрос сколько км от мск до питера", 5),
    ("как вернуть покупку", 4),
    ("хочу вернуть товар обратно", 4),
    ("во сколько вы работаете", 7),
    ("телефон поддержки", 8),
    # ниже — запросы, на которые ответа в базе НЕТ
    ("Сколько ?", None),
    ("Количество", None),
    ("Кто", None),
    ("нет", None),
    ("Кто такой ванёк грачев?", None),
    ("Как тебя зовут ?", None),
    ("Количество км от Москвы до Пушкино ?", None),
    ("сколько недель в году", None),
    ("сколько стоит доставка", None),
    ("какая столица Франции", None),
    ("сколько оборотов в водке", None),
]


async def main(offline: bool) -> int:
    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "test.db")
    db.init_db()
    ids = {i: db.add_qa(q, a, "import") for i, (q, a) in enumerate(KB, 1)}

    ok = wrong = missed = calls = 0
    rows = []
    for query, expected in TESTS:
        hits = db.search(query, None, 3)
        best = hits[0] if hits else None
        got, how = None, "ИИ"

        if best and best.score >= config.MATCH_THRESHOLD:
            got, how = best.id, "база"
        elif not offline and best and best.score >= config.RERANK_MIN_SCORE:
            calls += 1
            picked = await ai.rerank(query, [(h.id, h.question, h.answer) for h in hits[:3]])
            if picked:
                got, how = picked, "переспрос"

        want = ids.get(expected) if expected else None
        if got == want:
            verdict = "OK  "; ok += 1
        elif want is None or got is not None:
            verdict = "ЛОЖЬ"; wrong += 1
        else:
            verdict = "МИМО"; missed += 1
        top = f"[{best.id}] {best.question[:32]}" if best else "-"
        rows.append((verdict, best.score if best else 0.0, how, query, top))

    print(
        f"порог {config.MATCH_THRESHOLD} | серая зона от {config.RERANK_MIN_SCORE}"
        f" | переспрос: {'выкл' if offline else config.AI_RERANK_MODEL}"
    )
    print(f"OK {ok} | ложных {wrong} | промахов {missed} из {len(TESTS)} | переспросов {calls}\n")
    for verdict, score, how, query, top in rows:
        print(f"{verdict} {score:.2f} {how:<9} {query[:42]:<42} -> {top}")

    await ai.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return wrong + missed


if __name__ == "__main__":
    sys.exit(min(asyncio.run(main("--offline" in sys.argv)), 1))
