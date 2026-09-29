"""База знаний на SQLite + гибридный поиск (FTS5 + нечёткое сравнение + эмбеддинги)."""
from __future__ import annotations

import array
import math
from collections import Counter
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Iterable, Optional

import config

# --------------------------------------------------------------------------- #
#  Нормализация текста и очень лёгкий стеммер для русского
# --------------------------------------------------------------------------- #

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+", re.UNICODE)

_STOPWORDS = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "были", "куда", "зачем", "всех", "никогда",
    "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над",
    "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая",
    "много", "разве", "три", "эту", "моя", "впрочем", "хорошо", "свою",
    "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя", "такой",
    "им", "более", "всегда", "конечно", "всю", "между",
    "the", "a", "an", "is", "are", "of", "to", "in", "for", "and", "or",
}

_ENDINGS = (
    "иями", "ями", "ами", "ов", "ей", "ий", "ие", "ия", "ию", "иях", "ах", "ях",
    "ого", "ему", "ому", "ыми", "ими", "ать", "ить", "еть", "уть", "ешь", "ишь",
    "ете", "ите", "ают", "яют", "ают", "уют",
    "ет", "ит", "ут", "ют", "ат", "ят", "ся", "сь", "ые", "ых", "ая", "ой", "ом",
    "ем", "им", "ы", "и", "а", "я", "е", "у", "ю", "о", "ь",
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


_VOWELS = "аеиоуыэюя"


def _stem(word: str) -> str:
    # Порог 4 оставлял «фары» нетронутым, а «фар» из прайса резался до «фар» —
    # одно и то же слово получало разные основы и не находилось.
    if len(word) <= 3:
        return word
    for end in _ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 3:
            word = word[: -len(end)]
            break
    while len(word) > 3 and word[-1] in _VOWELS:
        word = word[:-1]
    return word


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in normalize(text).split() if w and w not in _STOPWORDS]


# Вопросительные слова: они есть почти в каждой записи и сами по себе ничего
# не значат. Запрос, где кроме них ничего нет («Сколько ?», «Количество»),
# не должен находить в базе НИЧЕГО — иначе бот отвечает наугад.
_QUESTION_WORDS = (
    "сколько", "количество", "как", "какой", "какая", "какие", "каков",
    "что", "кто", "где", "когда", "почему", "зачем", "чей", "куда", "откуда",
    "можно", "нужно", "надо", "скажи", "ответь", "подскажи", "вопрос",
)
_QUESTION_STEMS = {_stem(w) for w in _QUESTION_WORDS}


def content_tokens(text: str) -> list[str]:
    """Смысловые слова запроса — без стоп-слов и вопросительных."""
    return [t for t in tokens(text) if t not in _QUESTION_STEMS]


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _coverage(query_tokens: list[str], doc_tokens: list[str]) -> float:
    """Какая доля слов запроса нашлась в записи (важнее, чем симметричный Jaccard)."""
    if not query_tokens:
        return 0.0
    doc = set(doc_tokens)
    return sum(1 for t in query_tokens if t in doc) / len(query_tokens)


def text_similarity(query: str, question: str, answer: str = "") -> float:
    qt, tt = tokens(query), tokens(question)
    ratio = SequenceMatcher(None, normalize(query), normalize(question)).ratio()
    score = max(0.6 * _coverage(qt, tt) + 0.4 * _jaccard(qt, tt), ratio * 0.95)
    if answer:
        score = max(score, 0.7 * _coverage(qt, tokens(answer)))
    return min(score, 1.0)


# --------------------------------------------------------------------------- #
#  Эмбеддинги <-> BLOB
# --------------------------------------------------------------------------- #

