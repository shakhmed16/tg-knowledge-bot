# -*- coding: utf-8 -*-
"""Проверка ответов на нетекстовые сообщения.

    python test_media.py

Telegram не нужен: хендлеры вызываются напрямую с подставными объектами.
Проверяем то, из-за чего бот выглядел сломанным: тишину в ответ на фото,
голосовое, файл и неизвестную команду, — и то, что подпись к фото
по-прежнему считается обычным вопросом.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("BOT_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config

config.LEAD_CAPTURE = True
config.ADMIN_IDS = {555}
config.MEDIA_QUIET_SEC = 25.0

import db

config.DB_PATH = os.path.join(tempfile.mkdtemp(), "media.db")
db.init_db()

import bot


class FakeUser:
    def __init__(self, uid=777, username="klient"):
        self.id = uid
        self.username = username


class FakeMessage:
    """Минимум, который читают хендлеры нетекстовых сообщений."""

    def __init__(self, user=None, **kw):
        self.text = kw.get("text")
        self.caption = kw.get("caption")
        self.photo = kw.get("photo")
        self.voice = kw.get("voice")
        self.video = kw.get("video")
        self.sticker = kw.get("sticker")
        self.document = kw.get("document")
        self.media_group_id = kw.get("media_group_id")
        self.from_user = user or FakeUser()
        self.sent: list[str] = []

    async def answer(self, text, **kw):
        self.sent.append(text)
        return self


class FakeDoc:
    def __init__(self, name="price.pdf"):
        self.file_name = name
        self.file_id = "x"


class FakeState:
    def __init__(self, state=None):
        self.state = state
        self.data: dict = {}

    async def get_state(self):
        return self.state

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state = None
        self.data = {}


def check(label, cond):
    print(f"{'OK  ' if cond else 'FAIL'} {label}")
    return cond


async def main() -> int:
    ok = True

    # --- фото без подписи --------------------------------------------------
    print("=== нетекстовые сообщения ===")
    bot._media_last.clear()
    m = FakeMessage(user=FakeUser(1), photo=["f"])
    await bot.handle_photo(m, FakeState())
    ok &= check("на фото есть ответ", len(m.sent) == 1)
    ok &= check("ответ про фото", "фотограф" in m.sent[0].lower())

    # --- аудиофайл и кружок (голосовые — в test_voice.py) -------------------
    bot._media_last.clear()
    m = FakeMessage(user=FakeUser(2), voice="v")
    await bot.handle_audio(m)
    ok &= check("на аудиофайл есть ответ", len(m.sent) == 1)
    ok &= check("сказали писать текстом", "текст" in m.sent[0].lower())

    # --- стикер ------------------------------------------------------------
    bot._media_last.clear()
    m = FakeMessage(user=FakeUser(3), sticker="s")
    await bot.handle_sticker(m)
    ok &= check("на стикер есть ответ", len(m.sent) == 1)

    # --- видео -------------------------------------------------------------
    bot._media_last.clear()
    m = FakeMessage(user=FakeUser(4), video="v")
    await bot.handle_video(m, FakeState())
    ok &= check("на видео есть ответ", len(m.sent) == 1)

    # --- альбом: пять фото, один ответ -------------------------------------
    print("\n=== альбом не должен спамить ===")
    bot._media_last.clear()
    replies = 0
    for _ in range(5):
        m = FakeMessage(user=FakeUser(5), photo=["f"], media_group_id="album-1")
        await bot.handle_photo(m, FakeState())
        replies += len(m.sent)
    ok &= check("на альбом из пяти фото один ответ", replies == 1)

    # два отдельных фото подряд — тоже одно сообщение, а не два
    bot._media_last.clear()
    replies = 0
    for _ in range(2):
        m = FakeMessage(user=FakeUser(6), photo=["f"])
        await bot.handle_photo(m, FakeState())
        replies += len(m.sent)
    ok &= check("два фото подряд — один ответ", replies == 1)

    # разные люди друг другу не мешают
    bot._media_last.clear()
    a = FakeMessage(user=FakeUser(7), photo=["f"])
    b = FakeMessage(user=FakeUser(8), photo=["f"])
    await bot.handle_photo(a, FakeState())
    await bot.handle_photo(b, FakeState())
    ok &= check("разным людям отвечаем обоим", len(a.sent) == 1 and len(b.sent) == 1)

    # --- подпись к фото = обычный вопрос -----------------------------------
    print("\n=== подпись под фото ===")
    seen = {}

    async def fake_question(message, state):
        seen["query"] = (message.text or message.caption or "").strip()

    real = bot.handle_question
    bot.handle_question = fake_question
    try:
        bot._media_last.clear()
        m = FakeMessage(user=FakeUser(9), photo=["f"], caption="сколько стоит керамика")
        await bot.handle_photo(m, FakeState())
        ok &= check("подпись ушла в обычный путь", seen.get("query") == "сколько стоит керамика")
        ok &= check("шаблонного ответа про фото не было", not m.sent)
    finally:
        bot.handle_question = real

    # --- файл от клиента ---------------------------------------------------
    print("\n=== файл от клиента ===")
    bot._media_last.clear()
    m = FakeMessage(user=FakeUser(10), document=FakeDoc())
    await bot.handle_csv_import(m, None)
    ok &= check("клиенту на файл ответили", len(m.sent) == 1)
    ok &= check("не начали импорт", "Импортировано" not in m.sent[0])

    # --- неизвестная команда ----------------------------------------------
    print("\n=== неизвестная команда ===")
    bot._media_last.clear()
    m = FakeMessage(user=FakeUser(11), text="/zakaz")
    await bot.handle_unknown(m)
    ok &= check("на /zakaz есть ответ", len(m.sent) == 1)
    ok &= check("подсказали /help", "/help" in m.sent[0])

    # --- нетекстовое посреди записи ---------------------------------------
    print("\n=== запись на осмотр не рвётся ===")
    m = FakeMessage(user=FakeUser(12), photo=["f"])
    await bot.handle_media_in_booking(m, FakeState(bot.Booking.name.state))
    ok &= check("подсказали ответить текстом", "/cancel" in m.sent[0])

    m = FakeMessage(user=FakeUser(13), text="да те же")
    await bot.handle_media_in_booking(m, FakeState(bot.Booking.confirm.state))
    ok &= check("на шаге подтверждения зовём нажать кнопку", "кнопк" in m.sent[0].lower())

    print("\nИТОГ:", "всё зелёное" if ok else "есть падения")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
