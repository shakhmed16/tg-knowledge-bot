"""Конфигурация бота: читается из .env (или переменных окружения)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "да"}


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

# Прокси (нужен, если Telegram/AnyModel не открываются напрямую).
# Примеры: socks5://127.0.0.1:1080  |  http://user:pass@host:3128
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()
AI_PROXY = os.getenv("AI_PROXY", "").strip()

AI_BASE_URL = os.getenv("AI_BASE_URL", "https://anymodel.org/v1").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "gpt-5.2").strip()
AI_EMBEDDING_MODEL = os.getenv("AI_EMBEDDING_MODEL", "").strip()
AI_SYSTEM_PROMPT = os.getenv(
    "AI_SYSTEM_PROMPT",
    "Ты — справочный ассистент. Отвечай кратко, по делу, на русском языке. "
    "Если не знаешь ответа — прямо скажи об этом, не выдумывай.",
).strip()

DB_PATH = str(BASE_DIR / os.getenv("DB_PATH", "knowledge.db"))
MATCH_THRESHOLD = float(os.getenv("MATCH_THRESHOLD", "0.55"))
CONTEXT_TOP_K = int(os.getenv("CONTEXT_TOP_K", "3"))
AUTOSAVE_AI = _bool("AUTOSAVE_AI", True)

# --- Переспрос («серая зона») ---------------------------------------------- #
# Если лучший результат ниже MATCH_THRESHOLD, но не совсем мимо, показываем
# кандидатов дешёвой модели и спрашиваем, отвечает ли какой-то из них.
# Так закрываются синонимы («вернуть покупку» = «вернуть товар»), которые
# поиск по словам не видит, а эмбеддингов у провайдера нет.
AI_RERANK = _bool("AI_RERANK", True)
AI_RERANK_MODEL = os.getenv("AI_RERANK_MODEL", "ag/gemini-3.7-flash-low").strip()
RERANK_MIN_SCORE = float(os.getenv("RERANK_MIN_SCORE", "0.25"))

# --- Голосовые ------------------------------------------------------------- #
# Голосовое расшифровывается чат-моделью с поддержкой аудио и дальше идёт по
# обычному пути как текстовый вопрос. Модель должна принимать аудио: из
# доступных это Gemini (ag/gemini-*-flash*). 3.7-flash-low — точная, ~12 с
# на сообщение; 2.5-flash-lite — 2 с, но путает слова («камне» вместо «камри»).
VOICE_TRANSCRIBE = _bool("VOICE_TRANSCRIBE", True)
AI_VOICE_MODEL = os.getenv("AI_VOICE_MODEL", "ag/gemini-3.7-flash-low").strip()
# Длиннее — не расшифровываем: дорого, и в полутораминутном монологе всё
# равно нет вопроса, на который можно ответить по прайсу.
VOICE_MAX_SEC = int(os.getenv("VOICE_MAX_SEC", "90"))

# --- Строгий режим: бот-консультант по прайсу ----------------------------- #
# Включённый STRICT_CATALOG меняет поведение на два ключевых пункта:
#   1) модели уходит ВЕСЬ каталог (а не топ-3 записи), поэтому она может
#      складывать услуги, сравнивать варианты и подбирать цену по классу авто;
#   2) модели запрещено называть то, чего в каталоге нет. Не нашла — отправляет
#      к менеджеру. Без этого модель охотно выдумывает правдоподобные цены.
STRICT_CATALOG = _bool("STRICT_CATALOG", False)
# Сколько записей влезает в контекст. Больше — дороже каждый запрос к ИИ.
CATALOG_MAX_ROWS = int(os.getenv("CATALOG_MAX_ROWS", "200"))
# В строгом режиме из базы отвечаем напрямую только при почти точном
# совпадении. Всё остальное идёт к модели вместе с каталогом — иначе на
# «химчистка на кроссовере» бот выдаёт цены по всем трём классам вместо
# нужного. Ниже этого скора вопрос уходит к модели.
STRICT_DIRECT_MIN = float(os.getenv("STRICT_DIRECT_MIN", "0.90"))
# Куда отправлять, когда ответа в каталоге нет
FALLBACK_CONTACT = os.getenv("FALLBACK_CONTACT", "").strip()

# Чем занимается компания — одна строка, чтобы бот понимал предметную область
BUSINESS_INFO = os.getenv("BUSINESS_INFO", "").strip()

# --- Защита от лишних трат ------------------------------------------------ #
# Вопрос, ни одно слово которого не встречается в каталоге, до модели не
# доходит: «сколько стоит шаурма» стоил бы столько же, сколько вопрос про
# керамику, — весь каталог уходит в контекст на каждый запрос.
TOPIC_GUARD = _bool("TOPIC_GUARD", True)
OFFTOPIC_REPLY = os.getenv(
    "OFFTOPIC_REPLY",
    "Я подсказываю только по услугам и ценам нашей студии. "
    "Спросите про мойку, химчистку, полировку, керамику, оклейку плёнкой, "
    "шумоизоляцию или тонировку — отвечу по прайсу.",
).strip()

# Свои слова, которые всегда считаются «по теме», через запятую.
# Пригодится, если клиенты спрашивают словами, которых нет в прайсе.
EXTRA_TOPIC_WORDS = os.getenv("EXTRA_TOPIC_WORDS", "").strip()

# Сколько обращений к ИИ разрешено одному человеку в час (0 — без лимита)
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "20"))
RATE_LIMIT_REPLY = os.getenv(
    "RATE_LIMIT_REPLY",
    "Слишком много вопросов подряд — сделаем паузу. "
    "Напишите чуть позже или свяжитесь с менеджером напрямую.",
).strip()

# --- Запись на осмотр ------------------------------------------------------ #
# После ответа с ценой бот предлагает записаться, собирает контакт и шлёт
# карточку лида владельцу. Это то место, где вопрос превращается в клиента:
# без него человек, узнавший цену, уходит звонить — или не звонит.
LEAD_CAPTURE = _bool("LEAD_CAPTURE", False)
# Куда слать лиды: id чата студии. Пусто — шлём всем из ADMIN_IDS.
LEAD_CHAT_ID = os.getenv("LEAD_CHAT_ID", "").strip()
LEAD_BUTTON = os.getenv("LEAD_BUTTON", "📝 Записаться на осмотр").strip()
LEAD_DONE = os.getenv(
    "LEAD_DONE",
    "Записал! Менеджер свяжется с вами, чтобы подтвердить время.",
).strip()


# Сколько часов реплика остаётся контекстом. Разговор трёхдневной давности —
# не контекст, а источник путаницы: «а если ещё химчистку» относится к
# сегодняшнему вопросу, а не к позавчерашнему.
DIALOG_TTL_HOURS = float(os.getenv("DIALOG_TTL_HOURS", "6"))

# --- Нетекстовые сообщения -------------------------------------------------- #
# Раньше на фото, голосовое, видео, стикер и файл бот не отвечал вообще
# ничего: человек присылал фотографию машины и получал тишину. Со стороны
# это выглядит не как «не умею», а как сломанный бот — и человек уходит.
# Подпись к фото или видео считается обычным вопросом и идёт по общему пути.
MEDIA_PHOTO = os.getenv(
    "MEDIA_PHOTO",
    "Фото получил. Точную цену по фотографии не назову — состояние кузова "
    "и покрытия видно только вживую. Напишите словами, что нужно сделать, "
    "и я отвечу по прайсу. Либо запишитесь на осмотр: мастер посмотрит "
    "машину и назовёт точную сумму.",
).strip()
# Показывается, когда расшифровка выключена (VOICE_TRANSCRIBE=false)
MEDIA_VOICE = os.getenv(
    "MEDIA_VOICE",
    "Голосовые я не расшифровываю — напишите, пожалуйста, текстом. "
    "Если удобнее голосом, оставьте заявку: менеджер перезвонит.",
).strip()
# Расшифровка включена, но ничего не разобрали или сервис не ответил
VOICE_UNCLEAR = os.getenv(
    "VOICE_UNCLEAR",
    "Не разобрал голосовое — напишите, пожалуйста, текстом.",
).strip()
VOICE_TOO_LONG = os.getenv(
    "VOICE_TOO_LONG",
    "Голосовое слишком длинное. Напишите, пожалуйста, коротко текстом — "
    "или запишитесь на осмотр, и менеджер всё обсудит по телефону.",
).strip()
VOICE_LISTENING = os.getenv("VOICE_LISTENING", "🎧 Слушаю…").strip()
# Кружок без установленного декодера (av): звук из видео не вытащить
MEDIA_CIRCLE = os.getenv(
    "MEDIA_CIRCLE",
    "Видеокружок посмотреть не смогу — напишите текстом или отправьте "
    "голосовое.",
).strip()
# Что услышали — показываем человеку, чтобы он видел, на что отвечает бот
VOICE_ECHO = os.getenv("VOICE_ECHO", "🎤 Вы сказали: «{text}»").strip()
MEDIA_VIDEO = os.getenv(
    "MEDIA_VIDEO",
    "Видео получил, но оценить по нему не смогу. Опишите словами, что нужно "
    "сделать, — отвечу по прайсу. Или запишитесь на осмотр.",
).strip()
MEDIA_STICKER = os.getenv(
    "MEDIA_STICKER",
    "Напишите вопрос текстом — подскажу по услугам и ценам.",
).strip()
MEDIA_FILE = os.getenv(
    "MEDIA_FILE",
    "Файлы я не читаю. Напишите вопрос текстом — отвечу по прайсу.",
).strip()
MEDIA_OTHER = os.getenv(
    "MEDIA_OTHER",
    "С таким сообщением я работать не умею. Напишите вопрос текстом — "
    "подскажу по услугам и ценам.",
).strip()
# Пришло нетекстовое, пока идёт запись на осмотр
MEDIA_IN_BOOKING = os.getenv(
    "MEDIA_IN_BOOKING",
    "Сейчас записываю вас — ответьте, пожалуйста, текстом. "
    "Чтобы прервать запись, отправьте /cancel",
).strip()
# На шаге «данные те же?» ждём нажатия кнопки, а не текста
BOOKING_PICK_BUTTON = os.getenv(
    "BOOKING_PICK_BUTTON",
    "Выберите кнопкой: записать на те же данные или ввести новые. "
    "Чтобы прервать запись, отправьте /cancel",
).strip()
# Команда, которой у бота нет
UNKNOWN_COMMAND = os.getenv(
    "UNKNOWN_COMMAND",
    "Такой команды у меня нет. Просто напишите вопрос словами — "
    "или отправьте /help",
).strip()
# Альбом из пяти фото не должен вызвать пять одинаковых ответов…
MEDIA_QUIET_SEC = float(os.getenv("MEDIA_QUIET_SEC", "25"))
# …а два кружка подряд с паузой — это два обращения, второе не глушим
MEDIA_QUIET_SINGLE_SEC = float(os.getenv("MEDIA_QUIET_SINGLE_SEC", "3"))

# --- Дожим и напоминания -------------------------------------------------- #
# Фоновая задача раз в FOLLOWUP_EVERY_MIN минут смотрит, кому написать.
FOLLOWUP_ENABLED = _bool("FOLLOWUP_ENABLED", False)
FOLLOWUP_EVERY_MIN = int(os.getenv("FOLLOWUP_EVERY_MIN", "15"))

# Человек спросил цену и не записался. Пишем один раз, через столько часов.
NUDGE_AFTER_HOURS = float(os.getenv("NUDGE_AFTER_HOURS", "3"))
# И только если разговор был не давнее этого: напоминание про вопрос
# недельной давности человек воспринимает как спам.
NUDGE_WINDOW_HOURS = float(os.getenv("NUDGE_WINDOW_HOURS", "48"))
NUDGE_TEXT = os.getenv(
    "NUDGE_TEXT",
    "Остались вопросы по услугам? Могу записать на осмотр — "
    "мастер посмотрит машину и назовёт точную цену.",
).strip()

# Заявку никто не взял в работу столько минут — напоминаем владельцу
LEAD_REMIND_AFTER_MIN = int(os.getenv("LEAD_REMIND_AFTER_MIN", "120"))

# Демо-режим: команда /demo доступна всем и проводит по возможностям бота.
# Включать только на демо-боте, у ботов студий — выключено.
DEMO_MODE = _bool("DEMO_MODE", False)

# --- Тариф --------------------------------------------------------------- #
# standard — базовый: ответы, запись, заявки, дожим, отчёт по понедельникам.
# pro — плюс ежедневная сводка, несколько чатов для заявок, передача заявок
# в CRM по вебхуку, месячный отчёт. Функции «Про» на тарифе «Стандарт»
# молча выключены, а в отчёте появляется строка о том, что упускается.
PLAN = os.getenv("PLAN", "standard").strip().lower()
PRO = PLAN == "pro"

# Рассылки по своей базе (Про): /broadcast текст  или  /broadcast керамика | текст
BROADCAST_DAYS = int(os.getenv("BROADCAST_DAYS", "60"))        # кому: писали за N дней
BROADCAST_COOLDOWN_DAYS = int(os.getenv("BROADCAST_COOLDOWN_DAYS", "14"))  # не чаще
BROADCAST_PER_SEC = float(os.getenv("BROADCAST_PER_SEC", "5"))  # темп, лимит Telegram ~30

# Ежедневная короткая сводка владельцу (Про), в REPORT_HOUR
REPORT_DAILY = _bool("REPORT_DAILY", True)
# Куда отправлять каждую заявку в CRM: JSON POST на этот адрес (Про).
# Подходит для входящего вебхука Bitrix24, Albato/Make и любого своего сервиса.
CRM_WEBHOOK_URL = os.getenv("CRM_WEBHOOK_URL", "").strip()

# --- Студия и пробный период ------------------------------------------------ #
# Имя студии — в приветствии: «Я помощник студии Detailing Pro».
STUDIO_NAME = os.getenv("STUDIO_NAME", "").strip()

# Дата окончания пробного периода, ГГГГ-ММ-ДД. Пусто — без ограничения.
# За TRIAL_WARN_DAYS дней бот напоминает админам, в день окончания шлёт
# владельцу итоги и останавливается: клиентам отвечает TRIAL_OVER_TEXT.
# Продлить без правки файла: /trial +7 (запоминается в базе).
TRIAL_UNTIL = os.getenv("TRIAL_UNTIL", "").strip()
TRIAL_WARN_DAYS = [
    int(x) for x in os.getenv("TRIAL_WARN_DAYS", "3,1").replace(" ", "").split(",") if x.isdigit()
]
TRIAL_OVER_TEXT = os.getenv(
    "TRIAL_OVER_TEXT",
    "Бот временно не принимает вопросы. Свяжитесь со студией напрямую.",
).strip()
TRIAL_OVER_OWNER = os.getenv(
    "TRIAL_OVER_OWNER",
    "Пробный период бота завершён — с этого момента он не отвечает клиентам. "
    "Ниже итоги за время работы. Чтобы продолжить, напишите нам.",
).strip()

# --- Отчёт владельцу -------------------------------------------------------- #
# Раз в неделю сводка уходит в чат студии (LEAD_CHAT_ID или админам):
# сколько написали, сколько записались, о чём спрашивали, чего нет в прайсе.
REPORT_WEEKLY = _bool("REPORT_WEEKLY", True)
REPORT_WEEKDAY = int(os.getenv("REPORT_WEEKDAY", "0"))   # 0 = понедельник
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "10"))        # по местному времени

# Не писать людям ночью: часы по местному времени студии (24ч = круглосуточно)
QUIET_FROM_HOUR = int(os.getenv("QUIET_FROM_HOUR", "21"))
QUIET_TO_HOUR = int(os.getenv("QUIET_TO_HOUR", "10"))
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "3"))  # МСК = UTC+3


def contact_line() -> str:
    """Контакт менеджера без задвоенной подписи.

    В .env обычно пишут «Менеджер: +7 900…». Если добавить свою подпись
    сверху, получится «Менеджер: Менеджер: +7 900…» — так и было.
    """
    if not FALLBACK_CONTACT:
        return ""
    return FALLBACK_CONTACT if ":" in FALLBACK_CONTACT else f"Менеджер: {FALLBACK_CONTACT}"


MIN_QUESTION_LEN = 3
MAX_ANSWER_LEN = 3900  # лимит Telegram — 4096, оставляем запас


def validate() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните.")
    if not AI_API_KEY:
        print("[warn] AI_API_KEY пуст — бот будет работать только по базе, без ИИ.")
    if not ADMIN_IDS:
        print("[warn] ADMIN_IDS пуст — админ-команды будут недоступны никому.")
    if LEAD_CAPTURE and not (LEAD_CHAT_ID or ADMIN_IDS):
        print(
            "[warn] LEAD_CAPTURE включён, но лиды слать некому: задайте "
            "LEAD_CHAT_ID или ADMIN_IDS."
        )
    if STRICT_CATALOG and AUTOSAVE_AI:
        # Иначе ответ модели про услугу, которой нет в прайсе, осядет в базе
        # и станет «фактом»: дальше бот будет выдавать его как ваш прайс.
        print(
            "[warn] STRICT_CATALOG=true вместе с AUTOSAVE_AI=true — ответы ИИ "
            "будут попадать в прайс. Поставьте AUTOSAVE_AI=false."
        )
