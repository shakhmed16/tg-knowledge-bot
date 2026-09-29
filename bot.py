"""Telegram-бот: сначала ищет ответ в базе знаний, при неудаче спрашивает ИИ
и сохраняет полученный ответ обратно в базу."""
from __future__ import annotations

import asyncio
import csv
import httpx
import html
import io
import logging
import re
import signal
import time
from collections import Counter, deque
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import ai
import audit
import config
import db
import demo
import md
import net
import report
import trial
import voice

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

router = Router()


class TrialGate(BaseMiddleware):
    """После окончания пробного периода клиентам отвечаем одной фразой.

    Стоит перед всеми обработчиками: и текст, и фото, и кнопки. Админы
    проходят всегда — им нужно продлить командой /trial. Чтобы не
    заваливать человека одинаковыми ответами, отвечаем не чаще раза в час.
    """

    _last: dict[int, float] = {}

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or is_admin(user.id) or trial.active():
            return await handler(event, data)
        now = time.monotonic()
        last = self._last.get(user.id)
        # monotonic() считает от загрузки машины: сравнивать с нулём нельзя,
        # на свежем сервере первый час все ответы оказались бы подавлены
        if last is None or now - last > 3600:
            self._last[user.id] = now
            target = event.message if isinstance(event, CallbackQuery) else event
            try:
                await target.answer(config.TRIAL_OVER_TEXT)
            except Exception:
                pass
        if isinstance(event, CallbackQuery):
            try:
                await event.answer()
            except Exception:
                pass
        return None


router.message.middleware(TrialGate())
router.callback_query.middleware(TrialGate())

# Взводится по SIGTERM/SIGINT: сигнал остановиться, а не падать
_shutdown = asyncio.Event()

# Ответы ИИ на такие реплики в базу не сохраняем — это не знания
_JUNK_ANSWER_MARKERS = (
    "уточните", "прошу прощения", "переформулируйте", "чем именно",
    "не могу ответить", "извините", "не совсем понял", "не могу сказать",
    "точно не скажу", "не располагаю", "зависит от", "не знаю",
    "нет информации", "информации нет", "у меня нет", "посмотрите на",
    "лучше уточнить", "не нашёл", "не нашел", "недостаточно данных",
    "не имею доступа", "как ии", "языковая модель",
)

# Обращения к самому боту («как тебя зовут», «ты не прав») — это не знания
_CHAT_RE = re.compile(
    r"\b(ты|тебя|тебе|тобой|твой|твоя|твоё|твои|вы\s+кто)\b", re.IGNORECASE
)
_GREETINGS = {
    "привет", "здравствуй", "здравствуйте", "хай", "ку", "здорово", "спасибо",
    "пока", "ок", "окей", "да", "нет", "ага", "понял", "поняла", "супер",
    "круто", "help", "hi", "hello", "thanks",
}


def is_saveable(query: str, answer: str) -> bool:
    """Стоит ли класть эту пару в базу знаний."""
    q = query.strip()
    if len(q) < 12 or len(db.tokens(q)) < 2:
        return False  # «нет», «ок», «э» — не вопросы
    if db.normalize(q) in _GREETINGS:
        return False
    if not db.content_tokens(q):
        return False  # одни вопросительные слова: «сколько ?», «количество»
    if _CHAT_RE.search(q):
        return False  # разговор с ботом, а не вопрос по предметной области
    low = answer.lower()
    return not any(marker in low for marker in _JUNK_ANSWER_MARKERS)


def is_admin(user_id: int | None) -> bool:
    return user_id in config.ADMIN_IDS


# Так модель в строгом режиме говорит, что услуги в прайсе нет
_NOANSWER_MARKERS = (
    "нет в каталоге", "в каталоге нет", "в каталоге отсутству", "отсутствует в каталоге",
    "нет в прайсе", "в прайсе нет", "не включ", "уточните у менеджера",
    "обратитесь к менеджеру", "свяжитесь с менеджером", "не могу назвать",
    "не указан", "такой услуги нет", "этого нет",
)


def is_noanswer(answer: str) -> bool:
    """Ответ модели, в котором она отправила к менеджеру за тем, чего нет."""
    low = answer.lower()
    return any(m in low for m in _NOANSWER_MARKERS)


# --------------------------------------------------------------------------- #
#  Экономия токенов: не пускаем к модели то, что и так не про нас
# --------------------------------------------------------------------------- #

# Разговорные реплики отвечаются без модели. Разделены по смыслу: на
# «привет» надо поздороваться и пригласить спросить, а не вываливать
# инструкцию — клиент ещё ничего не спросил, читать её незачем.
_HELLO = {
    "привет", "приветик", "прив", "здравствуй", "здравствуйте", "хай",
    "здарова", "здорова", "здорово", "доброе утро", "добрый день",
    "добрый вечер", "ку", "hi", "hello", "start",
}
_THANKS = {"спасибо", "спасибо большое", "благодарю", "спс", "пасиб", "thanks", "thx"}
_BYE = {"пока", "до свидания", "всего доброго", "бывай", "ok", "bye"}
_ACK = {"ок", "окей", "понял", "поняла", "ясно", "хорошо", "супер", "круто",
        "класс", "ага", "угу", "отлично", "думаю", "сейчас"}
_SMALLTALK = _HELLO | _THANKS | _BYE | _ACK


def is_smalltalk(query: str) -> bool:
    return db.normalize(query) in _SMALLTALK


def smalltalk_reply(query: str) -> str:
    """Короткий человеческий ответ на разговорную реплику."""
    q = db.normalize(query)
    if q in _THANKS:
        return "Пожалуйста! Если что-то ещё нужно — спрашивайте."
    if q in _BYE:
        return "Хорошего дня! Будут вопросы по услугам — пишите."
    if q in _ACK:
        return "Если появятся вопросы по услугам или ценам — я здесь."
    # приветствие
    text = "Здравствуйте! Подскажу цены на наши услуги"
    text += " и запишу на осмотр." if config.LEAD_CAPTURE else "."
    return text + "\nЧто вас интересует?"


# Слова торга: есть в любом прайсе и потому ничего не говорят о теме.
# Без этого «сколько стоит шаурма» проходило фильтр через слово «стоит».
_TRADE_WORDS = (
    "стоит", "стоимость", "цена", "цены", "прайс", "почем", "почём",
    "делаете", "сделать", "хочу", "нужен", "нужна", "давайте", "сколько",
)
_TRADE_STEMS = {db._stem(db.normalize(w)) for w in _TRADE_WORDS}

# Вопросы, уместные в любой компании, даже если этих слов нет в прайсе:
# клиент спрашивает «где вы находитесь» — в прайсе слова «находитесь» нет,
# но отшивать за это нельзя.
_BUSINESS_WORDS = (
    "адрес", "находитесь", "находится", "приехать", "доехать", "заехать",
    "работаете", "график", "часы", "открыты", "закрыты", "открываетесь",
    "закрываетесь", "выходной", "записаться", "запись", "записывают",
    "очередь", "свободно", "телефон", "контакты", "связаться", "менеджер",
    "оплата", "оплатить", "картой", "наличными", "рассрочка", "гарантия",
    "скидка", "акция", "отзывы", "долго", "срок", "сроки", "успеете",
)
_BUSINESS_STEMS = {db._stem(db.normalize(w)) for w in _BUSINESS_WORDS}
_BUSINESS_STEMS |= {
    db._stem(db.normalize(w)) for w in config.EXTRA_TOPIC_WORDS.split(",") if w.strip()
}


def is_on_topic(query: str) -> bool:
    """Есть ли в запросе хоть одно слово из каталога.

    Дешёвая отсечка перед вызовом модели: в строгом режиме на каждый вопрос
    в контекст уходит весь прайс, так что «сколько стоит шаурма» обходится
    как настоящий вопрос про керамику. Слово запроса, которого нет в прайсе
    ни разу, — верный признак, что спрашивают не у нас.

    Ошибиться в одну сторону дешевле, чем в другую: пропустить лишний
    вопрос — потерять копейки, отшить клиента — потерять клиента. Поэтому
    при любых сомнениях пропускаем.
    """
    if not config.TOPIC_GUARD:
        return True

    words = [
        w for w in db.content_tokens(query)
        if w not in _TRADE_STEMS and not w.isdigit()  # цифры совпадают с ценами
    ]
    if not words:
        return False  # «сколько ?», «а что», «почём» — спрашивать нечего

    if any(w in _BUSINESS_STEMS for w in words):
        return True  # адрес, часы, запись, оплата — всегда про нас

    vocab = db.vocabulary()
    if not vocab:
        return True  # база пустая — фильтровать нечем, пропускаем
    return any(w in vocab for w in words)


