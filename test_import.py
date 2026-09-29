# -*- coding: utf-8 -*-
"""Проверка импорта CSV на разных форматах файлов.

    python test_import.py

Ловит главную ловушку: в ответах полно запятых («мойка, полировка, керамика»),
поэтому разделитель нельзя выбирать подсчётом символов — только по тому,
какой даёт ровное число колонок во всём файле.
"""
import os, sys, csv, io
os.environ.setdefault("BOT_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import bot

def check(name, raw: bytes, expect_ok=True, expect_n=None, expect_first=None):
    text = bot.decode_csv(raw)
    pairs, err = bot.parse_csv(text)
    ok = (not err) if expect_ok else bool(err)
    if expect_ok and expect_n is not None and len(pairs) != expect_n:
        ok = False
    if expect_ok and expect_first is not None:
        if not pairs or pairs[0] != expect_first:
            ok = False
    print(f"{'OK  ' if ok else 'FAIL'} {name}: строк={len(pairs)} ошибка={err[:70]!r}")
    if pairs and expect_ok:
        print(f"       первая: {pairs[0][0][:40]!r} -> {pairs[0][1][:40]!r}")
    return ok

results = []
real = open("price_detailing_demo.csv","rb").read()
results.append(check("наш демо-прайс (;, UTF-8 BOM)", real, True, 47,
    ("какие услуги вы оказываете",
     "Мойка, химчистка салона, полировка кузова, керамика, антидождь, оклейка "
     "плёнкой (PPF), оклейка крыши, шумоизоляция, снятие тонировки, ремонт "
     "лобового стекла, удаление вмятин (PDR), предпродажная подготовка, "
     "антихром. Спросите про любую — назову цену.")))

# Excel по-русски: cp1251 + ;
rows = [["question","answer"],["сколько стоит мойка","Мойка: 500, 600, 800 руб."],["адрес","Москва, ул. Ленина, 5"]]
b=io.StringIO(); csv.writer(b,delimiter=";").writerows(rows)
results.append(check("Excel cp1251 + ;", b.getvalue().encode("cp1251"), True, 2))

# запятая как разделитель, кавычки вокруг запятых внутри
b=io.StringIO(); csv.writer(b,delimiter=",").writerows(rows)
results.append(check("UTF-8 + , (кавычки)", b.getvalue().encode("utf-8"), True, 2))

# без заголовка
b=io.StringIO(); csv.writer(b,delimiter=";").writerows(rows[1:])
results.append(check("без заголовка, 2 колонки", b.getvalue().encode("utf-8"), True, 2,
    ("сколько стоит мойка", "Мойка: 500, 600, 800 руб.")))

# русские заголовки
rows_ru=[["услуга","цена"],["полировка","от 10 000 ₽"]]
b=io.StringIO(); csv.writer(b,delimiter=";").writerows(rows_ru)
results.append(check("русские заголовки услуга;цена", b.getvalue().encode("utf-8"), True, 1))

# табуляция (вставка из Excel)
results.append(check("табуляция", "question\tanswer\nмойка\t500 ₽\n".encode("utf-8"), True, 1))

# мусор — должен вернуть внятную ошибку
results.append(check("одна колонка без разделителя", "просто текст без ничего\n".encode("utf-8"), False))
results.append(check("пустой файл", b"", False))

print("\nИтог:", "все прошли" if all(results) else f"провалов: {results.count(False)}")
sys.exit(0 if all(results) else 1)
