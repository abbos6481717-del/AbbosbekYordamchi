"""Very small language hint: Uzbek, Russian or English.

The result is only a hint for Claude; it never decides anything on its own.
"""
from __future__ import annotations

import re
from typing import Optional

APOSTROPHES = "'ʻ’‘`ʼ"
_APOS_CLASS = "[" + re.escape(APOSTROPHES) + "]"

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_UZ_CYRILLIC_LETTERS = set("ўқғҳЎҚҒҲ")

UZ_CYRILLIC_WORDS = {
    "салом", "ассалому", "алейкум", "рахмат", "раҳмат", "яхши", "нима", "қалесан",
    "калесан", "йўқ", "йук", "бугун", "эртага", "керак", "ҳозир", "хозир", "кейин",
    "мен", "сен", "сиз", "ака", "опа", "хоп", "бопти", "зўр", "зор", "қаерда",
    "каерда", "ишлар", "қандай", "кандай", "майли", "ҳа", "хаъ",
}

UZ_LATIN_WORDS = {
    "salom", "assalomu", "alaykum", "rahmat", "raxmat", "yaxshi", "nima", "qalesan",
    "qalaysan", "qalay", "qayerda", "qayerdasan", "bugun", "ertaga", "kerak", "xop",
    "bopti", "zor", "hozir", "keyin", "men", "sen", "siz", "ham", "yoq", "bor",
    "opa", "uka", "nimaga", "qachon", "qanday", "qilyapsan", "ishlar",
    "boshmisan", "mayli", "kechirasiz", "iltimos", "uchrashamiz",
}

UZ_SUFFIXES = (
    "misan", "misiz", "mizmi", "yapsan", "yapman", "yapti", "yapmiz",
    "asizmi", "ganman", "dingmi",
)

# o' / g' followed by more letters. English "o'clock" and possessives such as
# "dog's" (and contractions) are excluded below.
_UZ_APOS_RE = re.compile(r"([og])" + _APOS_CLASS + r"([a-z]*)", re.IGNORECASE)
_ENGLISH_TAILS = {"s", "clock", "t", "ll", "re", "ve", "d", "m", ""}

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _has_uz_apostrophe(text: str) -> bool:
    for match in _UZ_APOS_RE.finditer(text):
        tail = match.group(2).lower()
        if tail in _ENGLISH_TAILS:
            continue
        return True
    return False


def detect_language(text: Optional[str]) -> Optional[str]:
    """Return "uz", "ru", "en" or None (no letters, e.g. emoji or digits)."""
    if not text:
        return None
    cyrillic = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if cyrillic == 0 and latin == 0:
        return None

    lowered = text.lower()
    if cyrillic > latin:
        if any(ch in _UZ_CYRILLIC_LETTERS for ch in text):
            return "uz"
        words = set(_WORD_RE.findall(lowered))
        if words & UZ_CYRILLIC_WORDS:
            return "uz"
        return "ru"

    if _has_uz_apostrophe(lowered):
        return "uz"
    stripped = re.sub(_APOS_CLASS, "", lowered)
    words = _WORD_RE.findall(stripped)
    for word in words:
        if word in UZ_LATIN_WORDS:
            return "uz"
        if len(word) > 5 and word.endswith(UZ_SUFFIXES):
            return "uz"
    return "en"


LANGUAGE_NAMES = {"uz": "Uzbek", "ru": "Russian", "en": "English"}
