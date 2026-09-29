"""Диагностика подключения к ИИ: какие модели доступны и работает ли чат.

Запуск:  .venv\\Scripts\\python.exe check_ai.py
"""
from __future__ import annotations

import asyncio
import json

import httpx

import config
import net

BASE_CANDIDATES = [
    config.AI_BASE_URL,
    "https://anymodel.org/v1",
    "https://api.anymodel.org/v1",
    "https://anymodel.org/api/v1",
]

# Чем заменить модель из .env, если её нет на этом аккаунте.
# Набор моделей у разных аккаунтов и тарифов отличается.
PREFERRED_CHAT = (
    "cx/gpt-5.6-sol", "cx/gpt-5.5", "ag/claude-sonnet-4-6",
    "ag/gemini-3.7-flash-high", "glm/glm-5.3",
)
PREFERRED_RERANK = (
    "ag/gemini-3.7-flash-low", "ag/gemini-3.7-flash-medium",
    "glm/glm-5.3-flash", "cx/gpt-5.4-mini", "qwen/qwen3.6-flash",
)


def pick(ids: list[str], wanted: str, env_name: str, preferred: tuple[str, ...]) -> str:
    """Проверить модель из .env и подобрать замену, если её нет у провайдера."""
    if wanted and wanted in ids:
        print(f"\n  {env_name}='{wanted}' — доступна.")
        return wanted

    print(f"\n  !! {env_name}='{wanted}' на этом аккаунте НЕТ.")
    near = [m for m in ids if wanted and wanted.split("/")[-1].lower() in m.lower()]
    if near:
        print(f"  Похожие ID: {', '.join(near[:8])}")
    if near:
        choice = near[0]
    else:
        choice = next((m for m in preferred if m in ids), "") or (ids[0] if ids else wanted)
    print(f"  Беру вместо неё: {choice}")
    return choice


async def main() -> None:
    if not config.AI_API_KEY:
        print("AI_API_KEY не задан в .env")
        return

    ok, proxy = await net.detect(config.AI_BASE_URL + "/models", config.AI_PROXY or None)
    if not ok:
        _, proxy = await net.detect("https://api.telegram.org", config.AI_PROXY or None)
    print(f"Прокси: {proxy or 'не нужен (прямое соединение)'}\n")

    headers = {"Authorization": f"Bearer {config.AI_API_KEY}"}
    async with httpx.AsyncClient(proxy=proxy, timeout=60, trust_env=False) as client:
        for base in dict.fromkeys(BASE_CANDIDATES):
            print(f"=== {base} ===")
            try:
                r = await client.get(f"{base}/models", headers=headers)
            except Exception as exc:
                print(f"  /models — ошибка соединения: {exc}\n")
                continue
            print(f"  GET /models -> {r.status_code}")
            if r.status_code != 200:
                print(f"  {r.text[:200]}\n")
                continue

            try:
                items = r.json().get("data", [])
            except json.JSONDecodeError:
                print(f"  не JSON: {r.text[:200]}\n")
                continue

            ids = [str(m.get("id")) for m in items]
            print(f"  Доступно моделей: {len(ids)}")
            for mid in ids[:60]:
                print(f"    {mid}")
            if len(ids) > 60:
                print(f"    ... и ещё {len(ids) - 60}")

            model = pick(ids, config.AI_MODEL, "AI_MODEL", PREFERRED_CHAT)

            r2 = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Ответь одним словом: работает?"}],
                },
            )
            print(f"\n  POST /chat/completions ({model}) -> {r2.status_code}")
            if r2.status_code != 200:
                print(f"  {r2.text[:300]}\n")
                continue
            answer = r2.json()["choices"][0]["message"]["content"]
            print(f"  Ответ модели: {answer.strip()[:200]}")

            # --- модель переспроса: она делает смысловой поиск по базе ---
            rerank = pick(ids, config.AI_RERANK_MODEL, "AI_RERANK_MODEL", PREFERRED_RERANK)
            r3 = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={
                    "model": rerank,
                    "messages": [{"role": "user", "content": "Ответь одной цифрой: 2+2"}],
                    "max_tokens": 8,
                    "temperature": 0,
                },
            )
            print(f"\n  Переспрос ({rerank}) -> {r3.status_code}")
            if r3.status_code == 200:
                print(f"  Ответ: {r3.json()['choices'][0]['message']['content'].strip()[:50]}")
            else:
                print(f"  {r3.text[:200]}")
                print("  Переспрос не работает — можно выключить: AI_RERANK=false")

            emb = [m for m in ids if "embed" in m.lower()]
            print(f"\n  Модели эмбеддингов: {', '.join(emb) if emb else 'нет (это нормально)'}")

            print(
                "\n  ГОТОВО. Впишите в .env:"
                f"\n    AI_BASE_URL={base}"
                f"\n    AI_MODEL={model}"
                f"\n    AI_RERANK_MODEL={rerank}"
            )
            return

    print(
        "Рабочую комбинацию найти не удалось.\n"
        "  401/403 — ключ неверный или удалён: проверьте в личном кабинете AnyModel.\n"
        "  402 — закончился баланс: пополните счёт.\n"
        "  Ошибка соединения — включите VPN."
    )


if __name__ == "__main__":
    asyncio.run(main())
