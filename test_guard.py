# -*- coding: utf-8 -*-
"""Проверка фильтра «не по теме» и лимита обращений.

    python test_guard.py

Цена ошибки несимметрична: пропустить лишний вопрос — потерять копейки,
отсечь настоящего клиента — потерять клиента. Поэтому в наборе много
кривых, коротких и разговорных формулировок, которые всё равно про услуги.
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile

os.environ.setdefault("BOT_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config
import db

config.STRICT_CATALOG = True
config.TOPIC_GUARD = True
config.RATE_LIMIT_PER_HOUR = 3
config.ADMIN_IDS = set()

tmp = tempfile.mkdtemp()
config.DB_PATH = os.path.join(tmp, "guard.db")
db.init_db()
with open("price_detailing_demo.csv", encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f, delimiter=";"):
        db.add_qa(r["question"].strip(), r["answer"].strip(), "import")

import bot  # после настройки config

# Должны ПРОЙТИ к модели — это клиенты, пусть и косноязычные
ON_TOPIC = [
    "у меня камри, сколько керамика",
    "почем полирнуть машину",
    "сколько будет химчистка на джипе",
    "хочу бронепленку на фары",
    "а тонировку снять сколько",
    "что входит в предпродажную подготовку",
    "шумка дверей почем",
    "скол на лобовом можно починить?",
    "вмятина на крыле, что делать",
    "керамика или воск что лучше",
    "мойка дисков есть?",
    "сколько стоит убрать царапину",
    "антидождь делаете",
    "оклейка капота плёнкой цена",
    "можно записаться на завтра",
    "где вы находитесь",
    "во сколько закрываетесь",
    "чем отличается евромойка от наномойки",
    "салон в коже, чем обработать",
    "сколько по времени займет полировка",
]

# Должны быть ОТСЕЧЕНЫ — до модели не доходят
OFF_TOPIC = [
    "сколько стоит шаурма",
    "Кубринск",
    "Ивантеевка",
    "кто такой ванёк грачев",
    "напиши стих про осень",
    "какая столица Франции",
    "реши уравнение 2x+5=13",
    "погода завтра",
    "курс доллара",
    "расскажи анекдот",
    "сколько ?",
    "а что",
    "переведи на английский привет",
    "кто выиграл вчера матч",
]

SMALLTALK = ["привет", "Здравствуйте", "спасибо", "ок", "здарова"]

fails = []

print("=== должны дойти до модели ===")
for q in ON_TOPIC:
    ok = bot.is_on_topic(q)
    if not ok:
        fails.append(("отсекли клиента", q))
    print(f"{'OK  ' if ok else 'FAIL'} {q}")

print("\n=== должны быть отсечены ===")
for q in OFF_TOPIC:
    ok = not bot.is_on_topic(q)
    if not ok:
        fails.append(("пропустили мусор", q))
    print(f"{'OK  ' if ok else 'FAIL'} {q}")

print("\n=== приветствия (отвечаем без модели) ===")
for q in SMALLTALK:
    ok = bot.is_smalltalk(q)
    if not ok:
        fails.append(("не распознали приветствие", q))
    print(f"{'OK  ' if ok else 'FAIL'} {q}")

print("\n=== лимит: 3 обращения в час ===")
seq = [bot.rate_limited(777) for _ in range(5)]
expected = [False, False, False, True, True]
ok = seq == expected
if not ok:
    fails.append(("лимит", str(seq)))
print(f"{'OK  ' if ok else 'FAIL'} {seq} (ожидали {expected})")

config.ADMIN_IDS = {42}
admin_seq = [bot.rate_limited(42) for _ in range(5)]
ok = not any(admin_seq)
if not ok:
    fails.append(("админа ограничили", str(admin_seq)))
print(f"{'OK  ' if ok else 'FAIL'} админ не ограничивается: {admin_seq}")

print("\n" + "=" * 60)
if fails:
    print(f"Провалов: {len(fails)}")
    for kind, q in fails:
        print(f"  {kind}: {q}")
else:
    print("Все прошли.")
sys.exit(1 if fails else 0)