def pack_vector(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


# --------------------------------------------------------------------------- #
#  Структуры
# --------------------------------------------------------------------------- #

@dataclass
class Hit:
    id: int
    question: str
    answer: str
    source: str
    score: float


SCHEMA = """
CREATE TABLE IF NOT EXISTS qa (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question      TEXT NOT NULL,
    question_norm TEXT NOT NULL,
    answer        TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'manual',   -- manual | ai | import
    tags          TEXT NOT NULL DEFAULT '',
    hits          INTEGER NOT NULL DEFAULT 0,
    author_id     INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_norm ON qa(question_norm);

CREATE VIRTUAL TABLE IF NOT EXISTS qa_fts USING fts5(
    question, answer, tags, tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS embeddings (
    qa_id  INTEGER PRIMARY KEY REFERENCES qa(id) ON DELETE CASCADE,
    model  TEXT NOT NULL,
    dim    INTEGER NOT NULL,
    vector BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    username   TEXT,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL,
    car        TEXT NOT NULL DEFAULT '',
    service    TEXT NOT NULL DEFAULT '',   -- о чём спрашивал перед записью
    when_text  TEXT NOT NULL DEFAULT '',   -- «завтра после 18» — как сказал клиент
    status     TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);

CREATE TABLE IF NOT EXISTS clients (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    name       TEXT NOT NULL DEFAULT '',
    phone      TEXT NOT NULL DEFAULT '',
    car        TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

-- Переписка для контекста: чтобы «а если ещё химчистку» понималось
-- и после перезапуска бота
CREATE TABLE IF NOT EXISTS dialog (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    role       TEXT NOT NULL,          -- user | assistant
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dialog_user ON dialog(user_id, id);

-- Что и когда мы уже писали человеку сами: защита от повторного дожима
CREATE TABLE IF NOT EXISTS followups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    ref_id     INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_followups_user ON followups(user_id, kind);

-- Настройки, изменённые командами бота (перекрывают .env)
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    username   TEXT,
    query      TEXT NOT NULL,
    qa_id      INTEGER,
    score      REAL,
    used_ai    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Чем закончился запрос: base | ai | noanswer | offtopic | smalltalk |
        # limit | media | unclear | error. Старые строки остаются NULL.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(query_log)")}
        if "outcome" not in cols:
            conn.execute("ALTER TABLE query_log ADD COLUMN outcome TEXT")


# --------------------------------------------------------------------------- #
#  CRUD
# --------------------------------------------------------------------------- #

def add_qa(
    question: str,
    answer: str,
    source: str = "manual",
    tags: str = "",
    author_id: Optional[int] = None,
) -> int:
    question, answer = question.strip(), answer.strip()
    qnorm = normalize(question)
    now = _now()
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM qa WHERE question_norm = ?", (qnorm,)
        ).fetchone()
        if existing:  # такой вопрос уже есть — обновляем ответ
            qa_id = existing["id"]
            conn.execute(
                "UPDATE qa SET answer=?, source=?, tags=?, updated_at=? WHERE id=?",
                (answer, source, tags, now, qa_id),
            )
            conn.execute("DELETE FROM qa_fts WHERE rowid=?", (qa_id,))
            conn.execute("DELETE FROM embeddings WHERE qa_id=?", (qa_id,))
        else:
            cur = conn.execute(
                "INSERT INTO qa (question, question_norm, answer, source, tags,"
                " author_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (question, qnorm, answer, source, tags, author_id, now, now),
            )
            qa_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO qa_fts (rowid, question, answer, tags) VALUES (?,?,?,?)",
            (qa_id, question, answer, tags),
        )
    return qa_id


def save_embedding(qa_id: int, model: str, vector: list[float]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO embeddings (qa_id, model, dim, vector) VALUES (?,?,?,?)",
            (qa_id, model, len(vector), pack_vector(vector)),
        )


def delete_qa(qa_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM qa WHERE id=?", (qa_id,))
        conn.execute("DELETE FROM qa_fts WHERE rowid=?", (qa_id,))
        conn.execute("DELETE FROM embeddings WHERE qa_id=?", (qa_id,))
        return cur.rowcount > 0


def purge(source: Optional[str] = None) -> int:
    """Удалить все записи (source='ai' — только ответы ИИ)."""
    with connect() as conn:
        sql = "SELECT id FROM qa" + (" WHERE source = ?" if source else "")
        ids = [r["id"] for r in conn.execute(sql, (source,) if source else ())]
        for qa_id in ids:
            conn.execute("DELETE FROM qa WHERE id=?", (qa_id,))
            conn.execute("DELETE FROM qa_fts WHERE rowid=?", (qa_id,))
            conn.execute("DELETE FROM embeddings WHERE qa_id=?", (qa_id,))
    return len(ids)


def get_qa(qa_id: int) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM qa WHERE id=?", (qa_id,)).fetchone()


def bump_hit(qa_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE qa SET hits = hits + 1 WHERE id=?", (qa_id,))


def list_qa(limit: int = 20, offset: int = 0, source: Optional[str] = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM qa"
    params: list = []
    if source:
        sql += " WHERE source = ?"
        params.append(source)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def stats() -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(source='manual') AS manual,"
            " SUM(source='ai') AS ai,"
            " SUM(source='import') AS imported,"
            " COALESCE(SUM(hits),0) AS hits FROM qa"
        ).fetchone()
        logs = conn.execute(
            "SELECT COUNT(*) AS q, COALESCE(SUM(used_ai),0) AS ai_calls FROM query_log"
        ).fetchone()
        emb = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()
    return {
        "total": row["total"] or 0,
        "manual": row["manual"] or 0,
        "ai": row["ai"] or 0,
        "imported": row["imported"] or 0,
        "hits": row["hits"] or 0,
        "queries": logs["q"] or 0,
        "ai_calls": logs["ai_calls"] or 0,
        "embeddings": emb["n"] or 0,
    }


def log_query(user_id, username, query, qa_id, score, used_ai, outcome=None) -> None:
    if outcome is None:
        outcome = "ai" if used_ai else ("base" if qa_id else None)
    with connect() as conn:
        conn.execute(
            "INSERT INTO query_log (user_id, username, query, qa_id, score, used_ai,"
            " outcome, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, username, query, qa_id, score, int(used_ai), outcome, _now()),
        )


def catalog(limit: int = 200) -> list[sqlite3.Row]:
    """Весь каталог для контекста ИИ: сначала то, что чаще спрашивают."""
    with connect() as conn:
        return conn.execute(
            "SELECT question, answer FROM qa ORDER BY hits DESC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()


_vocab_cache: tuple[int, set[str]] = (-1, set())


def vocabulary() -> set[str]:
    """Все смысловые слова каталога — словарь предметной области бота.

    По нему отсекаются вопросы не по теме: если ни одно слово запроса в
    каталоге не встречается, спрашивать модель бессмысленно и дорого.
    Кэш сбрасывается, когда меняется число записей.
    """
    global _vocab_cache
    with connect() as conn:
        n = int(conn.execute("SELECT COUNT(*) FROM qa").fetchone()[0])
        if _vocab_cache[0] == n:
            return _vocab_cache[1]
        words: set[str] = set()
        for row in conn.execute("SELECT question, answer FROM qa"):
            words.update(tokens(row["question"]))
            words.update(tokens(row["answer"]))
    _vocab_cache = (n, words)
    return words


def count_qa() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM qa").fetchone()[0])


def add_lead(
    user_id: Optional[int],
    username: Optional[str],
    name: str,
    phone: str,
    car: str = "",
    service: str = "",
    when_text: str = "",
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO leads (user_id, username, name, phone, car, service,"
            " when_text, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, username, name, phone, car, service, when_text, _now()),
        )
        lead_id = int(cur.lastrowid)
    # Запоминаем клиента здесь, а не в обработчике: иначе любой новый путь
    # создания заявки молча потеряет карточку, и повторная запись снова
    # спросит имя и телефон.
    if user_id:
        remember_client(user_id, username, name=name, phone=phone, car=car)
    return lead_id


def list_leads(limit: int = 20) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def lead_stats() -> dict:
    with connect() as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0])
        week = int(conn.execute(
            "SELECT COUNT(*) FROM leads WHERE created_at >= date('now','-7 day')"
        ).fetchone()[0])
    return {"total": total, "week": week}


def report_data(days: int = 7, tz_offset: int = 3) -> dict:
    """Сырьё для отчёта владельцу за последние N дней.

    Всё считается по журналу запросов и заявкам. Пометки вроде «[фото]»
    в журнале — это нетекстовые сообщения, они в темы не попадают.
    """
    since = _ago(days * 24)
    with connect() as conn:
        rows = conn.execute(
            "SELECT user_id, query, outcome, qa_id, used_ai, created_at FROM query_log"
            " WHERE created_at >= ? ORDER BY id", (since,),
        ).fetchall()
        leads = conn.execute(
            "SELECT status FROM leads WHERE created_at >= ?", (since,)
        ).fetchall()
        new_clients = int(conn.execute(
            "SELECT COUNT(*) FROM clients WHERE first_seen >= ?", (since,)
        ).fetchone()[0])

    users = {r["user_id"] for r in rows if r["user_id"]}
    by_outcome: dict[str, int] = {}
    hours = [0] * 24
    questions: list[str] = []
    unanswered: list[str] = []
    media = 0
    for r in rows:
        o = r["outcome"]
        if not o:
            # Журнал до появления колонки: исход восстанавливаем по старым
            # полям. Ответ из базы и через ИИ видны, а «не по теме» от
            # «привет» уже не отличить — они уходят в other.
            if r["query"].startswith("["):
                o = "media"
            elif r["used_ai"]:
                o = "ai"
            elif r["qa_id"]:
                o = "base"
            else:
                o = "other"
        by_outcome[o] = by_outcome.get(o, 0) + 1
        try:
            h = (int(r["created_at"][11:13]) + tz_offset) % 24
            hours[h] += 1
        except ValueError:
            pass
        if r["query"].startswith("["):
            media += 1
            continue
        if o in ("base", "ai", "noanswer", "booking"):
            questions.append(r["query"])
        if o == "noanswer":
            unanswered.append(r["query"])

    with connect() as conn:
        lead_users = {r["user_id"] for r in conn.execute(
            "SELECT DISTINCT user_id FROM leads WHERE created_at >= ?", (since,)
        ).fetchall()}
    asked_no_lead = len([u for u in users if u not in lead_users])

    return {
        "days": days,
        "since": since,
        "asked_no_lead": asked_no_lead,
        "requests": len(rows),
        "users": len(users),
        "new_users": min(new_clients, len(users)),
        "leads": len(leads),
        "leads_taken": sum(1 for l in leads if l["status"] != "new"),
        "by_outcome": by_outcome,
        "hours": hours,
        "questions": questions,
        "unanswered": unanswered,
        "media": media,
    }


# --------------------------------------------------------------------------- #
#  Клиенты: помним машину и контакт между разговорами
# --------------------------------------------------------------------------- #

def remember_client(user_id: int, username: Optional[str] = None, **fields) -> None:
    """Завести или обновить карточку клиента. Пустые значения не затирают."""
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO clients (user_id, username, first_seen, last_seen)"
            " VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET"
            " last_seen=excluded.last_seen,"
            " username=COALESCE(excluded.username, clients.username)",
            (user_id, username, now, now),
        )
        for key in ("name", "phone", "car"):
            value = (fields.get(key) or "").strip()
            if value:
                conn.execute(
                    f"UPDATE clients SET {key}=?, last_seen=? WHERE user_id=?",
                    (value, now, user_id),
                )


def get_client(user_id: int) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM clients WHERE user_id=?", (user_id,)
        ).fetchone()


# --------------------------------------------------------------------------- #
#  Переписка: короткая память диалога, переживающая перезапуск
# --------------------------------------------------------------------------- #

# Больше — дороже каждый запрос к модели и выше риск, что она зацепится
# за старую реплику вместо нового вопроса.
DIALOG_TURNS = 6


def add_dialog(user_id: int, role: str, content: str, keep: int = DIALOG_TURNS) -> None:
    """Записать реплику и тут же подрезать хвост.

    Чистим сразу при записи, а не отдельной уборкой: так таблица не растёт
    и не нужен ещё один фоновый процесс.
    """
    content = (content or "").strip()[:1500]
    if not content:
        return
    with connect() as conn:
        conn.execute(
            "INSERT INTO dialog (user_id, role, content, created_at) VALUES (?,?,?,?)",
            (user_id, role, content, _now()),
        )
        conn.execute(
            "DELETE FROM dialog WHERE user_id = ? AND id NOT IN ("
            "  SELECT id FROM dialog WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, keep),
        )


def dialog(user_id: int, ttl_hours: float = 6.0) -> list[dict]:
    """Последние реплики для контекста модели, в хронологическом порядке.

    Старше ttl_hours не берём: разговор трёхдневной давности — не контекст,
    а источник путаницы. «А если ещё химчистку» относится к сегодняшнему
    вопросу, не к позавчерашнему.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM dialog"
            " WHERE user_id = ? AND created_at >= ?"
            " ORDER BY id DESC LIMIT ?",
            (user_id, _ago(ttl_hours), DIALOG_TURNS),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def last_question(user_id: int, ttl_hours: float = 24.0) -> str:
    """О чём человек спрашивал последним — попадёт в карточку заявки."""
    with connect() as conn:
        row = conn.execute(
            "SELECT content FROM dialog"
            " WHERE user_id = ? AND role = 'user' AND created_at >= ?"
            " ORDER BY id DESC LIMIT 1",
            (user_id, _ago(ttl_hours)),
        ).fetchone()
    return row["content"] if row else ""


# --------------------------------------------------------------------------- #
#  Дожим: кто спросил и пропал, какие заявки висят без ответа
# --------------------------------------------------------------------------- #

def _ago(hours: float) -> str:
    """Момент времени N часов назад в том же формате, что и created_at."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def users_to_nudge(after_hours: float, window_hours: float) -> list[sqlite3.Row]:
    """Спрашивали, но не записались, и мы им ещё не писали.

    Сравнение идёт ПО ВРЕМЕНИ, а не по факту «когда-то записывался».
    Иначе клиент, оформивший заявку однажды, не получит дожим больше
    никогда — даже вернувшись через полгода с новым вопросом. Условие
    такое: последний вопрос задан позже последней заявки и позже нашего
    последнего письма этому человеку.

    Окно нужно, чтобы не написать через неделю после разговора:
    напоминание про давно забытый вопрос выглядит спамом.
    """
    with connect() as conn:
        return conn.execute(
            "SELECT q.user_id, q.username, MAX(q.created_at) AS last_seen,"
            "       (SELECT query FROM query_log q2 WHERE q2.user_id = q.user_id"
            "        ORDER BY q2.id DESC LIMIT 1) AS last_query"
            "  FROM query_log q"
            " WHERE q.user_id IS NOT NULL"
            " GROUP BY q.user_id"
            " HAVING last_seen <= ? AND last_seen >= ?"
            "   AND last_seen > COALESCE((SELECT MAX(created_at) FROM leads l"
            "                              WHERE l.user_id = q.user_id), '')"
            "   AND last_seen > COALESCE((SELECT MAX(created_at) FROM followups f"
            "                              WHERE f.user_id = q.user_id"
            "                                AND f.kind = 'nudge'), '')",
            (_ago(after_hours), _ago(after_hours + window_hours)),
        ).fetchall()


def stale_leads(after_minutes: float, max_age_hours: float = 72.0) -> list[sqlite3.Row]:
    """Заявки, которые никто не взял в работу.

    max_age_hours отсекает древние заявки: без него включение дожима на
    работающей базе разом выстреливает напоминаниями по всем старым
    заявкам, которые давно закрыты в жизни, но не отмечены в боте.
    """
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM leads WHERE status='new'"
            "   AND created_at <= ? AND created_at >= ?"
            "   AND id NOT IN (SELECT ref_id FROM followups"
            "                   WHERE kind='lead_remind' AND ref_id IS NOT NULL)"
            " ORDER BY id",
            (_ago(after_minutes / 60.0), _ago(max_age_hours)),
        ).fetchall()


def get_setting(key: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: Optional[str]) -> None:
    with connect() as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def broadcast_targets(topic: str = "", days: int = 60, cooldown_days: int = 14,
                      exclude_leads_days: int = 0) -> list[int]:
    """Кому отправить рассылку.

    Все, кто писал боту за последние days дней; при topic — только те, в чьих
    вопросах встречалась эта тема (по основе слова). Кому уже слали рассылку
    за cooldown_days — пропускаем: чаще раза в две недели это спам.
    exclude_leads_days > 0 — не трогать тех, кто за это время записался.
    """
    since = _ago(days * 24)
    stem = _stem(normalize(topic)) if topic else ""
    with connect() as conn:
        rows = conn.execute(
            "SELECT user_id, query FROM query_log WHERE created_at >= ? AND user_id IS NOT NULL",
            (since,),
        ).fetchall()
        recent = {r["user_id"] for r in conn.execute(
            "SELECT DISTINCT user_id FROM followups WHERE kind='broadcast' AND created_at >= ?",
            (_ago(cooldown_days * 24),),
        ).fetchall()}
        booked = set()
        if exclude_leads_days:
            booked = {r["user_id"] for r in conn.execute(
                "SELECT DISTINCT user_id FROM leads WHERE created_at >= ?",
                (_ago(exclude_leads_days * 24),),
            ).fetchall()}
    users: set[int] = set()
    for r in rows:
        if r["query"].startswith("["):
            continue
        if stem and stem not in {_stem(w) for w in normalize(r["query"]).split()}:
            continue
        users.add(int(r["user_id"]))
    return sorted(users - recent - booked)


def report_sent_recently(hours: float, kind: str = "report") -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM followups WHERE kind=? AND created_at >= ? LIMIT 1",
            (kind, _ago(hours)),
        ).fetchone()
    return row is not None


def mark_followup(user_id: int, kind: str, ref_id: Optional[int] = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO followups (user_id, kind, ref_id, created_at) VALUES (?,?,?,?)",
            (user_id, kind, ref_id, _now()),
        )


def set_lead_status(lead_id: int, status: str) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE leads SET status=? WHERE id=?", (status, lead_id))
        return cur.rowcount > 0


def rows_without_embeddings() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT q.id, q.question, q.answer FROM qa q"
            " LEFT JOIN embeddings e ON e.qa_id = q.id WHERE e.qa_id IS NULL"
        ).fetchall()


# --------------------------------------------------------------------------- #
#  Поиск
# --------------------------------------------------------------------------- #

# Запись, которая лексически не подтвердилась, зажимается в «серую зону»
# [WEAK_FLOOR..WEAK_CEIL] пропорционально покрытию смысла запроса. Именно
# пропорционально, а не одним числом: иначе все неподтверждённые записи
# получают равный скор и в тройку кандидатов для переспроса попадают первые
# по id, а не самые близкие.
WEAK_FLOOR = 0.20
WEAK_CEIL = 0.50
# Спрашивать нечего (запрос из одних вопросительных слов) — не переспрашиваем.
NO_MATCH_SCORE = 0.15


def _weak(coverage: float) -> float:
    return WEAK_FLOOR + (WEAK_CEIL - WEAK_FLOOR) * max(0.0, min(1.0, coverage))


def _fts_query(text: str) -> str:
    toks = [t for t in normalize(text).split() if t not in _STOPWORDS and len(t) > 2]
    if not toks:
        toks = normalize(text).split()
    if not toks:
        return ""
    # префиксный поиск покрывает разные окончания слов
    return " OR ".join(f'"{t}"*' for t in toks[:12])


def _weighted_coverage(
    query_tokens: list[str], idf: dict[str, float], doc: set[str], default: float = 1.0
) -> float:
    """Доля СМЫСЛА запроса, найденная в записи (редкие слова весят больше).

    Слово, которого нет в базе вообще, считается максимально значимым —
    иначе «сколько ДНЕЙ в году» совпало бы с «сколько МЕСЯЦЕВ в году».
    """
    if not query_tokens:
        return 0.0
    total = sum(idf.get(t, default) for t in query_tokens)
    if total <= 0:
        return 0.0
    found = sum(idf.get(t, default) for t in query_tokens if t in doc)
    return found / total


def search(query: str, query_vector: Optional[list[float]] = None, top_k: int = 5) -> list[Hit]:
    """Гибридный поиск: точное совпадение -> FTS5 -> взвешенное сравнение -> эмбеддинги."""
    qnorm = normalize(query)
    with connect() as conn:
        exact = conn.execute(
            "SELECT * FROM qa WHERE question_norm = ? LIMIT 1", (qnorm,)
        ).fetchone()
        if exact:
            return [Hit(exact["id"], exact["question"], exact["answer"], exact["source"], 1.0)]

        candidates: dict[int, sqlite3.Row] = {}
        fts_scores: dict[int, float] = {}

        match = _fts_query(query)
        if match:
            try:
                rows = conn.execute(
                    "SELECT rowid AS id, bm25(qa_fts, 3.0, 1.0, 1.0) AS rank"
                    " FROM qa_fts WHERE qa_fts MATCH ? ORDER BY rank LIMIT 30",
                    (match,),
                ).fetchall()
                for r in rows:
                    rel = max(0.0, -float(r["rank"]))  # bm25: меньше = лучше
                    # абсолютная шкала: не зависит от того, сколько записей в базе
                    fts_scores[r["id"]] = rel / (rel + 6.0)
            except sqlite3.OperationalError:
                pass

        if fts_scores:
            placeholders = ",".join("?" * len(fts_scores))
            for row in conn.execute(
                f"SELECT * FROM qa WHERE id IN ({placeholders})", list(fts_scores)
            ):
                candidates[row["id"]] = row

        if len(candidates) < 200:
            for row in conn.execute("SELECT * FROM qa ORDER BY id DESC LIMIT 500"):
                candidates.setdefault(row["id"], row)

        emb_scores: dict[int, float] = {}
        if query_vector:
            for r in conn.execute("SELECT qa_id, vector FROM embeddings"):
                if r["qa_id"] in candidates:
                    emb_scores[r["qa_id"]] = cosine(query_vector, unpack_vector(r["vector"]))

    if not candidates:
        return []

    # IDF по базе: общие слова ("сколько", "как", "какой") почти ничего не весят
    doc_tokens = {qa_id: tokens(row["question"]) for qa_id, row in candidates.items()}
    n_docs = len(doc_tokens) or 1
    df = Counter()
    for toks in doc_tokens.values():
        df.update(set(toks))
    idf = {t: math.log(1.0 + n_docs / (1.0 + c)) for t, c in df.items()}
    idf_unknown = math.log(1.0 + n_docs)  # слово, не встречающееся в базе

    qt = tokens(query)
    qnorm_full = normalize(query)
    use_emb = bool(emb_scores)
    # «Сколько ?», «Количество», «Кто» — одни вопросительные слова, спрашивать
    # нечего. Такой запрос не должен вытаскивать из базы случайную запись.
    empty_query = not content_tokens(query)
    hits: list[Hit] = []

    for qa_id, row in candidates.items():
        doc = set(doc_tokens[qa_id])
        cov = _weighted_coverage(qt, idf, doc, idf_unknown)
        # обратное покрытие: сколько смысла ЗАПИСИ есть в запросе.
        # Высокое значение = запрос содержит весь вопрос записи плюс уточнения.
        rev = _weighted_coverage(doc_tokens[qa_id], idf, set(qt), idf_unknown)
        ratio = SequenceMatcher(None, qnorm_full, normalize(row["question"])).ratio()
        sim = 0.78 * max(cov, 0.85 * cov + 0.15 * rev) + 0.22 * ratio
        # ответ учитываем как слабый бонус: он может содержать нужные слова
        acov = _weighted_coverage(qt, idf, set(tokens(row["answer"])), idf_unknown)
        sim = max(sim, 0.55 * acov)

        fts = fts_scores.get(qa_id, 0.0)
        if use_emb:
            emb = emb_scores.get(qa_id, 0.0)
            score = 0.50 * emb + 0.32 * sim + 0.18 * fts
            if emb < 0.60 and cov < 0.40 and ratio < 0.90 and rev < 0.85:
                score = min(score, _weak(max(cov, rev)))
        else:
            score = 0.85 * sim + 0.15 * fts
            # запрос про другое: ключевые слова запроса в записи не встречаются.
            # Чем короче запрос, тем выше требование к совпадению.
            min_cov = 0.75 if len(qt) <= 2 else (0.60 if len(qt) == 3 else 0.50)
            full_question_covered = rev >= 0.85  # запрос = вопрос записи + уточнения
            if cov < min_cov and ratio < 0.90 and not full_question_covered:
                score = min(score, _weak(max(cov, rev)))

        if empty_query:
            score = min(score, NO_MATCH_SCORE)

        hits.append(Hit(qa_id, row["question"], row["answer"], row["source"], round(score, 4)))

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]