# user_id -> время последних обращений к ИИ
_ai_calls: dict[int, deque] = {}


def rate_limited(user_id: int) -> bool:
    """Не слишком ли часто этот человек дёргает модель."""
    if config.RATE_LIMIT_PER_HOUR <= 0 or is_admin(user_id):
        return False
    now = time.monotonic()
    calls = _ai_calls.setdefault(user_id, deque())
    while calls and now - calls[0] > 3600:
        calls.popleft()
    if len(calls) >= config.RATE_LIMIT_PER_HOUR:
        return True
    calls.append(now)
    return False


def clip(text: str) -> str:
    return text if len(text) <= config.MAX_ANSWER_LEN else text[: config.MAX_ANSWER_LEN] + "…"


async def answer_rich(message: Message, text: str, reply_markup=None) -> Message:
    """Ответить с разметкой: Markdown из ИИ превращаем в HTML для Telegram."""
    body = clip(text)
    try:
        return await message.answer(
            md.to_html(body), parse_mode="HTML", reply_markup=reply_markup
        )
    except TelegramBadRequest:
        # некорректная разметка — отправляем чистый текст без звёздочек
        return await message.answer(md.strip(body), reply_markup=reply_markup)


def feedback_kb(qa_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="👍 Полезно", callback_data=f"fb:ok:{qa_id}"),
            InlineKeyboardButton(text="👎 Неверно", callback_data=f"fb:bad:{qa_id}"),
        ]]
    )


# --------------------------------------------------------------------------- #
#  Нетекстовые сообщения
# --------------------------------------------------------------------------- #

# Когда и на что человеку последний раз отвечали: альбом из пяти фотографий
# приходит пятью отдельными сообщениями, и без этого бот пять раз подряд
# написал бы одно и то же.
_media_last: dict[int, tuple[str, float]] = {}


def media_repeat(user_id: int, key: str, album: bool = False) -> bool:
    """True, если такому же сообщению только что уже отвечали.

    Альбом приходит пачкой за секунду-две — его узнаём по id группы и держим
    долго. Одиночные сообщения одного типа гасим только в коротком окне:
    два кружка подряд с паузой — это два обращения, и молчать на второе
    нельзя (так и было).
    """
    now = time.monotonic()
    prev = _media_last.get(user_id)
    _media_last[user_id] = (key, now)
    window = config.MEDIA_QUIET_SEC if album else config.MEDIA_QUIET_SINGLE_SEC
    return bool(prev and prev[0] == key and now - prev[1] < window)


async def media_reply(message: Message, kind: str, text: str) -> None:
    """Ответ на фото, голосовое, файл и прочее нетекстовое.

    Модель не зовём: отвечать тут нечем, а каждый вызов стоит денег.
    В журнал пишем пометку вида «[фото]» — по ней потом видно, чего люди
    ждут от бота помимо текста.
    """
    user = message.from_user
    group = getattr(message, "media_group_id", None)
    if media_repeat(user.id, group or kind, album=bool(group)):
        return
    await asyncio.to_thread(db.remember_client, user.id, user.username)
    await asyncio.to_thread(
        db.log_query, user.id, user.username, f"[{kind}]", None, 0.0, False, "media"
    )
    await message.answer(text, reply_markup=offer_booking())


# --------------------------------------------------------------------------- #
#  Команды
# --------------------------------------------------------------------------- #

def help_user() -> str:
    """Справка клиенту: что спросить, а не список команд.

    Клиент не админ — команды ему почти все недоступны, и перечислять их
    бессмысленно. Полезнее показать примеры вопросов: человек видит,
    какими словами с ботом говорить.
    """
    if not config.STRICT_CATALOG:
        return (
            "Просто напишите вопрос — я поищу ответ в базе знаний.\n"
            "Если в базе ничего не найдётся, спрошу ИИ и запомню ответ на будущее."
        )

    parts = [
        "Я подскажу цены на наши услуги и запишу на осмотр.",
        "",
        "Спрашивайте как удобно, например:",
        "• сколько стоит керамика на Camry",
        "• полировка и химчистка на джипе — сколько выйдет",
        "• во сколько вы работаете",
        "",
        "Назовёте машину — посчитаю цену именно для неё. Несколько услуг "
        "сразу — сложу и назову итог.",
    ]
    if config.VOICE_TRANSCRIBE and config.AI_API_KEY:
        parts.append("Можно голосовым или кружком — расшифрую и отвечу.")
    if config.LEAD_CAPTURE:
        parts += ["", "Записаться на осмотр — /zapis или кнопка под ответом."]
    contact = config.contact_line()
    if contact:
        parts += ["", contact]
    return "\n".join(parts)


HELP_USER = help_user()

HELP_ADMIN = HELP_USER + (
    "\n\nАдмин-команды:\n"
    "/add вопрос | ответ — добавить запись\n"
    "/del <id> — удалить запись\n"
    "/find <текст> — что найдёт бот по этому запросу (с оценками)\n"
    "/list [ai|manual|import] — последние записи\n"
    "/purge ai — удалить все ответы ИИ, /purge all — очистить базу\n"
    "/leads — последние заявки на осмотр\n"
    "/audit — найти услуги с разными ценами в разных записях\n"
    "/stats — статистика базы\n"
    "/settings — с какими настройками работает бот\n"
    "/trial [+7 | дата | off] — пробный период\n"
    "/broadcast [тема |] текст — рассылка по базе клиентов (Про)\n"
    "/report [дней] [send] — сводка владельцу; send — отправить в чат студии\n"
    "/export — выгрузить базу в CSV\n"
    "/reindex — пересчитать эмбеддинги\n"
    "Импорт: пришлите CSV-файл — колонки question;answer\n"
    "(подойдёт и файл без заголовка из двух колонок)"
)


def greeting_text() -> str:
    if not config.STRICT_CATALOG:
        return "Привет! Я отвечаю на вопросы по базе знаний."
    who = f"Я помощник студии {config.STUDIO_NAME}: " if config.STUDIO_NAME else "Я "
    tail = "подскажу цены и запишу на осмотр." if config.LEAD_CAPTURE else "подскажу по услугам и ценам."
    return "Здравствуйте! " + who + tail


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    greeting = greeting_text()
    await message.answer(
        greeting + "\n\n"
        + (HELP_ADMIN if is_admin(message.from_user.id) else HELP_USER)
    )


@router.message(Command("demo"))
async def cmd_demo(message: Message, state: FSMContext) -> None:
    """Показ возможностей за минуту. Только на демо-боте (DEMO_MODE=true)."""
    if not config.DEMO_MODE and not is_admin(message.from_user.id):
        await message.answer(config.UNKNOWN_COMMAND)
        return
    await demo.run(message, state, handle_question)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_ADMIN if is_admin(message.from_user.id) else HELP_USER)


