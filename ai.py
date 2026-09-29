"""Клиент к AnyModel (OpenAI-совместимый API): чат + эмбеддинги."""
from __future__ import annotations

import base64
import logging
import re
from typing import Optional

import httpx

import config

log = logging.getLogger("ai")

_client: Optional[httpx.AsyncClient] = None
_proxy: Optional[str] = config.AI_PROXY or None
# Выключается автоматически, если у провайдера нет /embeddings
_embeddings_enabled = bool(config.AI_EMBEDDING_MODEL)


def embeddings_enabled() -> bool:
    return _embeddings_enabled


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.AI_BASE_URL,
            proxy=_proxy,
            headers={
                "Authorization": f"Bearer {config.AI_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(180.0, connect=20.0),
        )
    return _client


def set_proxy(proxy: Optional[str]) -> None:
    """Задать прокси для запросов к ИИ (сбрасывает текущего клиента)."""
    global _proxy, _client
    if proxy != _proxy:
        _proxy = proxy
        _client = None


def get_proxy() -> Optional[str]:
    return _proxy


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _reset() -> None:
    """Выбросить клиента вместе с пулом соединений."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


async def post(path: str, payload: dict) -> httpx.Response:
    """POST с одним повтором на сетевых сбоях.

    Соединения в пуле протухают: через VPN/SOCKS провайдер молча рвёт их
    после простоя, и следующий запрос падает с ReadTimeout без текста
    («Не удалось получить ответ от ИИ:» — и пусто). Повтор с новым клиентом
    закрывает этот случай.
    """
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return await client().post(path, json=payload)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last = exc
            log.warning(
                "Сеть до ИИ (попытка %s/2): %s: %s",
                attempt, type(exc).__name__, str(exc) or "без текста",
            )
            await _reset()
    raise RuntimeError(
        f"нет связи с {config.AI_BASE_URL} ({type(last).__name__}). "
        "Проверьте VPN и попробуйте ещё раз."
    ) from last


_models_cache: list[str] = []

# Порядок предпочтения, если модель из .env недоступна
PREFERRED_MODELS = (
    "cx/gpt-5.6-sol",
    "cx/gpt-5.5",
    "ag/claude-sonnet-4-6",
    "ag/gemini-3.7-flash-high",
    "ag/gemini-3.7-flash-medium",
    "glm/glm-5.3",
)


async def list_models() -> list[str]:
    """Список ID моделей у провайдера (кешируется)."""
    global _models_cache
    if _models_cache or not config.AI_API_KEY:
        return _models_cache
    try:
        resp = await client().get("/models")
        resp.raise_for_status()
        _models_cache = [str(m.get("id")) for m in resp.json().get("data", []) if m.get("id")]
    except Exception as exc:
        log.warning("Не удалось получить список моделей: %s", exc)
    return _models_cache


async def ensure_model() -> None:
    """Проверить модель из .env и при необходимости подобрать рабочую.

    Заодно включает эмбеддинги, если у провайдера есть подходящая модель.
    """
    global _embeddings_enabled
    ids = await list_models()
    if not ids:
        return

    if config.AI_MODEL not in ids:
        pick = next((m for m in PREFERRED_MODELS if m in ids), ids[0])
        log.warning(
            "Модель '%s' недоступна у провайдера — использую '%s'. "
            "Впишите её в .env, чтобы закрепить.", config.AI_MODEL, pick,
        )
        config.AI_MODEL = pick

    if config.AI_RERANK and config.AI_RERANK_MODEL not in ids:
        cheap = next(
            (m for m in ids if any(k in m.lower() for k in ("flash-low", "flash", "mini"))),
            config.AI_MODEL,
        )
        log.warning(
            "Модель переспроса '%s' недоступна — использую '%s'.",
            config.AI_RERANK_MODEL, cheap,
        )
        config.AI_RERANK_MODEL = cheap

    if config.VOICE_TRANSCRIBE and config.AI_VOICE_MODEL not in ids:
        # аудио из чат-моделей принимают только Gemini
        voice = next((m for m in ids if "gemini" in m.lower() and "flash" in m.lower()), None)
        if voice:
            log.warning(
                "Модель для голосовых '%s' недоступна — использую '%s'.",
                config.AI_VOICE_MODEL, voice,
            )
            config.AI_VOICE_MODEL = voice
        else:
            log.warning(
                "Модель для голосовых '%s' недоступна, замены нет — "
                "голосовые отвечаем отпиской.", config.AI_VOICE_MODEL,
            )
            config.VOICE_TRANSCRIBE = False

    if not config.AI_EMBEDDING_MODEL:
        emb = [m for m in ids if "embed" in m.lower()]
        if emb:
            config.AI_EMBEDDING_MODEL = emb[0]
            _embeddings_enabled = True
            log.info(
                "Найдена модель эмбеддингов %s — умный поиск включён. "
                "Выполните /reindex, чтобы проиндексировать записи в базе.", emb[0],
            )
        else:
            log.info(
                "У провайдера нет моделей эмбеддингов — смысловой поиск идёт "
                "через переспрос модели %s.",
                config.AI_RERANK_MODEL if config.AI_RERANK else "(выключен)",
            )


def strict_prompt() -> str:
    """Промпт для режима прайса: ни одной цифры мимо каталога."""
    parts = [
        "Ты — консультант компании. Отвечай коротко, вежливо, на русском.",
        "",
        "ЗАПРЕЩЕНО: называть цену, услугу, срок или условие, которых нет в "
        "каталоге ниже. Никаких «примерно», «обычно», «в среднем по рынку». "
        "Нет в каталоге — говоришь, что этого нет, и отправляешь к менеджеру. "
        "Не подставляй похожую услугу вместо спрошенной.",
        "",
        "РАЗРЕШЕНО И НУЖНО (это не выдумывание, а работа с каталогом):",
        "• Понимать вопрос по смыслу: «почём убрать вмятину» = удаление вмятин, "
        "«обработать кожу» = консервант кожи.",
        "• Определять класс автомобиля по правилам классификации из каталога. "
        "Клиент говорит «джип», «кроссовер», «Camry» — сам сопоставь с классом "
        "и назови цену именно для него, не переспрашивая.",
        "• Складывать цены нескольких услуг и называть итог.",
        "• Объяснить своими словами, что за услуга и зачем она нужна (что такое "
        "керамика, чем полировка отличается от мойки). Это общие знания о ремесле.",
        "",
        "Но всё, что касается ИМЕННО НАС, — цены, сроки работ, гарантии, состав "
        "работ, материалы, наличие услуги — только из каталога. «Керамика "
        "держится дольше воска» сказать можно; «наша керамика держится 3 года» — "
        "нельзя, если этого нет в каталоге.",
        "",
        "Как отвечать про цены:",
        "• Клиент назвал автомобиль — назови цену для его класса и укажи, какой "
        "это класс.",
        "• Клиент автомобиль не назвал — приведи цены по всем классам или спроси "
        "марку и модель.",
        "• Несколько услуг — перечисли каждую с ценой, потом итог. Услугу, "
        "которой нет в каталоге, в сумму не включай и скажи о ней отдельно.",
        "• Цену «от» называй именно как «от»: это минимум, а не итог.",
    ]
    if config.BUSINESS_INFO:
        parts += ["", f"О компании: {config.BUSINESS_INFO}"]
    contact = config.contact_line()
    if contact:
        parts += ["", f"Контакт для клиента — {contact}"]
    return "\n".join(parts)


async def ask(
    question: str,
    context: list[tuple[str, str]] | None = None,
    history: list[dict] | None = None,
    catalog: list[tuple[str, str]] | None = None,
    client_info: str = "",
) -> str:
    """Задать вопрос модели.

    context — пары (вопрос, ответ) из базы знаний;
    history — последние реплики диалога, чтобы понимать «нет», «а если...» и т.п.;
    catalog — весь каталог: включает строгий режим, ответ только по нему.
    """
    if not config.AI_API_KEY:
        raise RuntimeError("AI_API_KEY не задан")

    # ВАЖНО: всё системное — в ОДНО сообщение. Некоторые провайдеры (в том
    # числе AnyModel) второе system-сообщение молча игнорируют, и модель
    # отвечает так, будто каталога ей не давали.
    if catalog:
        block = "\n".join(f"- {q}: {a}" for q, a in catalog)
        system = strict_prompt()
        if client_info:
            # машина известна с прошлого разговора — не заставляем называть заново
            system += f"\n\nЧто мы знаем о клиенте: {client_info}"
        system += "\n\nКАТАЛОГ:\n" + block
    else:
        system = config.AI_SYSTEM_PROMPT
        if context:
            ctx = "\n\n".join(f"В: {q}\nО: {a}" for q, a in context)
            system += (
                "\n\nНиже — похожие записи из базы знаний. Используй их, если они "
                "относятся к вопросу; если нет — отвечай самостоятельно и не "
                "ссылайся на них.\n\n" + ctx
            )
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-6:])
    messages.append({"role": "user", "content": question})

    resp = await post(
        "/chat/completions",
        {"model": config.AI_MODEL, "messages": messages, "temperature": 0.3},
    )
    if resp.status_code >= 400:
        detail = resp.text[:400].replace("\n", " ")
        hint = ""
        if resp.status_code == 404:
            hint = (
                f" Похоже, модель '{config.AI_MODEL}' не существует у провайдера. "
                "Запустите check_ai.py — он покажет список доступных ID."
            )
        raise RuntimeError(f"{resp.status_code} от {config.AI_BASE_URL}: {detail}.{hint}")
    data = resp.json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Неожиданный ответ API: {str(data)[:300]}")


_RERANK_PROMPT = (
    "Ты — фильтр базы знаний. Тебе дают вопрос пользователя и пронумерованные "
    "записи из базы. Определи, какая запись действительно отвечает на вопрос — "
    "по смыслу, а не по совпадению слов.\n"
    "Ответь ОДНИМ числом: номером записи или 0, если ни одна не отвечает.\n"
    "Ставь 0, если запись про другой объект, другое место, другой период или "
    "отвечает лишь частично. Никаких пояснений — только цифра."
)
_NUM_RE = re.compile(r"\d+")


async def rerank(question: str, candidates: list[tuple[int, str, str]]) -> Optional[int]:
    """Спросить дешёвую модель, отвечает ли какая-то из записей на вопрос.

    candidates — [(qa_id, вопрос, ответ)]. Возвращает qa_id или None.
    Заменяет эмбеддинги там, где их нет: одна короткая дешёвая проверка вместо
    полноценной генерации ответа.
    """
    if not candidates or not config.AI_API_KEY or not config.AI_RERANK:
        return None

    listing = "\n\n".join(
        f"{i}. В: {q}\n   О: {a[:400]}" for i, (_, q, a) in enumerate(candidates, 1)
    )
    try:
        resp = await post(
            "/chat/completions",
            {
                "model": config.AI_RERANK_MODEL or config.AI_MODEL,
                "messages": [
                    {"role": "system", "content": _RERANK_PROMPT},
                    {"role": "user", "content": f"Вопрос: {question}\n\nЗаписи:\n{listing}"},
                ],
                "temperature": 0,
                "max_tokens": 8,
            },
        )
        resp.raise_for_status()
        raw = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        log.warning("Переспрос не удался (%s) — иду в ИИ за полным ответом", exc)
        return None

    match = _NUM_RE.search(raw)
    if not match:
        log.warning("Переспрос вернул '%s' — не число, иду в ИИ", raw[:60])
        return None
    idx = int(match.group())
    if 1 <= idx <= len(candidates):
        return candidates[idx - 1][0]
    return None  # 0 или мусор — в базе ответа нет


_TRANSCRIBE_PROMPT = (
    "Расшифруй голосовое сообщение дословно, на русском языке. Верни только "
    "текст сказанного, без комментариев, кавычек и пояснений. Названия марок "
    "машин и услуг пиши так, как их произнесли. Если речи нет или разобрать "
    "невозможно — верни ровно один символ: —"
)


async def transcribe(audio: bytes, fmt: str = "ogg") -> str:
    """Текст голосового сообщения. Пустая строка — не разобрали.

    Отдельный STT-эндпоинт у провайдера есть, но моделей за ним сейчас нет
    (whisper и transcribe отвечают «temporarily unavailable»). Зато чат-модели
    Gemini принимают аудио прямо в сообщении — этим и пользуемся. Голосовое
    в Telegram — это ogg/opus, Gemini его понимает без перекодирования.
    """
    if not config.AI_API_KEY or not audio:
        return ""
    payload = {
        "model": config.AI_VOICE_MODEL,
        "temperature": 0,
        "max_tokens": 400,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _TRANSCRIBE_PROMPT},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(audio).decode("ascii"),
                    "format": fmt,
                }},
            ],
        }],
    }
    resp = await post("/chat/completions", payload)
    if resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code} от {config.AI_BASE_URL}: {resp.text[:200]}")
    text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    # модель иногда всё же оборачивает в кавычки — снимаем
    text = text.strip("«»\"'“” ")
    if text in ("", "—", "-", "–") or len(text) < 2:
        return ""
    return text


async def embed(text: str) -> Optional[list[float]]:
    """Вектор текста. None — если эмбеддинги не настроены или провайдер их не отдаёт."""
    global _embeddings_enabled
    if not _embeddings_enabled or not config.AI_API_KEY:
        return None
    try:
        resp = await post(
            "/embeddings",
            {"model": config.AI_EMBEDDING_MODEL, "input": text[:8000]},
        )
        if resp.status_code in (400, 404, 405, 501):
            log.warning(
                "Провайдер не поддерживает /embeddings (%s) — переключаюсь на "
                "поиск без эмбеддингов.", resp.status_code
            )
            _embeddings_enabled = False
            return None
        resp.raise_for_status()
        return list(resp.json()["data"][0]["embedding"])
    except Exception as exc:  # сеть, таймаут, неожиданный формат
        log.warning("Ошибка эмбеддинга: %s", exc)
        return None
