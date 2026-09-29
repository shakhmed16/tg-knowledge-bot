# -*- coding: utf-8 -*-
"""Поиск противоречий в прайсе: одна услуга с разными ценами в разных записях.

    python audit.py                    # проверить базу бота
    python audit.py price.csv          # проверить CSV перед импортом

Зачем. Цену одной и той же услуги легко упомянуть дважды — в её собственной
записи и «заодно» в описании смежной («перед керамикой нужна полировка…»).
Дальше одну из них правят, вторую забывают, и бот начинает называть клиентам
две разные цены — обе «из прайса», обе с виду законные. Глазами это не
ловится: записей десятки, а цифры разбросаны по тексту.
"""
from __future__ import annotations

import csv
import re
import sys

import db

# «Название услуги: цены…» — как раз так устроены записи прайса
_SEGMENT = re.compile(r"([^.;:]{4,80}?):\s*([^.;]*?\d[^.;]*)", re.UNICODE)
_NUMBER = re.compile(r"\d[\d   ]*\d|\d")


def _prices(text: str) -> tuple[str, ...]:
    """Все числа отрезка в нормальном виде: «13 000» и «13000» — одно и то же."""
    out = []
    for m in _NUMBER.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if len(digits) >= 3:  # 500, 13000 — цены; «1», «4 шт» — нет
            out.append(digits)
    return tuple(out)


def segments(answer: str) -> list[tuple[frozenset, tuple[str, ...], str]]:
    """Разобрать ответ на пары «услуга → цены»."""
    found = []
    for m in _SEGMENT.finditer(answer):
        name, tail = m.group(1).strip(" ,-—«»"), m.group(2)
        prices = _prices(tail)
        words = frozenset(db.tokens(name))
        if prices and len(words) >= 2:
            found.append((words, prices, name))
    return found


def _same_service(a: frozenset, b: frozenset) -> bool:
    """Одна ли это услуга.

    Названия одной услуги в разных записях не совпадают дословно:
    «Восстановительная полировка кузова» и «Перед керамикой нужна
    восстановительная полировка». Поэтому смотрим, насколько короткое
    название вложено в длинное.
    """
    common = a & b
    if len(common) < 2:
        return False
    return len(common) / min(len(a), len(b)) >= 0.6


def find_conflicts(rows: list[tuple[int, str, str]]) -> list[dict]:
    """rows: [(id, вопрос, ответ)]. Вернёт услуги с расходящимися ценами."""
    items = []
    for qa_id, question, answer in rows:
        for words, prices, name in segments(answer):
            items.append((qa_id, words, prices, name, question))

    conflicts, paired = [], set()
    for i, (id_a, wa, pa, na, qa) in enumerate(items):
        for id_b, wb, pb, nb, qb in items[i + 1:]:
            if id_a == id_b or not _same_service(wa, wb):
                continue
            if set(pa) == set(pb):
                continue  # цены совпадают — всё хорошо
            # Одна услуга с опечаткой выглядит так: список цен почти тот же,
            # разъехалась одна позиция (13 000 / 12 000, остальные совпали).
            # Одна случайно общая цифра — это разные услуги: у химчистки кожи
            # и велюра совпадает 1 900, но это совпадение, а не противоречие.
            sa, sb = set(pa), set(pb)
            if len(sa & sb) / min(len(sa), len(sb)) < 0.5:
                continue
            mark = (id_a, id_b, na, nb)
            if mark in paired:
                continue
            paired.add(mark)
            conflicts.append({
                "name": na if len(wa) <= len(wb) else nb,
                "entries": [(id_a, pa, na, qa), (id_b, pb, nb, qb)],
            })
    return conflicts


def report(conflicts: list[dict]) -> str:
    if not conflicts:
        return "Противоречий не нашлось: каждая услуга упомянута с одной ценой."
    lines = [f"Услуг с расходящимися ценами: {len(conflicts)}", ""]
    for c in conflicts:
        lines.append(f"«{c['name']}»")
        for qa_id, prices, name, question in c["entries"]:
            shown = ", ".join(f"{int(p):,}".replace(",", " ") for p in prices)
            lines.append(f"  [{qa_id}] {shown}   ← {question[:50]}")
        lines.append("")
    lines.append(
        "Оставьте цену в одном месте. В смежных записях лучше ссылаться "
        "на услугу, а не повторять цифры: копия рано или поздно разойдётся."
    )
    return "\n".join(lines)


def _rows_from_csv(path: str) -> list[tuple[int, str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read()
    delim = ";" if sample.splitlines()[0].count(";") else ","
    rows = list(csv.DictReader(sample.splitlines(), delimiter=delim))
    return [
        (i, (r.get("question") or "").strip(), (r.get("answer") or "").strip())
        for i, r in enumerate(rows, 1)
        if (r.get("answer") or "").strip()
    ]


if __name__ == "__main__":
    if len(sys.argv) > 1:
        data = _rows_from_csv(sys.argv[1])
        print(f"Проверяю {sys.argv[1]}: записей {len(data)}\n")
    else:
        data = [(r["id"], r["question"], r["answer"]) for r in db.list_qa(100000, 0, None)]
        print(f"Проверяю базу: записей {len(data)}\n")
    found = find_conflicts(data)
    print(report(found))
    sys.exit(1 if found else 0)
