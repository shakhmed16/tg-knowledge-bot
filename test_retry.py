# -*- coding: utf-8 -*-
"""Проверка повтора при сетевом сбое.

    python test_retry.py

Через VPN/SOCKS соединение в пуле протухает после простоя, и запрос падает
с ReadTimeout без текста. Здесь проверяется, что повтор с новым клиентом
это лечит, а при двух неудачах подряд ошибка внятная, а не пустая.
"""
import asyncio, os, sys
os.environ.setdefault("BOT_TOKEN","x"); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import httpx, ai

calls = {"n": 0}

class FakeClient:
    async def post(self, path, json=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("")          # пустой текст — как в реальности
        r = httpx.Response(200, json={"ok": True})
        return r
    async def aclose(self):
        pass

async def main():
    ai._client = FakeClient()
    ai.client = lambda: ai._client or FakeClient()
    # подменяем фабрику так, чтобы после сброса создавался новый FakeClient
    ai.client = lambda: FakeClient() if ai._client is None else ai._client

    r = await ai.post("/chat/completions", {"x": 1})
    print(f"OK   повтор сработал: запросов {calls['n']}, статус {r.status_code}")
    assert calls["n"] == 2 and r.status_code == 200

    # теперь оба раза падаем — должна быть внятная ошибка, не пустая
    calls["n"] = 0
    class AlwaysFail(FakeClient):
        async def post(self, path, json=None):
            calls["n"] += 1
            raise httpx.ReadTimeout("")
    ai._client = AlwaysFail()
    ai.client = lambda: AlwaysFail() if ai._client is None else ai._client
    try:
        await ai.post("/chat/completions", {"x": 1})
        print("FAIL исключения не было")
    except RuntimeError as e:
        text = str(e)
        ok = bool(text.strip()) and "ReadTimeout" in text
        print(f"{'OK  ' if ok else 'FAIL'} после 2 неудач: попыток {calls['n']}, текст: {text}")

asyncio.run(main())