@router.message(Command("add"))
async def cmd_add(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    payload = (message.text or "").partition(" ")[2].strip()
    if "|" not in payload:
        await message.answer("Формат: /add вопрос | ответ")
        return
    question, _, answer = payload.partition("|")
    question, answer = question.strip(), answer.strip()
    if not question or not answer:
        await message.answer("Формат: /add вопрос | ответ")
        return

    qa_id = await asyncio.to_thread(
        db.add_qa, question, answer, "manual", "", message.from_user.id
    )
    vector = await ai.embed(f"{question}\n{answer}")
    if vector:
        await asyncio.to_thread(db.save_embedding, qa_id, config.AI_EMBEDDING_MODEL, vector)
    await message.answer(f"Сохранено, id={qa_id}")


@router.message(Command("purge"))
async def cmd_purge(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    arg = (message.text or "").partition(" ")[2].strip().lower()
    if arg not in {"ai", "import", "manual", "all"}:
        await message.answer(
            "Формат: /purge import — удалить загруженный прайс, /purge ai — ответы ИИ, "
            "/purge manual — добавленное вручную, /purge all — очистить базу целиком."
        )
        return
    removed = await asyncio.to_thread(db.purge, None if arg == "all" else arg)
    await message.answer(f"Удалено записей: {removed}. Теперь отправьте новый CSV." if arg == "import"
                         else f"Удалено записей: {removed}")


@router.message(Command("del"))
async def cmd_del(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    arg = (message.text or "").partition(" ")[2].strip()
    if not arg.isdigit():
        await message.answer("Формат: /del <id>")
        return
    ok = await asyncio.to_thread(db.delete_qa, int(arg))
    await message.answer("Удалено." if ok else "Запись не найдена.")


@router.message(Command("find"))
async def cmd_find(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("Формат: /find <текст>")
        return
    vector = await ai.embed(query)
    hits = await asyncio.to_thread(db.search, query, vector, 5)
    if not hits:
        await message.answer("База пуста или ничего не найдено.")
        return
    lines = [
        f"Порог: {config.MATCH_THRESHOLD} | серая зона от {config.RERANK_MIN_SCORE}"
        f" ({'переспрос вкл' if config.AI_RERANK else 'переспрос выкл'})"
    ]
    for h in hits:
        if h.score >= config.MATCH_THRESHOLD:
            mark = "✅"          # ответим из базы сразу
        elif h.score >= config.RERANK_MIN_SCORE:
            mark = "🤔"          # серая зона — спросим модель
        else:
            mark = "▫️"          # мимо
        lines.append(f"{mark} [{h.id}] {h.score:.2f} — {h.question[:80]}")

    shortlist = [(h.id, h.question, h.answer) for h in hits[:3]]
    grey = config.RERANK_MIN_SCORE <= hits[0].score < config.MATCH_THRESHOLD
    if grey and config.AI_RERANK and config.AI_API_KEY:
        picked = await ai.rerank(query, shortlist)
        lines.append(
            f"\nПереспрос: {'запись ' + str(picked) if picked else 'ни одна не подходит → ИИ'}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    arg = (message.text or "").partition(" ")[2].strip().lower()
    source = arg if arg in {"ai", "manual", "import"} else None
    rows = await asyncio.to_thread(db.list_qa, 20, 0, source)
    if not rows:
        await message.answer("Пусто.")
        return
    lines = [
        f"[{r['id']}] ({r['source']}, 👁{r['hits']}) {r['question'][:70]}" for r in rows
    ]
    await message.answer(clip("Последние записи:\n" + "\n".join(lines)))


@router.message(Command("audit"))
async def cmd_audit(message: Message) -> None:
    """Найти услуги, у которых в разных записях разные цены."""
    if not is_admin(message.from_user.id):
        return
    rows = await asyncio.to_thread(db.list_qa, 100000, 0, None)
    data = [(r["id"], r["question"], r["answer"]) for r in rows]
    conflicts = await asyncio.to_thread(audit.find_conflicts, data)
    await message.answer(clip(audit.report(conflicts)))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    s = await asyncio.to_thread(db.stats)
    await message.answer(
        f"Записей в базе: {s['total']}\n"
        f"  вручную: {s['manual']} | от ИИ: {s['ai']} | импорт: {s['imported']}\n"
        f"Эмбеддингов: {s['embeddings']}"
        f" ({'включены' if ai.embeddings_enabled() else 'выключены'})\n"
        f"Запросов пользователей: {s['queries']}\n"
        f"Из них ушло в ИИ: {s['ai_calls']}\n"
        f"Ответов выдано из базы: {s['hits']}"
    )


def _mask(secret: str) -> str:
    """Показать, что значение задано, не показывая его."""
    if not secret:
        return "— не задан"
    return f"…{secret[-4:]} (задан)"


def settings_report() -> str:
    """Действующие настройки — то, что бот реально прочитал из .env.

    Нужна, потому что .env снаружи не посмотреть: файл с секретами, его
    не передают. А вопрос «а точно ли применилось» возникает после каждой
    правки. Секреты маскируются.
    """
    c = config
    quiet = (
        "круглосуточно" if c.QUIET_FROM_HOUR == c.QUIET_TO_HOUR
        else f"тихие часы {c.QUIET_FROM_HOUR:02d}:00–{c.QUIET_TO_HOUR:02d}:00 (UTC{c.TIMEZONE_OFFSET:+d})"
    )
    on = lambda v: "вкл" if v else "выкл"  # noqa: E731
    lines = [
        "⚙️ Действующие настройки",
        "",
        f"Студия: {c.STUDIO_NAME or '— не задана'} · тариф {'Про' if c.PRO else 'Стандарт'}",
        trial.status_line(),
        "",
        f"Бот-токен: {_mask(c.BOT_TOKEN)}",
        f"Админы: {', '.join(map(str, sorted(c.ADMIN_IDS))) or '— нет'}",
        "",
        f"ИИ: ключ {_mask(c.AI_API_KEY)}, модель {c.AI_MODEL}",
        f"Переспрос: {on(c.AI_RERANK)}, {c.AI_RERANK_MODEL}",
        f"Голосовые: {on(c.VOICE_TRANSCRIBE)}, {c.AI_VOICE_MODEL}, до {c.VOICE_MAX_SEC} с, "
        f"декодер {'есть' if voice.AVAILABLE else 'НЕТ'}",
        "",
        f"Режим прайса (STRICT_CATALOG): {on(c.STRICT_CATALOG)}",
        f"Автосохранение ответов ИИ: {on(c.AUTOSAVE_AI)}",
        f"Фильтр не по теме: {on(c.TOPIC_GUARD)}",
        f"Лимит на человека: {c.RATE_LIMIT_PER_HOUR} обращений в час"
        if c.RATE_LIMIT_PER_HOUR else "Лимит на человека: без лимита",
        "",
        f"Заявки: {on(c.LEAD_CAPTURE)}, идут в "
        + (f"чаты {', '.join(map(str, lead_targets()))}" if c.LEAD_CHAT_ID else "личку админам"),
        f"CRM: {'вебхук задан' if c.CRM_WEBHOOK_URL else '— не подключена'}" if c.PRO else "CRM: только в тарифе Про",
        f"Ежедневная сводка: {on(c.REPORT_DAILY)}" if c.PRO else "Ежедневная сводка: только в тарифе Про",
        f"Дожим: {on(c.FOLLOWUP_ENABLED)}, проверка каждые {c.FOLLOWUP_EVERY_MIN} мин",
        f"  клиенту — через {c.NUDGE_AFTER_HOURS:g} ч, если разговор не старше {c.NUDGE_WINDOW_HOURS:g} ч",
        f"  владельцу — если заявку не взяли {c.LEAD_REMIND_AFTER_MIN} мин",
        f"  {quiet}",
        c.contact_line() or "Контакт менеджера: — не задан",
    ]
    return "\n".join(lines)


@router.message(Command("report"))
async def cmd_report(message: Message, bot: Bot) -> None:
    """Сводка владельцу. /report — за 7 дней; /report 30 — за месяц;
    /report 7 send — отправить в чат студии."""
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()[1:]
    days = 7
    send = False
    for p in parts:
        if p.isdigit():
            days = max(1, min(int(p), 90))
        elif p.lower() in ("send", "отправить"):
            send = True
    text = await asyncio.to_thread(
        report.build, days, config.TIMEZONE_OFFSET, config.LEAD_CAPTURE, config.PRO
    )
    if send:
        sent = 0
        for chat_id in lead_targets():
            try:
                await bot.send_message(chat_id, text)
                sent += 1
            except Exception as exc:
                log.warning("Отчёт в %s не ушёл: %s", chat_id, exc)
        await message.answer(f"Отчёт отправлен: {sent} чат(ов).")
    else:
        await message.answer(text)


async def weekly_report(bot: Bot) -> int:
    """Раз в неделю, в назначенный день и час, отчёт уходит в чат студии.

    Отметка в followups (kind='report') защищает от повтора: цикл дожима
    крутится каждые 15 минут, а отправить нужно один раз.
    """
    if not config.REPORT_WEEKLY:
        return 0
    now = time.gmtime(time.time() + config.TIMEZONE_OFFSET * 3600)
    if now.tm_wday != config.REPORT_WEEKDAY or now.tm_hour < config.REPORT_HOUR:
        return 0
    if await asyncio.to_thread(db.report_sent_recently, 6 * 24, "report"):
        return 0
    text = await asyncio.to_thread(
        report.build, 7, config.TIMEZONE_OFFSET, config.LEAD_CAPTURE, config.PRO
    )
    sent = 0
    for chat_id in lead_targets():
        try:
            await bot.send_message(chat_id, text)
            sent += 1
        except Exception as exc:
            log.warning("Еженедельный отчёт в %s не ушёл: %s", chat_id, exc)
    if sent:
        await asyncio.to_thread(db.mark_followup, 0, "report")
    return sent


@router.message(Command("trial"))
async def cmd_trial(message: Message) -> None:
    """/trial — статус; /trial +7 — продлить; /trial 2026-09-30 — задать; /trial off."""
    if not is_admin(message.from_user.id):
        return
    arg = (message.text or "").split(maxsplit=1)[1:]
    if not arg:
        await message.answer(trial.status_line())
        return
    try:
        new = await asyncio.to_thread(trial.set_until, arg[0])
    except ValueError:
        await message.answer("Не понял дату. Примеры: /trial +7, /trial 2026-09-30, /trial off")
        return
    if new is None:
        await message.answer("Пробный период снят — бот работает без ограничения.")
    else:
        await message.answer(f"Готово. {trial.status_line()}")


async def trial_watch(bot: Bot) -> int:
    """Раз в день: напомнить админам за N дней, в день окончания — итоги
    владельцу и остановка. Отметки в базе защищают от повторов."""
    key = await asyncio.to_thread(trial.pending_warning)
    if not key:
        return 0
    status = trial.status_line()
    if key == "over":
        summary = await asyncio.to_thread(
            report.build, 7, config.TIMEZONE_OFFSET, config.LEAD_CAPTURE
        )
        for chat_id in lead_targets():
            try:
                await bot.send_message(chat_id, config.TRIAL_OVER_OWNER + "\n\n" + summary)
            except Exception as exc:
                log.warning("Итоги пробного периода в %s не ушли: %s", chat_id, exc)
        text = f"⏹ {status}\nВладельцу отправлены итоги. Продлить: /trial +7"
    else:
        text = f"⏳ {status}\nПродлить: /trial +7 · снять: /trial off"
    for admin in sorted(config.ADMIN_IDS):
        try:
            await bot.send_message(admin, text)
        except Exception as exc:
            log.warning("Напоминание о пробном периоде админу %s не ушло: %s", admin, exc)
    await asyncio.to_thread(trial.mark_sent, key)
    return 1


# --------------------------------------------------------------------------- #
#  Рассылки по своей базе (Про)
# --------------------------------------------------------------------------- #

# Черновик рассылки до подтверждения: admin_id -> (targets, text)
_broadcast_draft: dict[int, tuple[list[int], str]] = {}


def broadcast_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📣 Отправить", callback_data="bc:go"),
        InlineKeyboardButton(text="Отмена", callback_data="bc:no"),
    ]])


@router.message(Command("broadcast", "рассылка"))
async def cmd_broadcast(message: Message) -> None:
    """/broadcast текст — всем, кто писал за 60 дней.
    /broadcast керамика | текст — тем, кто спрашивал про керамику.
    Сначала показывает, сколько человек получит, и просит подтвердить."""
    if not is_admin(message.from_user.id):
        return
    if not config.PRO:
        await message.answer("Рассылки по базе клиентов доступны в тарифе «Про».")
        return
    body = (message.text or "").split(maxsplit=1)
    if len(body) < 2 or not body[1].strip():
        await message.answer(
            "Как пользоваться:\n"
            "/broadcast Текст сообщения — всем, кто писал за последние "
            f"{config.BROADCAST_DAYS} дней\n"
            "/broadcast керамика | Текст — только тем, кто спрашивал про керамику\n\n"
            "Пример:\n/broadcast керамика | До конца месяца керамика со скидкой 10%. "
            "Запишитесь на осмотр — посчитаем точно.\n\n"
            f"Одному человеку — не чаще раза в {config.BROADCAST_COOLDOWN_DAYS} дней. "
            "Ночью не отправляется."
        )
        return
    raw = body[1].strip()
    topic, text = "", raw
    if "|" in raw:
        topic, text = (x.strip() for x in raw.split("|", 1))
    if len(text) < 10:
        await message.answer(
            "После «|» должно идти само сообщение для клиентов — то, что они прочитают. "
            "Оно слишком короткое.\n\nПример:\n"
            "/broadcast керамика | До конца месяца керамика со скидкой 10%. "
            "Запишитесь на осмотр — посчитаем точно."
        )
        return
    if quiet_hours():
        await message.answer("Сейчас тихие часы — рассылку лучше запустить утром после "
                             f"{config.QUIET_TO_HOUR:02d}:00.")
        return
    targets = await asyncio.to_thread(
        db.broadcast_targets, topic, config.BROADCAST_DAYS, config.BROADCAST_COOLDOWN_DAYS
    )
    if not targets:
        await message.answer(
            "Некому отправлять: " + (f"никто не спрашивал про «{topic}»" if topic else "база пуста")
            + f" за {config.BROADCAST_DAYS} дней, либо всем уже писали недавно."
        )
        return
    _broadcast_draft[message.from_user.id] = (targets, text)
    who = f"спрашивали про «{topic}»" if topic else f"писали боту за {config.BROADCAST_DAYS} дней"
    await message.answer(
        f"Получат {len(targets)} чел. ({who}).\n\nТекст:\n{text}\n\nОтправляем?",
        reply_markup=broadcast_kb(),
    )


@router.callback_query(F.data.in_({"bc:go", "bc:no"}))
async def cb_broadcast(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    draft = _broadcast_draft.pop(call.from_user.id, None)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if call.data == "bc:no" or not draft:
        await call.message.answer("Отменено.")
        return
    targets, text = draft
    sent = failed = 0
    for uid in targets:
        try:
            await bot.send_message(uid, text, reply_markup=offer_booking())
            sent += 1
            await asyncio.to_thread(db.mark_followup, uid, "broadcast")
        except Exception as exc:
            failed += 1
            log.info("Рассылка: %s не доставлено (%s)", uid, type(exc).__name__)
        await asyncio.sleep(1 / max(config.BROADCAST_PER_SEC, 0.5))
    await call.message.answer(
        f"Готово: доставлено {sent}" + (f", не доставлено {failed} (заблокировали бота)" if failed else "")
    )
    log.info("Рассылка: %s доставлено, %s нет", sent, failed)


async def daily_report(bot: Bot) -> int:
    """Про: короткая сводка за вчера каждое утро в REPORT_HOUR."""
    if not (config.PRO and config.REPORT_DAILY):
        return 0
    now = time.gmtime(time.time() + config.TIMEZONE_OFFSET * 3600)
    if now.tm_hour < config.REPORT_HOUR:
        return 0
    if await asyncio.to_thread(db.report_sent_recently, 20, "daily"):
        return 0
    text = await asyncio.to_thread(
        report.build, 1, config.TIMEZONE_OFFSET, config.LEAD_CAPTURE
    )
    sent = 0
    for chat_id in lead_targets():
        try:
            await bot.send_message(chat_id, text)
            sent += 1
        except Exception as exc:
            log.warning("Ежедневная сводка в %s не ушла: %s", chat_id, exc)
    if sent:
        await asyncio.to_thread(db.mark_followup, 0, "daily")
    return sent


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(settings_report())


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    rows = await asyncio.to_thread(db.list_qa, 100000, 0, None)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["id", "question", "answer", "source", "hits", "created_at"])
    for r in rows:
        writer.writerow(
            [r["id"], r["question"], r["answer"], r["source"], r["hits"], r["created_at"]]
        )
    data = buf.getvalue().encode("utf-8-sig")
    await message.answer_document(
        BufferedInputFile(data, filename="knowledge.csv"),
        caption=f"Выгружено записей: {len(rows)}",
    )


@router.message(Command("reindex"))
async def cmd_reindex(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    if not ai.embeddings_enabled():
        await message.answer(
            "Эмбеддинги выключены (AI_EMBEDDING_MODEL пуст или не поддерживается "
            "провайдером). Поиск работает по FTS5 + нечёткому сравнению."
        )
        return
    rows = await asyncio.to_thread(db.rows_without_embeddings)
    done = 0
    for r in rows:
        vector = await ai.embed(f"{r['question']}\n{r['answer']}")
        if not vector:
            break
        await asyncio.to_thread(db.save_embedding, r["id"], config.AI_EMBEDDING_MODEL, vector)
        done += 1
    await message.answer(f"Проиндексировано: {done} из {len(rows)}")


_Q_NAMES = {"question", "вопрос", "услуга", "наименование", "название", "работа"}
_A_NAMES = {"answer", "ответ", "цена", "стоимость", "прайс", "описание"}


def decode_csv(raw: bytes) -> str:
    """Excel в России сохраняет CSV в cp1251, а не в UTF-8."""
    for encoding in ("utf-8-sig", "cp1251", "utf-16"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if "�" not in text:
            return text
    return raw.decode("utf-8-sig", errors="replace")


def _sniff(text: str) -> tuple[str, list[list[str]]]:
    """Выбрать разделитель по тому, какой даёт РОВНЫЕ колонки во всём файле.

    Считать символы нельзя: в ответах полно запятых («мойка, полировка,
    керамика»), и по количеству запятая всегда побеждает точку с запятой.
    А вот ровное число колонок в каждой строке даёт только настоящий
    разделитель — по нему и выбираем.
    """
    best_delim, best_rows, best_score = "", [], ()
    for delim in (";", ",", "\t"):
        try:
            rows = [
                r for r in csv.reader(io.StringIO(text), delimiter=delim)
                if any((c or "").strip() for c in r)
            ]
        except csv.Error:
            continue
        if not rows:
            continue
        counts = Counter(len(r) for r in rows)
        modal, n = counts.most_common(1)[0]
        if modal < 2:
            continue  # разделитель не сработал: всё одной колонкой
        # ровность колонок важнее всего; при равной — предпочитаем 2 колонки
        score = (n / len(rows), modal == 2, len(rows))
        if score > best_score:
            best_delim, best_rows, best_score = delim, rows, score
    return best_delim, best_rows


def parse_csv(text: str) -> tuple[list[tuple[str, str]], str]:
    """Разобрать CSV в пары (вопрос, ответ). Второе значение — текст ошибки."""
    text = text.strip("﻿\r\n ")
    if not text:
        return [], "Файл пустой."
    header = text.splitlines()[0]

    best, rows = _sniff(text)
    if not rows:
        return [], (
            "Не понял разделитель колонок. Нужен CSV: две колонки через «;» "
            "или «,».\nПервая строка файла:\n" + header[:200]
        )

    names = [(c or "").strip().lower().lstrip("﻿") for c in rows[0]]
    q_idx = next((i for i, n in enumerate(names) if n in _Q_NAMES), None)
    a_idx = next((i for i, n in enumerate(names) if n in _A_NAMES), None)

    if q_idx is not None and a_idx is not None:
        body = rows[1:]
    elif len(rows[0]) >= 2:
        # заголовка нет — считаем, что это просто две колонки
        q_idx, a_idx, body = 0, 1, rows
    else:
        return [], (
            "Не нашёл колонки с вопросом и ответом. Первая строка должна быть "
            "«question;answer» — либо пришлите файл без заголовка, две колонки.\n"
            "Первая строка файла:\n" + header[:200]
        )

    pairs, skipped = [], 0
    for r in body:
        if len(r) <= max(q_idx, a_idx):
            skipped += 1
            continue
        q, a = r[q_idx].strip(), r[a_idx].strip()
        if q and a:
            pairs.append((q, a))
        else:
            skipped += 1

    if not pairs:
        return [], (
            f"Ни одной готовой строки: разделитель «{best}», колонок в первой "
            f"строке {len(rows[0])}, пропущено {skipped}.\nПервая строка:\n"
            + header[:200]
        )
    return pairs, ""


@router.message(F.document)
async def handle_csv_import(message: Message, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        # Клиент прислал файл. Раньше здесь был молчаливый return, и человек
        # не получал ничего — выглядело как будто бот умер.
        await media_reply(message, "файл", config.MEDIA_FILE)
        return
    name = (message.document.file_name or "").lower()
    if not name.endswith(".csv"):
        await message.answer("Пришлите CSV с колонками question,answer")
        return

    file = await bot.get_file(message.document.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)

    text = decode_csv(buf.getvalue())
    pairs, problem = parse_csv(text)
    if problem:
        await message.answer(problem)
        return

    added = 0
    for q, a in pairs:
        await asyncio.to_thread(db.add_qa, q, a, "import", "", message.from_user.id)
        added += 1
    total = await asyncio.to_thread(db.count_qa)
    tail = "" if config.STRICT_CATALOG else " Запустите /reindex, если используете эмбеддинги."
    await message.answer(f"Импортировано: {added}. Всего в базе: {total}.{tail}")


# --------------------------------------------------------------------------- #
#  Запись на осмотр: превращаем вопрос про цену в лид
# --------------------------------------------------------------------------- #

class Booking(StatesGroup):
    confirm = State()
    name = State()
    phone = State()
    car = State()
    when = State()


# Клиент может попросить записаться словами, не нажимая кнопку
# Границу слова нельзя ставить сразу после основы: «запиш» + «ите» — не
# граница, и «запишите меня» не срабатывало. Поэтому основы с \w*.
_BOOK_RE = re.compile(
    r"\b(?:запиш\w*|записа\w*|запись|записыва\w*|"
    r"приеду|подъеду|подъехать|заеду|"
    r"хочу\s+приехать|можно\s+(?:подъехать|приехать|заехать)|"
    r"когда\s+можно\s+(?:подъехать|приехать))",
    re.IGNORECASE,
)


def wants_booking(text: str) -> bool:
    return bool(_BOOK_RE.search(text or ""))


def offer_booking() -> InlineKeyboardMarkup | None:
    """Кнопка записи под ответом — если захват лидов включён."""
    return booking_kb() if config.LEAD_CAPTURE else None


def booking_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=config.LEAD_BUTTON, callback_data="lead:start")
        ]]
    )


def phone_kb() -> ReplyKeyboardMarkup:
    """Кнопка «поделиться номером»: телефон приходит от Telegram, без опечаток."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def lead_targets() -> list[int]:
    """Кому слать лиды: чаты студии либо все админы.

    LEAD_CHAT_ID может быть списком через запятую — например, группа
    менеджеров и личка владельца. На «Стандарте» используется только первый.
    """
    ids = []
    for raw in config.LEAD_CHAT_ID.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ids.append(int(raw))
        except ValueError:
            log.error("LEAD_CHAT_ID: '%s' — не число, пропускаю", raw)
    if ids:
        return ids if config.PRO else ids[:1]
    return sorted(config.ADMIN_IDS)


async def crm_push(lead_id: int, data: dict, user) -> None:
    """Заявка в CRM одним POST (Про). Ошибка CRM не должна ломать заявку."""
    if not (config.PRO and config.CRM_WEBHOOK_URL):
        return
    payload = {
        "lead_id": lead_id,
        "name": data.get("name", ""),
        "phone": data.get("phone", ""),
        "car": data.get("car", ""),
        "when": data.get("when", ""),
        "service": data.get("service", ""),
        "telegram_id": getattr(user, "id", None),
        "telegram_username": getattr(user, "username", None),
        "studio": config.STUDIO_NAME,
        "source": "telegram-bot",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(config.CRM_WEBHOOK_URL, json=payload)
        if resp.status_code >= 300:
            log.warning("CRM вернула %s на заявку %s: %s", resp.status_code, lead_id, resp.text[:200])
        else:
            log.info("Заявка %s передана в CRM", lead_id)
    except Exception as exc:
        log.warning("CRM недоступна, заявка %s не передана: %s", lead_id, exc)


def lead_card(lead_id: int, data: dict, user) -> str:
    """Карточка лида для владельца студии."""
    handle = f"@{user.username}" if user.username else "без юзернейма"
    lines = [
        f"🔔 <b>Заявка №{lead_id}</b>",
        "",
        f"👤 {html.escape(data.get('name', ''))} — {html.escape(data.get('phone', ''))}",
    ]
    if data.get("car"):
        lines.append(f"🚗 {html.escape(data['car'])}")
    if data.get("when"):
        lines.append(f"🕒 Удобно: {html.escape(data['when'])}")
    if data.get("service"):
        lines.append(f"❓ Спрашивал: {html.escape(data['service'][:200])}")
    lines += [
        "",
        f'<a href="tg://user?id={user.id}">Написать в Telegram</a> ({html.escape(handle)})',
    ]
    return "\n".join(lines)


def lead_taken_kb(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Взял в работу", callback_data=f"lead:take:{lead_id}")
    ]])


async def notify_lead(bot: Bot, lead_id: int, data: dict, user) -> None:
    """Разослать карточку владельцу. Один недоступный чат не ломает остальные."""
    text = lead_card(lead_id, data, user)
    targets = lead_targets()
    if not targets:
        log.error("Лид %s некому отправить: LEAD_CHAT_ID и ADMIN_IDS пусты", lead_id)
        return
    for chat_id in targets:
        try:
            await bot.send_message(
                chat_id, text, parse_mode="HTML", reply_markup=lead_taken_kb(lead_id)
            )
        except Exception as exc:
            log.error("Не удалось отправить лид %s в чат %s: %s", lead_id, chat_id, exc)
    await crm_push(lead_id, data, user)


def known_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, всё верно", callback_data="lead:same")],
        [InlineKeyboardButton(text="Изменить данные", callback_data="lead:new")],
    ])


async def start_booking(
    message: Message, state: FSMContext, service: str = "", user=None
) -> None:
    """Начать запись. Если человек записывался раньше — не спрашиваем заново.

    Повторная заявка в два касания вместо четырёх шагов: контакт и машина
    у нас уже есть, меняется только время.
    """
    await state.update_data(service=service)
    user = user or message.from_user
    known = await asyncio.to_thread(db.get_client, user.id)

    if known and known["name"] and known["phone"]:
        await state.update_data(
            name=known["name"], phone=known["phone"], car=known["car"] or ""
        )
        lines = [f"👤 {known['name']} — {known['phone']}"]
        if known["car"]:
            lines.append(f"🚗 {known['car']}")
        await state.set_state(Booking.confirm)
        await message.answer(
            "Записываю на осмотр. Данные те же?\n\n" + "\n".join(lines),
            reply_markup=known_kb(),
        )
        return

    await state.set_state(Booking.name)
    await message.answer(
        "Записываю на осмотр. Как вас зовут?\n\n/cancel — отменить",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.callback_query(F.data == "lead:same", StateFilter(Booking.confirm))
async def cb_lead_same(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.set_state(Booking.when)
    await call.message.answer(
        "Когда вам удобно приехать? Можно примерно: «завтра после 18»."
    )


@router.callback_query(F.data == "lead:new", StateFilter(Booking.confirm))
async def cb_lead_new(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.set_state(Booking.name)
    await call.message.answer("Хорошо. Как вас зовут?\n\n/cancel — отменить")


@router.callback_query(F.data == "lead:start")
async def cb_lead_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    # что человек спрашивал перед записью — самое ценное для менеджера
    service = await asyncio.to_thread(db.last_question, call.from_user.id)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await start_booking(call.message, state, service, user=call.from_user)


@router.callback_query(F.data.startswith("lead:take:"))
async def cb_lead_take(call: CallbackQuery) -> None:
    """Заявку взяли в работу — напоминания по ней прекращаются."""
    lead_id = int(call.data.rsplit(":", 1)[1])
    await asyncio.to_thread(db.set_lead_status, lead_id, "taken")
    who = call.from_user.first_name or call.from_user.username or "менеджер"
    await call.answer("Отметил, напоминать не буду.")
    try:
        await call.message.edit_text(
            call.message.html_text + f"\n\n✅ В работе: {html.escape(who)}",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.message(Command("cancel"), StateFilter(Booking))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменил. Если что — спрашивайте про услуги и цены.",
                         reply_markup=ReplyKeyboardRemove())


@router.message(Command("zapis"))
@router.message(Command("record"))
async def cmd_record(message: Message, state: FSMContext) -> None:
    if not config.LEAD_CAPTURE:
        return
    await start_booking(message, state)


@router.message(Booking.name, F.text)
async def booking_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 60:
        await message.answer("Напишите, пожалуйста, имя — так к вам обратится мастер.")
        return
    await state.update_data(name=name)
    await state.set_state(Booking.phone)
    await message.answer(
        f"Приятно познакомиться, {name}. Оставьте номер телефона — "
        "нажмите кнопку ниже или напишите вручную.",
        reply_markup=phone_kb(),
    )


_PHONE_RE = re.compile(r"[\d+][\d\s()\-]{8,}")


@router.message(Booking.phone, F.contact)
async def booking_phone_contact(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=message.contact.phone_number)
    await _ask_car(message, state)


@router.message(Booking.phone, F.text)
async def booking_phone_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not _PHONE_RE.search(text):
        await message.answer(
            "Не похоже на номер. Напишите в формате +7 900 123-45-67 "
            "или нажмите кнопку ниже.",
            reply_markup=phone_kb(),
        )
        return
    await state.update_data(phone=text)
    await _ask_car(message, state)


async def _ask_car(message: Message, state: FSMContext) -> None:
    await state.set_state(Booking.car)
    await message.answer(
        "Какая у вас машина? Марка и модель — по ним мастер определит класс.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Booking.car, F.text)
async def booking_car(message: Message, state: FSMContext) -> None:
    await state.update_data(car=(message.text or "").strip()[:100])
    await state.set_state(Booking.when)
    await message.answer(
        "Когда вам удобно приехать? Можно примерно: «завтра после 18», "
        "«в субботу утром»."
    )


@router.message(Booking.when, F.text)
async def booking_when(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.update_data(when=(message.text or "").strip()[:100])
    data = await state.get_data()
    await state.clear()

    user = message.from_user
    lead_id = await asyncio.to_thread(
        db.add_lead,
        user.id,
        user.username,
        data.get("name", ""),
        data.get("phone", ""),
        data.get("car", ""),
        data.get("service", ""),
        data.get("when", ""),
    )
    log.info(
        "Новый лид %s: %s, %s, %s",
        lead_id, data.get("name"), data.get("car"), data.get("when"),
    )
    await notify_lead(bot, lead_id, data, user)

    text = config.LEAD_DONE
    contact = config.contact_line()
    if contact:
        text += f"\n\nСрочно — напишите напрямую. {contact}"
    await message.answer(text, reply_markup=ReplyKeyboardRemove())


@router.message(Command("leads"))
async def cmd_leads(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    rows = await asyncio.to_thread(db.list_leads, 15)
    if not rows:
        await message.answer("Заявок пока нет.")
        return
    stats = await asyncio.to_thread(db.lead_stats)
    lines = [f"Заявок всего: {stats['total']}, за неделю: {stats['week']}", ""]
    for r in rows:
        when = f", {r['when_text']}" if r["when_text"] else ""
        car = f" ({r['car']})" if r["car"] else ""
        lines.append(
            f"[{r['id']}] {r['created_at'][:16].replace('T', ' ')} — "
            f"{r['name']}{car}, {r['phone']}{when}"
        )
    await message.answer(clip("\n".join(lines)))


# --------------------------------------------------------------------------- #
#  Дожим: догоняем тех, кто спросил и пропал, и заявки, которые никто не взял
# --------------------------------------------------------------------------- #

def quiet_hours() -> bool:
    """Ночь по местному времени студии — людям не пишем.

    Уведомление в три часа ночи не приносит заявку, а вот отписку приносит.
    """
    if config.QUIET_FROM_HOUR == config.QUIET_TO_HOUR:
        return False  # круглосуточно
    hour = (time.gmtime().tm_hour + config.TIMEZONE_OFFSET) % 24
    if config.QUIET_FROM_HOUR < config.QUIET_TO_HOUR:
        return config.QUIET_FROM_HOUR <= hour < config.QUIET_TO_HOUR
    return hour >= config.QUIET_FROM_HOUR or hour < config.QUIET_TO_HOUR


async def nudge_clients(bot: Bot) -> int:
    """Написать тем, кто спрашивал цену и не записался."""
    rows = await asyncio.to_thread(
        db.users_to_nudge, config.NUDGE_AFTER_HOURS, config.NUDGE_WINDOW_HOURS
    )
    sent = 0
    for r in rows:
        user_id = r["user_id"]
        if is_admin(user_id):
            continue  # себя не дожимаем
        try:
            await bot.send_message(
                user_id, config.NUDGE_TEXT,
                reply_markup=booking_kb() if config.LEAD_CAPTURE else None,
            )
            sent += 1
        except Exception as exc:
            # заблокировал бота, удалил чат — отмечаем, чтобы не долбиться снова
            log.info("Дожим %s не доставлен: %s", user_id, exc)
        await asyncio.to_thread(db.mark_followup, user_id, "nudge")
    return sent


async def remind_owner(bot: Bot) -> int:
    """Напомнить про заявки, которые никто не взял в работу."""
    rows = await asyncio.to_thread(db.stale_leads, config.LEAD_REMIND_AFTER_MIN)
    sent = 0
    for lead in rows:
        age = config.LEAD_REMIND_AFTER_MIN
        text = (
            f"⏰ Заявка №{lead['id']} висит без ответа больше {age} мин.\n\n"
            f"👤 {html.escape(lead['name'])} — {html.escape(lead['phone'])}"
        )
        if lead["car"]:
            text += f"\n🚗 {html.escape(lead['car'])}"
        if lead["when_text"]:
            text += f"\n🕒 Удобно: {html.escape(lead['when_text'])}"
        if lead["user_id"]:
            text += f'\n\n<a href="tg://user?id={lead["user_id"]}">Написать клиенту</a>'
        for chat_id in lead_targets():
            try:
                await bot.send_message(
                    chat_id, text, parse_mode="HTML",
                    reply_markup=lead_taken_kb(lead["id"]),
                )
                sent += 1
            except Exception as exc:
                log.error("Напоминание по заявке %s не ушло: %s", lead["id"], exc)
        await asyncio.to_thread(
            db.mark_followup, lead["user_id"] or 0, "lead_remind", lead["id"]
        )
    return sent


async def followup_worker(bot: Bot) -> None:
    """Фоновый цикл. Живёт, пока работает polling.

    Дожим, еженедельный отчёт и пробный период — всё здесь. Цикл крутится
    всегда: даже с выключенным дожимом отчёт и напоминания о пробном
    периоде должны приходить.
    """
    nudges = config.FOLLOWUP_ENABLED and config.LEAD_CAPTURE
    if nudges:
        log.info(
            "Дожим включён: клиентам через %s ч, напоминание по заявкам через %s мин",
            config.NUDGE_AFTER_HOURS, config.LEAD_REMIND_AFTER_MIN,
        )
    log.info("Фоновая проверка каждые %s мин. %s", config.FOLLOWUP_EVERY_MIN, trial.status_line())
    while not _shutdown.is_set():
        try:
            await asyncio.wait_for(
                _shutdown.wait(), timeout=config.FOLLOWUP_EVERY_MIN * 60
            )
            return  # пришёл сигнал остановки
        except asyncio.TimeoutError:
            pass
        try:
            if quiet_hours():
                continue
            if nudges:
                nudged = await nudge_clients(bot)
                reminded = await remind_owner(bot)
                if nudged or reminded:
                    log.info("Дожим: клиентам %s, напоминаний владельцу %s", nudged, reminded)
            if await weekly_report(bot):
                log.info("Еженедельный отчёт отправлен")
            if await daily_report(bot):
                log.info("Ежедневная сводка отправлена")
            if await trial_watch(bot):
                log.info("Пробный период: напоминание отправлено")
        except Exception:
            log.exception("Сбой в фоновом дожиме")  # цикл не роняем


# --------------------------------------------------------------------------- #
#  Основной сценарий: база -> ИИ -> запись в базу
# --------------------------------------------------------------------------- #

@router.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def handle_question(
    message: Message, state: FSMContext, query: str | None = None
) -> None:
    # query приходит от расшифровки голосового; caption — подпись к фото или
    # видео. Человек присылает снимок и пишет под ним вопрос; для нас это
    # обычный текстовый вопрос.
    if query is None:
        query = (message.text or message.caption or "").strip()
    user = message.from_user
    if len(query) < config.MIN_QUESTION_LEN:
        await message.answer("Сформулируйте вопрос чуть подробнее.")
        return

    await asyncio.to_thread(db.remember_client, user.id, user.username)

    vector = await ai.embed(query)
    hits = await asyncio.to_thread(db.search, query, vector, max(config.CONTEXT_TOP_K, 3))
    best = hits[0] if hits else None

    # 1) Ответ найден в базе.
    #    В строгом режиме планка выше: почти точное совпадение отдаём даром,
    #    остальное пусть разбирает модель — она подставит нужный класс авто.
    direct_min = (
        max(config.MATCH_THRESHOLD, config.STRICT_DIRECT_MIN)
        if config.STRICT_CATALOG
        else config.MATCH_THRESHOLD
    )
    if best and best.score >= direct_min:
        await asyncio.to_thread(db.bump_hit, best.id)
        await asyncio.to_thread(
            db.log_query, user.id, user.username, query, best.id, best.score, False
        )
        await answer_rich(message, best.answer, offer_booking())
        return

    # 2) Просьба записаться словами, без нажатия кнопки
    if config.LEAD_CAPTURE and wants_booking(query):
        await start_booking(message, state, query)
        return

    # 3) Отсечки до вызова модели — они бесплатные, а вызов нет.
    if is_smalltalk(query):
        await message.answer(smalltalk_reply(query))
        await asyncio.to_thread(
            db.log_query, user.id, user.username, query, None, 0.0, False, "smalltalk"
        )
        return

    if not is_on_topic(query):
        log.info("Не по теме, в ИИ не пошло: %s", query[:60])
        await asyncio.to_thread(
            db.log_query, user.id, user.username, query, None, 0.0, False, "offtopic"
        )
        await message.answer(config.OFFTOPIC_REPLY)
        return

    if rate_limited(user.id):
        log.info("Лимит запросов у %s (@%s)", user.id, user.username)
        await asyncio.to_thread(
            db.log_query, user.id, user.username, query, None, 0.0, False, "limit"
        )
        await message.answer(config.RATE_LIMIT_REPLY)
        return

    # 4) Серая зона: лексически слабо, но по смыслу запись может подойти.
    #    Спрашиваем дешёвую модель — это заменяет эмбеддинги, которых у
    #    провайдера нет, и стоит куда дешевле полной генерации ответа.
    if (
        config.AI_RERANK
        and config.AI_API_KEY
        and not config.STRICT_CATALOG  # там весь каталог и так уходит модели
        and best
        and best.score >= config.RERANK_MIN_SCORE
    ):
        # берём всю тройку: слабый кандидат модели не мешает (она ответит 0),
        # а нужная запись нередко оказывается второй-третьей
        shortlist = [(h.id, h.question, h.answer) for h in hits[:3]]
        picked = await ai.rerank(query, shortlist)
        if picked:
            row = await asyncio.to_thread(db.get_qa, picked)
            if row:
                log.info("Переспрос: '%s' -> запись %s", query[:50], picked)
                await asyncio.to_thread(db.bump_hit, picked)
                await asyncio.to_thread(
                    db.log_query, user.id, user.username, query, picked, best.score, False
                )
                await answer_rich(message, row["answer"], offer_booking())
                return

    # 5) В базе нет — идём в ИИ
    if not config.AI_API_KEY:
        await asyncio.to_thread(
            db.log_query, user.id, user.username, query, None, best.score if best else 0,
            False, "noanswer",
        )
        await message.answer("В базе знаний нет ответа на этот вопрос, а ИИ не подключён.")
        return

    thinking = await message.answer("Секунду, уточняю…")
    # Контекст диалога — из базы, а не из памяти процесса: на сервере бот
    # перезапускается, и «а если ещё химчистку» не должно ломаться из-за этого.
    history = await asyncio.to_thread(db.dialog, user.id, config.DIALOG_TTL_HOURS)

    catalog = None
    context = None
    client_info = ""
    if config.STRICT_CATALOG:
        known = await asyncio.to_thread(db.get_client, user.id)
        if known and known["car"]:
            client_info = f"машина — {known['car']}"
        # Весь прайс в контекст: так модель может сложить несколько услуг
        # и подобрать цену по классу авто, не выходя за наши данные.
        rows = await asyncio.to_thread(db.catalog, config.CATALOG_MAX_ROWS)
        catalog = [(r["question"], r["answer"]) for r in rows]
    else:
        context = [
            (h.question, h.answer)
            for h in hits[: config.CONTEXT_TOP_K]
            if h.score > 0.40
        ]

    try:
        answer = await ai.ask(query, context, history, catalog, client_info)
    except Exception as exc:
        log.exception("Ошибка запроса к ИИ")
        # у таймаутов httpx текст пустой — тогда показываем хотя бы тип,
        # иначе в чат приходит «Не удалось получить ответ от ИИ:» и всё
        detail = str(exc).strip() or type(exc).__name__
        await asyncio.to_thread(
            db.log_query, user.id, user.username, query, None, 0.0, True, "error"
        )
        await thinking.edit_text(f"Не удалось получить ответ от ИИ: {detail}")
        return

    if not answer:
        await thinking.edit_text("ИИ вернул пустой ответ. Попробуйте переформулировать.")
        return

    await asyncio.to_thread(db.add_dialog, user.id, "user", query)
    await asyncio.to_thread(db.add_dialog, user.id, "assistant", answer)

    # 6) Сохраняем ответ ИИ в базу (мусорные реплики не сохраняем).
    #    В строгом режиме не сохраняем ничего: прайс наполняет человек, иначе
    #    пересказ модели станет источником цен наравне с настоящим прайсом.
    qa_id = None
    if config.AUTOSAVE_AI and not config.STRICT_CATALOG and is_saveable(query, answer):
        qa_id = await asyncio.to_thread(db.add_qa, query, answer, "ai", "", user.id)
        emb = await ai.embed(f"{query}\n{answer}")
        if emb:
            await asyncio.to_thread(
                db.save_embedding, qa_id, config.AI_EMBEDDING_MODEL, emb
            )

    # Модель сказала, что чего-то нет в прайсе, — для отчёта владельцу это
    # самое ценное: список того, что стоит добавить.
    outcome = "noanswer" if is_noanswer(answer) else "ai"
    await asyncio.to_thread(
        db.log_query, user.id, user.username, query, qa_id, 0.0, True, outcome
    )
    await thinking.delete()
    keyboard = feedback_kb(qa_id) if qa_id else offer_booking()
    await answer_rich(message, answer, keyboard)


# --------------------------------------------------------------------------- #
#  Всё, что не текст. Регистрируется последним: более точные обработчики
#  выше по файлу разбирают свои случаи первыми, сюда попадают остатки.
# --------------------------------------------------------------------------- #

@router.message(StateFilter(None), F.photo)
async def handle_photo(message: Message, state: FSMContext) -> None:
    if (message.caption or "").strip():
        # Подпись под фото — обычный вопрос, ведём по общему пути
        await handle_question(message, state)
        return
    await media_reply(message, "фото", config.MEDIA_PHOTO)


@router.message(StateFilter(None), F.video | F.animation)
async def handle_video(message: Message, state: FSMContext) -> None:
    if (message.caption or "").strip():
        await handle_question(message, state)
        return
    await media_reply(message, "видео", config.MEDIA_VIDEO)


@router.message(StateFilter(None), F.voice | F.video_note)
async def handle_voice(message: Message, state: FSMContext, bot: Bot) -> None:
    """Голосовое или видеокружок: расшифровать и ответить как на текст.

    Порядок важен. Сначала декодируем и меряем громкость — модель на тишине
    выдумывает слова, так что тихую запись к ней не пускаем вовсе. Только
    потом тратим лимит и зовём модель. Кружок — то же самое, у него просто
    есть ещё и картинка, которую мы выбрасываем.
    """
    media = message.voice or message.video_note
    is_circle = message.voice is None
    kind = "кружок" if is_circle else "голосовое"

    if not (config.VOICE_TRANSCRIBE and config.AI_API_KEY):
        await media_reply(message, kind, config.MEDIA_VOICE)
        return

    user = message.from_user
    duration = int(getattr(media, "duration", 0) or 0)
    if duration > config.VOICE_MAX_SEC:
        await media_reply(message, kind + "-длинное", config.VOICE_TOO_LONG)
        return

    listening = await message.answer(config.VOICE_LISTENING)

    async def unclear(note: str) -> None:
        await asyncio.to_thread(
            db.log_query, user.id, user.username, f"[{kind}: {note}]", None, 0.0, False, "voice"
        )
        await listening.edit_text(config.VOICE_UNCLEAR, reply_markup=offer_booking())

    try:
        file = await bot.get_file(media.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        raw = buf.getvalue()
    except Exception as exc:
        log.warning("Не скачалось %s: %s", kind, str(exc) or type(exc).__name__)
        await unclear("не скачалось")
        return

    audio = await asyncio.to_thread(voice.decode, raw)
    if audio is not None:
        if not audio.has_speech:
            log.info(
                "%s от %s без речи: звука %.1f с из %.1f, размах %.0f дБ",
                kind, user.id, audio.loud_seconds, audio.seconds, audio.range_db,
            )
            await unclear("тишина")
            return
        payload, fmt = audio.wav(), "wav"
    elif is_circle:
        # без декодера из кружка звук не вытащить
        await listening.edit_text(config.MEDIA_CIRCLE, reply_markup=offer_booking())
        return
    else:
        payload, fmt = raw, "ogg"   # декодера нет — шлём как есть, без проверки

    # расшифровка — такой же платный вызов, как ответ модели
    if rate_limited(user.id):
        await listening.edit_text(config.RATE_LIMIT_REPLY)
        return

    try:
        text = await ai.transcribe(payload, fmt)
    except Exception as exc:
        log.warning("Расшифровка не удалась: %s", str(exc) or type(exc).__name__)
        text = ""
    if not text:
        await unclear("не разобрано")
        return

    log.info("%s от %s (@%s), %s с: %s", kind, user.id, user.username, duration, text[:80])
    await asyncio.to_thread(
        db.log_query, user.id, user.username, f"[{kind}]", None, 0.0, False, "voice"
    )
    await listening.edit_text(config.VOICE_ECHO.format(text=text))
    await handle_question(message, state, query=text)


@router.message(StateFilter(None), F.audio)
async def handle_audio(message: Message) -> None:
    """Аудиофайл — это трек, а не вопрос: отвечаем отпиской."""
    await media_reply(message, "аудио", config.MEDIA_VOICE)


@router.message(StateFilter(None), F.sticker)
async def handle_sticker(message: Message) -> None:
    await media_reply(message, "стикер", config.MEDIA_STICKER)


@router.message(StateFilter(Booking))
async def handle_media_in_booking(message: Message, state: FSMContext) -> None:
    """Во время записи пришло что-то помимо ожидаемого ответа.

    Шаги записи ловят текст и контакт и стоят выше по файлу; сюда попадает
    фото, голосовое и прочее. Без этого человек, приславший фото посреди
    записи, не получал ничего и не понимал, что диалог ещё идёт.
    """
    # На шаге подтверждения ждём нажатия кнопки, а не текста, — иначе совет
    # «ответьте текстом» сбивает с толку сильнее, чем молчание.
    if await state.get_state() == Booking.confirm.state and (message.text or "").strip():
        await message.answer(config.BOOKING_PICK_BUTTON)
        return
    await message.answer(config.MEDIA_IN_BOOKING)


@router.message(StateFilter(None))
async def handle_unknown(message: Message) -> None:
    """Последняя сеть: опрос, геопозиция, неизвестная команда и прочее."""
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.answer(config.UNKNOWN_COMMAND)
        return
    await media_reply(message, "другое", config.MEDIA_OTHER)


@router.callback_query(F.data.startswith("fb:"))
async def handle_feedback(call: CallbackQuery) -> None:
    _, verdict, raw_id = call.data.split(":", 2)
    qa_id = int(raw_id)
    if verdict == "bad":
        await asyncio.to_thread(db.delete_qa, qa_id)
        await call.answer("Спасибо, удалил эту запись из базы.", show_alert=False)
    else:
        await call.answer("Спасибо!")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# --------------------------------------------------------------------------- #

async def run_once() -> None:
    """Один цикл жизни бота: определить сеть, запустить polling."""
    ok, proxy = await net.detect(net.TELEGRAM_PROBE, config.TELEGRAM_PROXY or None)
    if not ok:
        raise ConnectionError(
            "Нет доступа к api.telegram.org — ни напрямую, ни через прокси. "
            "Включите VPN (Happ и т.п.) и подождите."
        )

    if config.VOICE_TRANSCRIBE and not voice.AVAILABLE:
        log.warning(
            "Библиотека av не установлена — голосовые уйдут модели без проверки "
            "на тишину (на пустой записи она выдумывает слова), а кружки "
            "расшифровываться не будут. Установка: pip install av"
        )

    if config.AI_API_KEY:
        ai_ok, ai_proxy = await net.detect(
            config.AI_BASE_URL + "/models", config.AI_PROXY or proxy
        )
        ai.set_proxy(ai_proxy)
        if not ai_ok:
            log.warning("ИИ сейчас недоступен — бот будет отвечать только из базы.")
        else:
            await ai.ensure_model()

    log.info(
        "Сеть: Telegram %s | ИИ %s",
        f"через {proxy}" if proxy else "напрямую",
        f"через {ai.get_proxy()}" if ai.get_proxy() else "напрямую",
    )

    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(token=config.BOT_TOKEN, session=session)
    worker = None
    dp = Dispatcher()
    dp.include_router(router)
    try:
        me = await bot.me()
        # Если у токена настроен webhook (например, остался от другого сервиса),
        # getUpdates работать не будет — снимаем его.
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Бот запущен: @%s", me.username)

        worker = asyncio.create_task(followup_worker(bot))

        # Ждём либо падения polling, либо сигнала на остановку от systemd.
        polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        stopping = asyncio.create_task(_shutdown.wait())
        done, _ = await asyncio.wait(
            {polling, stopping}, return_when=asyncio.FIRST_COMPLETED
        )
        if stopping in done:
            log.info("Получен сигнал остановки — доигрываю текущие апдейты…")
            await dp.stop_polling()
        else:
            stopping.cancel()
        await polling  # пробрасываем исключение, если polling упал сам
    finally:
        if worker is not None:
            worker.cancel()
        await ai.close()
        await bot.session.close()


def _install_signal_handlers() -> None:
    """SIGTERM от systemd и Ctrl+C — останавливаемся штатно, а не на полуслове."""
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _shutdown.set)
        except NotImplementedError:
            pass  # Windows: остаётся обычный KeyboardInterrupt


async def main() -> None:
    config.validate()
    db.init_db()
    _install_signal_handlers()
    log.info("База: %s (%s записей)", config.DB_PATH, db.count_qa())
    if config.STRICT_CATALOG:
        log.info(
            "Режим прайса: отвечаем только по каталогу, автосохранение ответов ИИ выключено"
        )
    log.info(
        "ИИ: %s (%s), эмбеддинги: %s",
        config.AI_MODEL,
        config.AI_BASE_URL,
        config.AI_EMBEDDING_MODEL or "выключены",
    )

    delay = 10
    while not _shutdown.is_set():
        try:
            await run_once()
            delay = 10  # штатная остановка polling — перезапускаем сразу
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if _shutdown.is_set():
                break
            log.error("Сбой: %s", exc)
            log.info("Повтор через %s сек. (Ctrl+C — выход)", delay)
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=delay)
                break  # сигнал пришёл, пока ждали — выходим сразу
            except asyncio.TimeoutError:
                delay = min(delay * 2, 300)
    log.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
