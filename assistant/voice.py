"""Voice messages: Uzbek text-to-speech and Telegram voice-note files (OGG/Opus).

Speech comes from Microsoft's online neural voices (``edge-tts``; e.g. uz-UZ-SardorNeural,
uz-UZ-MadinaNeural). Only the text being spoken is sent to that service. The result is
converted to OGG/Opus with ``soundfile`` and cached under ``data/``, so a fixed text
(the greeting) is synthesised once, not for every message.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("assistant.voice")

DEFAULT_VOICE = "uz-UZ-SardorNeural"

_CYR_TO_LAT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j",
        "з": "z", "и": "i", "й": "y", "к": "k", "қ": "q", "л": "l", "м": "m", "н": "n", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ў": "o", "ф": "f", "х": "x", "ҳ": "h",
        "ц": "s", "ч": "ch", "ш": "sh", "ъ": "", "ь": "", "ы": "i", "э": "e", "ю": "yu", "я": "ya",
    }
)
# A greeting phrase at the start (Uzbek, Russian after transliteration, English) ...
_GREETING_START = re.compile(
    r"^(?:"
    r"a?s+[ae]l+[aeo]m+u?(?:\s*a?l[ae]y?kum)?"  # assalomu alaykum, salom, salom aleykum ...
    r"|s[ae]l+[aeo]m+lar"  # salomlar
    r"|slm"
    r"|[xh]a(?:y|yi)rli\s+(?:kun|tong|kech|oqshom)"  # xayrli kun / tong / kech
    r"|hello|hi|hey|good\s+(?:morning|afternoon|evening|day)"
    r"|privet(?:ik)?|zdra[a-z]*|dobr[a-z]*\s+(?:den|utro|vecher)"  # привет, здравствуйте, добрый день
    r")(?=\s|$)"
)
# ... optionally followed only by small talk / an address, nothing else.
_GREETING_TAIL = frozenset(
    "aka akajon uka opa opajon singil brat hammaga abbosbek abbos va ham sizga sizlarga "
    "yaxshimisiz yaxshimisan yaxshimi qalaysiz qalesiz qalaysan qalesan qandaysiz qandaysan ishlar "
    "kak dela vy how are you there everyone".split()
)
MAX_GREETING_TAIL = 4


def normalize_greeting(text: str) -> str:
    text = text.lower().translate(_CYR_TO_LAT)
    text = re.sub(r"[ʻʼ'`’‘-]", "", text)
    text = re.sub(r"[^a-z\s]", " ", text)
    return " ".join(text.split())


def is_greeting_only(text: Optional[str]) -> bool:
    """True for a greeting with at most some small talk ("Salom!", "Assalomu alaykum, yaxshimisiz?",
    "Xayrli kun", "Привет, как дела?"), but not when it carries a real question or request."""
    if not text or len(text) > 80:
        return False
    normalized = normalize_greeting(text)
    match = _GREETING_START.match(normalized)
    if not match:
        return False
    tail = normalized[match.end():].split()
    return len(tail) <= MAX_GREETING_TAIL and all(word in _GREETING_TAIL for word in tail)


def cached_voice_path(data_dir: Path, text: str, voice: str) -> Path:
    digest = hashlib.sha1(f"{voice}\n{text}".encode("utf-8")).hexdigest()[:12]
    return Path(data_dir) / f"voice-{digest}.ogg"


TTS_ATTEMPTS = 3
TTS_RETRY_PAUSE = 5.0


def tts_timeout(text: str) -> float:
    """The online voice service sometimes stalls mid-stream; a normal run takes ~10-20 s."""
    return max(60.0, len(text) / 15.0)


async def save_speech(text: str, voice: str, mp3: Path, *, rate: str = "+0%", attempts: int = TTS_ATTEMPTS,
                      pause: float = TTS_RETRY_PAUSE) -> None:
    """edge-tts to an MP3 file, with a time limit and retries (a stalled stream is cut off)."""
    import edge_tts

    timeout = tts_timeout(text)
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.wait_for(edge_tts.Communicate(text, voice, rate=rate).save(str(mp3)), timeout)
            return
        except Exception as exc:  # noqa: BLE001 - network hiccups: try again
            if attempt == attempts:
                raise
            reason = "no answer in time" if isinstance(exc, asyncio.TimeoutError) else f"{type(exc).__name__}: {exc}"
            log.warning("Text-to-speech attempt %s failed (%s); trying again", attempt, reason)
            await asyncio.sleep(pause)


async def synthesize(text: str, voice: str, out: Path, *, rate: str = "+0%") -> float:
    """Speak ``text`` into ``out`` (OGG/Opus voice note). Returns the duration in seconds.

    ``rate`` is the edge-tts speed change, e.g. "-5%" for a slightly slower, softer voice.
    """
    import soundfile as sf

    out = Path(out)
    mp3 = out.with_suffix(".mp3")
    try:
        await save_speech(text, voice, mp3, rate=rate)
        data, rate = sf.read(str(mp3))
        tmp = out.with_suffix(".tmp.ogg")
        sf.write(str(tmp), data, rate, format="OGG", subtype="OPUS")
        tmp.replace(out)
    finally:
        try:
            mp3.unlink()
        except OSError:
            pass
    return voice_duration(out)


def voice_duration(path: Path) -> float:
    import soundfile as sf

    return float(sf.info(str(path)).duration)


async def ensure_voice(data_dir: Path, text: str, voice: str) -> Tuple[Path, float]:
    """Cached voice note for a fixed text; synthesised on first use."""
    path = cached_voice_path(data_dir, text, voice)
    if path.exists():
        return path, voice_duration(path)
    duration = await synthesize(text, voice, path)
    log.info("Voice note created: %s (%.1f s, %s)", path.name, duration, voice)
    return path, duration
