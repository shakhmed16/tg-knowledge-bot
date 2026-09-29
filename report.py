"""Сводка владельцу: что бот сделал за неделю, человеческим языком.

Это тот документ, который показывают в конце пробного периода и раз в
неделю на подписке. Поэтому здесь не «запросов: 61, used_ai: 27», а
«написали 23 человека, 6 записались, вот о чём спрашивали, вот на что
бот не смог ответить — добавьте в прайс».
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

import db

# Слова, которые есть в любом вопросе и не говорят о теме
_STOP = {
    "стоит", "стоимость", "цена", "цены", "прайс", "почем", "почём", "делаете",
    "сделать", "хочу", "нужен", "нужна", "нужно", "давайте", "сколько", "можно",
    "подскажите", "скажите", "здравствуйте", "добрый", "день", "вечер", "утро",
    "пожалуйста", "спасибо", "есть", "будет", "меня", "мне", "вас", "вам", "это",
    "какая", "какой", "какие", "что", "как", "или", "ещё", "еще", "все", "всё",
    "выйдет", "обойдется", "обойдётся", "примерно", "точно", "вообще",
}
_STOP_STEMS = {db._stem(db.normalize(w)) for w in _STOP}


def topics(questions: list[str], top: int = 6) -> list[tuple[str, int]]:
    """Самые частые слова вопросов — по основам, показываем самую частую форму.

    «керамика», «керамику», «керамики» — одна тема. Считаем один раз на
    вопрос, чтобы «полировка полировка полировка» не перевешивало.
    """
    stems: Counter[str] = Counter()
    forms: dict[str, Counter[str]] = {}
    for q in questions:
        seen = set()
        for word in db.normalize(q).split():
            if len(word) < 4 or word.isdigit():
                continue
            stem = db._stem(word)
            if stem in _STOP_STEMS or stem in seen:
                continue
            seen.add(stem)
            stems[stem] += 1
            forms.setdefault(stem, Counter())[word] += 1
    out = []
    for stem, n in stems.most_common(top):
        if n < 2 and len(out) >= 3:
            break  # единичные упоминания в топ не тянем
        # при равной частоте берём самую короткую форму — обычно это
        # именительный падеж («полировка», а не «полировку»)
        best = min(forms[stem].items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0]
        out.append((best, n))
    return out


def _key(question: str) -> str:
    """«тонировка задних стёкол» и «тонировка задних стёкол сколько» — один вопрос."""
    stems = sorted({
        db._stem(w) for w in db.normalize(question).split()
        if len(w) >= 4 and db._stem(w) not in _STOP_STEMS
    })
    return " ".join(stems) or db.normalize(question)


def _period(days: int, tz_offset: int) -> str:
    tz = timezone(timedelta(hours=tz_offset))
    end = datetime.now(tz)
    start = end - timedelta(days=days)
    months = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    return f"{start.day} {months[start.month - 1]} — {end.day} {months[end.month - 1]}"


def _people(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} человек"
    return f"{n} человек"


def _q(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} вопрос"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} вопроса"
    return f"{n} вопросов"


def build(days: int = 7, tz_offset: int = 3, lead_capture: bool = True, pro: bool = True) -> str:
    d = db.report_data(days, tz_offset)
    o = d["by_outcome"]
    answered = o.get("base", 0) + o.get("ai", 0) + o.get("noanswer", 0)

    lines = [f"📊 Отчёт за {days} дн. ({_period(days, tz_offset)})", ""]

    if d["requests"] == 0:
        lines.append("За этот период боту никто не писал.")
        return "\n".join(lines)

    # --- люди и заявки
    people = f"👥 Написали: {_people(d['users'])}, {_q(answered)}"
    if d["new_users"] and d["users"]:
        people += f"\n   новых — {d['new_users']}, вернулись — {d['users'] - d['new_users']}"
    lines.append(people)

    if lead_capture:
        lead_line = f"📝 Заявок на осмотр: {d['leads']}"
        if d["leads"]:
            lead_line += f" (взяты в работу: {d['leads_taken']})"
            if d["users"]:
                ratio = d["users"] / d["leads"]
                if ratio >= 2:
                    lead_line += f"\n   → записался каждый {round(ratio)}-й, кто написал"
                else:
                    lead_line += "\n   → записалось большинство написавших"
        lines.append(lead_line)

    # --- темы
    tp = topics(d["questions"])
    if tp:
        lines += ["", "💬 О чём спрашивали чаще всего:",
                  "   " + " · ".join(f"{w} — {n}" for w, n in tp)]

    # --- когда пишут
    total_h = sum(d["hours"])
    if total_h >= 5:
        evening = sum(d["hours"][18:23])
        night = sum(d["hours"][23:]) + sum(d["hours"][:8])
        work = total_h - evening - night
        lines += ["", "⏰ Когда пишут:",
                  f"   в рабочее время 8–18 — {round(100 * work / total_h)}%, "
                  f"вечером 18–23 — {round(100 * evening / total_h)}%, "
                  f"ночью — {round(100 * night / total_h)}%"]
        if evening + night > 0:
            lines.append(
                f"   → {evening + night} обращений пришло, когда администратор не на месте, "
                "— бот ответил сразу"
            )

    # --- на что не ответили
    un = d["unanswered"]
    if un:
        cnt = Counter(_key(q) for q in un)
        lines += ["", f"❓ Не смог ответить (нет в прайсе): {len(un)}"]
        shown = 0
        for key, n in cnt.most_common(5):
            original = min((q for q in un if _key(q) == key), key=len)
            tail = f" ×{n}" if n > 1 else ""
            lines.append(f"   • «{original[:70]}»{tail}")
            shown += 1
        if len(cnt) > shown:
            lines.append(f"   …и ещё {len(cnt) - shown}")
        lines.append("   → добавьте это в прайс — или ответ «не делаем», чтобы бот не отправлял к менеджеру")

    # --- отсечено
    cut = o.get("offtopic", 0) + o.get("limit", 0)
    extra = []
    if cut:
        extra.append(f"🚫 Не по теме и спам: {cut} — на ИИ не потрачено")
    if d["media"]:
        voice = o.get("voice", 0)
        note = f"🎤 Голосовых и кружков: {voice}" if voice else ""
        other = d["media"] - voice
        if other:
            note += (", " if note else "📎 ") + f"фото и файлов: {other}"
        if note:
            extra.append(note)
    if extra:
        lines += [""] + extra

    lines += ["", f"Ответов из прайса напрямую: {o.get('base', 0)} · через ИИ: "
              f"{o.get('ai', 0) + o.get('noanswer', 0)}"]

    # На «Стандарте» показываем, что именно упускается: конкретное число,
    # а не список функций. Это единственная строка о тарифе во всём отчёте.
    if not pro and lead_capture and d.get("asked_no_lead", 0) >= 3:
        n = d["asked_no_lead"]
        lines += ["", f"💡 {n} человек спросили цену и не записались. В тарифе «Про» им можно "
                  "отправить предложение одной командой."]
    return "\n".join(lines)
