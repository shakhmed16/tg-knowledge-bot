# -*- coding: utf-8 -*-
"""Прогон записи на осмотр: от вопроса про цену до карточки владельцу.

    python test_booking.py

Telegram не нужен — шаги диалога вызываются напрямую с подставными
объектами сообщения и состояния. Проверяем то, что ломается на практике:
телефон с опечаткой, отмену посреди диалога, и что владелец получил
карточку с контактом.
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
config.LEAD_CHAT_ID = ""
config.FALLBACK_CONTACT = "+7 926 107-21-20"

import db

config.DB_PATH = os.path.join(tempfile.mkdtemp(), "leads.db")
db.init_db()

import bot


class FakeUser:
    def __init__(self, uid=777, username="klient"):
        self.id = uid
        self.username = username


class FakeContact:
    def __init__(self, phone):
        self.phone_number = phone


class FakeMessage:
    """Минимум, который нужен хендлерам: текст, автор и .answer()."""

    def __init__(self, text=None, contact=None, user=None):
        self.text = text
        self.contact = contact
        self.from_user = user or FakeUser()
        self.sent: list[str] = []

    async def answer(self, text, **kw):
        self.sent.append(text)
        return self


class FakeState:
    def __init__(self):
        self.state = None
        self.data: dict = {}

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state = None
        self.data = {}


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def check(label, cond):
    print(f"{'OK  ' if cond else 'FAIL'} {label}")
    return cond


async def main() -> int:
    ok = True
    user = FakeUser()

    # --- полный путь -------------------------------------------------------
    print("=== клиент записывается ===")
    state = FakeState()
    fake_bot = FakeBot()

    m = FakeMessage(user=user)
    await bot.start_booking(m, state, "у меня камри, сколько керамика")
    ok &= check("спросили имя", "Как вас зовут" in m.sent[-1])
    ok &= check("состояние = имя", state.state == bot.Booking.name)

    m = FakeMessage("Дмитрий", user=user)
    await bot.booking_name(m, state)
    ok &= check("спросили телефон", "номер телефона" in m.sent[-1])

    # опечатка вместо номера — диалог не должен ехать дальше
    m = FakeMessage("не помню", user=user)
    await bot.booking_phone_text(m, state)
    ok &= check("кривой номер отклонён", "Не похоже на номер" in m.sent[-1])
    ok &= check("остались на шаге телефона", state.state == bot.Booking.phone)

    # номер через кнопку Telegram
    m = FakeMessage(contact=FakeContact("+79261072120"), user=user)
    await bot.booking_phone_contact(m, state)
    ok &= check("спросили машину", "машина" in m.sent[-1].lower())

    m = FakeMessage("Toyota Camry 2019", user=user)
    await bot.booking_car(m, state)
    ok &= check("спросили время", "удобно приехать" in m.sent[-1])

    m = FakeMessage("завтра после 18", user=user)
    await bot.booking_when(m, state, fake_bot)
    ok &= check("клиент получил подтверждение", "Записал" in m.sent[-1])
    ok &= check("состояние очищено", state.state is None)

    leads = db.list_leads(5)
    ok &= check("лид сохранён в базу", len(leads) == 1)
    if leads:
        lead = leads[0]
        ok &= check("имя записано", lead["name"] == "Дмитрий")
        ok &= check("телефон записан", lead["phone"] == "+79261072120")
        ok &= check("машина записана", lead["car"] == "Toyota Camry 2019")
        ok &= check("время записано", lead["when_text"] == "завтра после 18")
        ok &= check(
            "сохранён вопрос, с которого начал",
            "керамика" in (lead["service"] or ""),
        )

    ok &= check("владельцу ушло одно сообщение", len(fake_bot.sent) == 1)
    if fake_bot.sent:
        chat_id, card = fake_bot.sent[0]
        ok &= check("ушло админу", chat_id == 555)
        ok &= check("в карточке телефон", "+79261072120" in card)
        ok &= check("в карточке машина", "Camry" in card)
        ok &= check("в карточке ссылка на клиента", "tg://user?id=777" in card)
        print("\n--- карточка владельцу ---")
        print(card)
        print("--------------------------\n")

    # --- отмена посреди диалога -------------------------------------------
    print("=== клиент передумал ===")
    state2 = FakeState()
    m = FakeMessage(user=user)
    await bot.start_booking(m, state2)
    m = FakeMessage("Иван", user=user)
    await bot.booking_name(m, state2)
    m = FakeMessage("/cancel", user=user)
    await bot.cmd_cancel(m, state2)
    ok &= check("отмена сработала", "Отменил" in m.sent[-1])
    ok &= check("состояние очищено", state2.state is None)
    ok &= check("лишний лид не создан", len(db.list_leads(5)) == 1)

    # --- распознавание намерения словами ----------------------------------
    print("\n=== просьба записаться словами ===")
    for text in ["хочу записаться", "запишите меня на завтра", "можно подъехать в субботу"]:
        ok &= check(f"«{text}»", bot.wants_booking(text))
    for text in ["сколько стоит керамика", "где вы находитесь"]:
        ok &= check(f"«{text}» — не запись", not bot.wants_booking(text))

    # --- лиды некому слать -------------------------------------------------
    print("\n=== некому слать (защита от потери лида) ===")
    config.ADMIN_IDS = set()
    config.LEAD_CHAT_ID = ""
    ok &= check("список получателей пуст", bot.lead_targets() == [])
    config.ADMIN_IDS = {555}

    print("\n" + "=" * 60)
    print("Все прошли." if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
