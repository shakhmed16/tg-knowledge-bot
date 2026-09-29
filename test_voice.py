# -*- coding: utf-8 -*-
"""Голосовые: расшифровка и маршрут дальше как обычный вопрос.

    python test_voice.py

Первая часть — без сети: сам сервис расшифровки подменяется, проверяется
логика бота (эхо «вы сказали», отписка на длинное и неразборчивое, лимит).
Вторая часть — живой прогон через AnyModel, если в .env есть AI_API_KEY и
рядом лежит voice/sample.ogg. Без ключа она просто пропускается.
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
config.VOICE_TRANSCRIBE = True
config.VOICE_MAX_SEC = 90
config.RATE_LIMIT_PER_HOUR = 3

import db

config.DB_PATH = os.path.join(tempfile.mkdtemp(), "voice.db")
db.init_db()

import ai
import bot
import voice


class FakeUser:
    def __init__(self, uid=777, username="klient"):
        self.id = uid
        self.username = username


class FakeVoice:
    def __init__(self, duration=5):
        self.duration = duration
        self.file_id = "voice-1"


class FakeMessage:
    def __init__(self, user=None, duration=5, circle=False):
        self.voice = None if circle else FakeVoice(duration)
        self.video_note = FakeVoice(duration) if circle else None
        self.text = None
        self.caption = None
        self.media_group_id = None
        self.from_user = user or FakeUser()
        self.sent: list[str] = []
        self.edits: list[str] = []

    async def answer(self, text, **kw):
        self.sent.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.edits.append(text)
        return self


class FakeFile:
    file_path = "voice/1.oga"


SPEECH = open(os.path.join("voice", "sample.ogg"), "rb").read()
SILENCE = open(os.path.join("voice", "silence.ogg"), "rb").read()
CIRCLE = open(os.path.join("voice", "circle.mp4"), "rb").read()


class FakeBot:
    def __init__(self, data=SPEECH):
        self.data = data

    async def get_file(self, file_id):
        return FakeFile()

    async def download_file(self, path, buf):
        buf.write(self.data)


class FakeState:
    async def get_state(self):
        return None


def check(label, cond):
    print(f"{'OK  ' if cond else 'FAIL'} {label}")
    return cond


async def main() -> int:
    ok = True
    asked: list[str] = []

    async def fake_question(message, state, query=None):
        asked.append(query)

    async def transcribe_ok(audio, fmt="ogg"):
        return "сколько стоит керамика на камри"

    async def transcribe_empty(audio, fmt="ogg"):
        return ""

    async def transcribe_boom(audio, fmt="ogg"):
        raise RuntimeError("503 от провайдера")

    real_q, real_t = bot.handle_question, ai.transcribe
    bot.handle_question = fake_question
    try:
        print("=== расшифровка удалась ===")
        ai.transcribe = transcribe_ok
        m = FakeMessage(FakeUser(1))
        await bot.handle_voice(m, FakeState(), FakeBot())
        ok &= check("показали «слушаю»", m.sent and "Слушаю" in m.sent[0])
        ok &= check("показали, что услышали", m.edits and "керамика на камри" in m.edits[-1])
        ok &= check("вопрос ушёл по обычному пути", asked == ["сколько стоит керамика на камри"])

        print("\n=== ничего не разобрали ===")
        ai.transcribe = transcribe_empty
        asked.clear()
        m = FakeMessage(FakeUser(2))
        await bot.handle_voice(m, FakeState(), FakeBot())
        ok &= check("попросили написать текстом", m.edits and "текстом" in m.edits[-1])
        ok &= check("к модели не пошли", not asked)

        print("\n=== сервис упал ===")
        ai.transcribe = transcribe_boom
        m = FakeMessage(FakeUser(3))
        await bot.handle_voice(m, FakeState(), FakeBot())
        ok &= check("не упали, а отписались", m.edits and "текстом" in m.edits[-1])

        print("\n=== слишком длинное ===")
        ai.transcribe = transcribe_ok
        asked.clear()
        m = FakeMessage(FakeUser(4), duration=200)
        await bot.handle_voice(m, FakeState(), FakeBot())
        ok &= check("отказали без расшифровки", m.sent and "длинное" in m.sent[0] and not asked)

        print("\n=== лимит в час ===")
        bot._ai_calls.clear()
        u = FakeUser(5)
        hits = 0
        for _ in range(5):
            asked.clear()
            m = FakeMessage(u)
            await bot.handle_voice(m, FakeState(), FakeBot())
            hits += bool(asked)
        ok &= check("после лимита голосовые не расшифровываем", hits == config.RATE_LIMIT_PER_HOUR)

        print("\n=== расшифровка выключена ===")
        config.VOICE_TRANSCRIBE = False
        bot._media_last.clear()
        asked.clear()
        m = FakeMessage(FakeUser(6))
        await bot.handle_voice(m, FakeState(), FakeBot())
        ok &= check("старая отписка", m.sent and "не расшифровываю" in m.sent[0] and not asked)
        config.VOICE_TRANSCRIBE = True

        print("\n=== тишина не доходит до модели ===")
        ai.transcribe = transcribe_ok        # если бы дошло — вернулось бы «керамика»
        asked.clear()
        bot._ai_calls.clear()
        m = FakeMessage(FakeUser(7))
        await bot.handle_voice(m, FakeState(), FakeBot(SILENCE))
        ok &= check("на тишину — «не разобрал»", m.edits and "Не разобрал" in m.edits[-1])
        ok &= check("модель не звали, лимит не тратили", not asked and not bot._ai_calls.get(7))

        print("\n=== кружок расшифровывается как голосовое ===")
        asked.clear()
        m = FakeMessage(FakeUser(8), circle=True)
        await bot.handle_voice(m, FakeState(), FakeBot(CIRCLE))
        ok &= check("кружок ушёл по обычному пути", asked == ["сколько стоит керамика на камри"])

        print("\n=== два кружка подряд — два ответа ===")
        bot._media_last.clear()
        config.VOICE_TRANSCRIBE = False
        replies = 0
        for _ in range(2):
            m = FakeMessage(FakeUser(9), circle=True)
            await bot.handle_voice(m, FakeState(), FakeBot(CIRCLE))
            replies += len(m.sent)
            await asyncio.sleep(config.MEDIA_QUIET_SINGLE_SEC + 0.2)
        ok &= check("на второй кружок тоже ответили", replies == 2)
        config.VOICE_TRANSCRIBE = True

        print("\n=== декодер: что считается речью ===")
        for name, want in [("silence", False), ("noise", False), ("loudnoise", False),
                           ("quiet_speech", True), ("speech_noisy", True), ("sample", True)]:
            a = voice.decode(open(os.path.join("voice", name + ".ogg"), "rb").read())
            ok &= check(f"{name}: {'речь' if want else 'тихо'}", a is not None and a.has_speech == want)
    finally:
        bot.handle_question, ai.transcribe = real_q, real_t

    # --- живой прогон -----------------------------------------------------
    sample = os.path.join("voice", "sample.ogg")
    if config.AI_API_KEY and os.path.exists(sample):
        print("\n=== живая расшифровка через AnyModel ===")
        a = voice.decode(open(sample, "rb").read())
        text = await ai.transcribe(a.wav(), "wav")
        print("   услышали:", text)
        low = text.lower()
        ok &= check("узнали керамику и камри", "керамик" in low and ("камри" in low or "camry" in low))
        await ai.close()
    else:
        print("\n(живой прогон пропущен: нет AI_API_KEY или voice/sample.ogg)")

    print("\nИТОГ:", "всё зелёное" if ok else "есть падения")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
