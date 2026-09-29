"""Подготовка голосового к расшифровке: декодирование и проверка, что в
записи вообще есть речь.

Зачем это нужно. Модель, которой мы отдаём аудио, на тишине не молчит —
она выдумывает: на три секунды пустоты отвечает «Коллеги, добрый день»,
на шум — «раз два». Никакой промпт это не лечит, проверено. Поэтому
громкость меряем сами, до отправки: тихая запись к модели не идёт.

Декодирует библиотека av (ffmpeg внутри). Если её нет, всё продолжает
работать, но без проверки на тишину — с предупреждением в логе.
"""
from __future__ import annotations

import io
import logging
import math
import wave
from dataclasses import dataclass

log = logging.getLogger("voice")

try:
    import av  # type: ignore
    AVAILABLE = True
except Exception:  # noqa: BLE001 — любая ошибка импорта: библиотеки нет
    av = None
    AVAILABLE = False

RATE = 16000        # частота для модели: речи хватает, а файл втрое меньше
FRAME_SEC = 0.05    # окно замера громкости

# Пороги подобраны на записях с телефона: речь даёт -25…-10 дБ, комнатный
# шум с зажатой кнопкой — тише -40 дБ.
LOUD_DB = -38.0     # громче — считаем «звук есть»
MIN_LOUD_SEC = 0.35 # столько «звука» нужно, чтобы поверить, что говорили
MIN_RANGE_DB = 10.0 # речь неровная; ровный гул на -30 дБ — это не речь


@dataclass
class Audio:
    pcm: bytes          # 16-бит моно, RATE Гц
    seconds: float
    loud_seconds: float
    range_db: float

    @property
    def has_speech(self) -> bool:
        return self.loud_seconds >= MIN_LOUD_SEC and self.range_db >= MIN_RANGE_DB

    def wav(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(self.pcm)
        return buf.getvalue()


def _db(rms: float) -> float:
    return 20 * math.log10(rms / 32768.0) if rms > 0 else -120.0


def decode(data: bytes) -> Audio | None:
    """Декодировать ogg/opus, mp4 и прочее в PCM и померить громкость.

    None — библиотеки нет или файл не читается.
    """
    if not AVAILABLE:
        return None
    try:
        container = av.open(io.BytesIO(data))
        stream = next(s for s in container.streams if s.type == "audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        chunks: list[bytes] = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                # буфер плоскости выровнен с запасом — хвост за samples*2
                # это мусор, а не звук
                chunks.append(bytes(out.planes[0])[: out.samples * 2])
        container.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось декодировать аудио: %s", exc)
        return None

    pcm = b"".join(chunks)
    n = len(pcm) // 2
    if n == 0:
        return Audio(pcm, 0.0, 0.0, 0.0)

    # громкость по окнам
    import array
    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])
    win = int(RATE * FRAME_SEC)
    levels: list[float] = []
    for i in range(0, n - win + 1, win):
        acc = 0
        for s in samples[i:i + win]:
            acc += s * s
        levels.append(_db(math.sqrt(acc / win)))

    loud = sum(1 for d in levels if d > LOUD_DB) * FRAME_SEC
    if levels:
        levels_sorted = sorted(levels)
        floor = levels_sorted[len(levels_sorted) // 5]        # 20-й перцентиль
        peak = levels_sorted[int(len(levels_sorted) * 0.95)]  # 95-й
        rng = max(0.0, peak - floor)
    else:
        rng = 0.0
    return Audio(pcm, n / RATE, loud, rng)
