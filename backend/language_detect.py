"""
Appello — Language Detection & Cross-script Transliteration
Detects user language from transcript text using script analysis + romanisation heuristics.
Supports: Hindi, Tamil, Telugu, Kannada, Malayalam, Bengali, English.
"""

import logging
import re
from typing import Optional, Dict

logger = logging.getLogger("appello")

# ─── Script Unicode Ranges → BCP-47 language tag ────────────────────────
_SCRIPT_RANGES = [
    ("hi-IN", (0x0900, 0x097F)),  # Devanagari
    ("ta-IN", (0x0B80, 0x0BFF)),  # Tamil
    ("te-IN", (0x0C00, 0x0C7F)),  # Telugu
    ("kn-IN", (0x0C80, 0x0CFF)),  # Kannada
    ("ml-IN", (0x0D00, 0x0D7F)),  # Malayalam
    ("bn-IN", (0x0980, 0x09FF)),  # Bengali
]


# ─── Cross-script transliteration to Latin ───────────────────────────────
# Whisper sometimes transcribes Tamil audio into Devanagari or Gurmukhi when its
# language autodetect is biased by prior conversation context. To recover, we
# romanise the transcript and look for distinctively-Tamil keywords that never
# occur in Hindi/Punjabi.

_DEVA_TO_LATIN = {
    # Independent vowels
    'अ': 'a', 'आ': 'aa', 'इ': 'i', 'ई': 'ii', 'उ': 'u', 'ऊ': 'uu',
    'ऋ': 'ri', 'ए': 'e', 'ऐ': 'ai', 'ओ': 'o', 'औ': 'au',
    # Vowel signs (matras)
    'ा': 'aa', 'ि': 'i', 'ी': 'ii', 'ु': 'u', 'ू': 'uu',
    'ृ': 'ri', 'े': 'e', 'ै': 'ai', 'ो': 'o', 'ौ': 'au',
    'ं': 'n', 'ः': 'h', 'ँ': 'n',
    # Consonants
    'क': 'k', 'ख': 'kh', 'ग': 'g', 'घ': 'gh', 'ङ': 'ng',
    'च': 'ch', 'छ': 'chh', 'ज': 'j', 'झ': 'jh', 'ञ': 'n',
    'ट': 't', 'ठ': 'th', 'ड': 'd', 'ढ': 'dh', 'ण': 'n',
    'त': 't', 'थ': 'th', 'द': 'd', 'ध': 'dh', 'न': 'n',
    'प': 'p', 'फ': 'ph', 'ब': 'b', 'भ': 'bh', 'म': 'm',
    'य': 'y', 'र': 'r', 'ल': 'l', 'व': 'v',
    'श': 'sh', 'ष': 'sh', 'स': 's', 'ह': 'h',
    '्': '', '़': '',  # halant / nukta
}

_GURMUKHI_TO_LATIN = {
    'ਅ': 'a', 'ਆ': 'aa', 'ਇ': 'i', 'ਈ': 'ii', 'ਉ': 'u', 'ਊ': 'uu',
    'ਏ': 'e', 'ਐ': 'ai', 'ਓ': 'o', 'ਔ': 'au',
    'ਾ': 'aa', 'ਿ': 'i', 'ੀ': 'ii', 'ੁ': 'u', 'ੂ': 'uu',
    'ੇ': 'e', 'ੈ': 'ai', 'ੋ': 'o', 'ੌ': 'au',
    'ੰ': 'n', 'ਂ': 'n', 'ੱ': '',
    'ਕ': 'k', 'ਖ': 'kh', 'ਗ': 'g', 'ਘ': 'gh', 'ਙ': 'ng',
    'ਚ': 'ch', 'ਛ': 'chh', 'ਜ': 'j', 'ਝ': 'jh', 'ਞ': 'n',
    'ਟ': 't', 'ਠ': 'th', 'ਡ': 'd', 'ਢ': 'dh', 'ਣ': 'n',
    'ਤ': 't', 'ਥ': 'th', 'ਦ': 'd', 'ਧ': 'dh', 'ਨ': 'n',
    'ਪ': 'p', 'ਫ': 'ph', 'ਬ': 'b', 'ਭ': 'bh', 'ਮ': 'm',
    'ਯ': 'y', 'ਰ': 'r', 'ਲ': 'l', 'ਵ': 'v',
    'ਸ਼': 'sh', 'ਸ': 's', 'ਹ': 'h',
    '੍': '',
}

_GUJARATI_TO_LATIN = {
    'અ': 'a', 'આ': 'aa', 'ઇ': 'i', 'ઈ': 'ii', 'ઉ': 'u', 'ઊ': 'uu',
    'ઋ': 'ri', 'એ': 'e', 'ઐ': 'ai', 'ઓ': 'o', 'ઔ': 'au',
    'ા': 'aa', 'િ': 'i', 'ી': 'ii', 'ુ': 'u', 'ૂ': 'uu',
    'ૃ': 'ri', 'ે': 'e', 'ૈ': 'ai', 'ો': 'o', 'ૌ': 'au',
    'ં': 'n', 'ઃ': 'h', 'ઁ': 'n',
    'ક': 'k', 'ખ': 'kh', 'ગ': 'g', 'ઘ': 'gh', 'ઙ': 'ng',
    'ચ': 'ch', 'છ': 'chh', 'જ': 'j', 'ઝ': 'jh', 'ઞ': 'n',
    'ટ': 't', 'ઠ': 'th', 'ડ': 'd', 'ઢ': 'dh', 'ણ': 'n',
    'ત': 't', 'થ': 'th', 'દ': 'd', 'ધ': 'dh', 'ન': 'n',
    'પ': 'p', 'ફ': 'ph', 'બ': 'b', 'ભ': 'bh', 'મ': 'm',
    'ય': 'y', 'ર': 'r', 'લ': 'l', 'વ': 'v',
    'શ': 'sh', 'ષ': 'sh', 'સ': 's', 'હ': 'h',
    '્': '', '઼': '',  # halant / nukta
}

_BENGALI_TO_LATIN = {
    'অ': 'a', 'আ': 'aa', 'ই': 'i', 'ঈ': 'ii', 'উ': 'u', 'ঊ': 'uu',
    'ঋ': 'ri', 'এ': 'e', 'ঐ': 'ai', 'ও': 'o', 'ঔ': 'au',
    'া': 'aa', 'ি': 'i', 'ী': 'ii', 'ু': 'u', 'ূ': 'uu',
    'ৃ': 'ri', 'ে': 'e', 'ৈ': 'ai', 'ো': 'o', 'ৌ': 'au',
    'ং': 'n', 'ঃ': 'h', 'ঁ': 'n',
    'ক': 'k', 'খ': 'kh', 'গ': 'g', 'ঘ': 'gh', 'ঙ': 'ng',
    'চ': 'ch', 'ছ': 'chh', 'জ': 'j', 'ঝ': 'jh', 'ঞ': 'n',
    'ট': 't', 'ঠ': 'th', 'ড': 'd', 'ঢ': 'dh', 'ণ': 'n',
    'ত': 't', 'থ': 'th', 'দ': 'd', 'ধ': 'dh', 'ন': 'n',
    'প': 'p', 'ফ': 'ph', 'ব': 'b', 'ভ': 'bh', 'ম': 'm',
    'য': 'y', 'র': 'r', 'ল': 'l', 'ৱ': 'v',
    'শ': 'sh', 'ষ': 'sh', 'স': 's', 'হ': 'h',
    '্': '', '়': '',
}


def _romanise(text: str) -> str:
    """Best-effort romanisation across Devanagari / Gurmukhi / Gujarati / Bengali / Latin."""
    out = []
    for ch in text:
        if ch in _DEVA_TO_LATIN:
            out.append(_DEVA_TO_LATIN[ch])
        elif ch in _GURMUKHI_TO_LATIN:
            out.append(_GURMUKHI_TO_LATIN[ch])
        elif ch in _GUJARATI_TO_LATIN:
            out.append(_GUJARATI_TO_LATIN[ch])
        elif ch in _BENGALI_TO_LATIN:
            out.append(_BENGALI_TO_LATIN[ch])
        else:
            out.append(ch)
    return ''.join(out).lower()


# Distinctively-Tamil substrings (after romanisation). These do NOT occur in
# Hindi or Punjabi, so finding them means the audio was Tamil even if Whisper
# wrote it in the wrong script.
_TAMIL_MARKERS = (
    "theri", "theriy", "theriyum", "theriyuma",
    "thiri", "thiriy", "thiriyum", "thiriyuma",
    # Whisper often mis-renders Tamil aspirate th/dh sounds as kh in Devanagari.
    "kheri", "kheriy", "kheriyum",
    # The literal word "tamil" — survives across most scripts when the user
    # explicitly names the language ("Tamil theriyuma?" → "tmil theri ma?").
    "tamil", "tmil ", " tmil",
    "epdi", "epadi",
    "enakku", "enaku",
    "puriyuth", "puriy",
    "irukk", "irukku",
    "vendam", "venda",
    "vanakkam",
    "ungaluk", "neenga", "naanga",
    "panniduv", "pannitt", "pannitten",
    "sollung", "sollu",
)

# Distinctively-Hindi substrings (after romanisation). Used as a counter-check
# so we don't false-flag Hindi sentences that happen to contain a Tamil-looking
# substring.
_HINDI_MARKERS = (
    "mujhe", "mera", "meri", "tumhar", "aapka", "aapko", "kaise", "kaisa",
    "kyunki", "kyon", "kyu ", "kyu?", "haan", "nahi", "thik", "theek",
    "duungaa", "kar duun", "karunga", "karenge", "rahaa", "raha hu",
    "tumko", "tumhe", "humko", "humein",
)


def detect_language(text: str) -> str:
    """
    Detect script using *character-ratio* (not "any single match") so that a
    mis-transcribed stray glyph cannot flip the detected language.

    Whisper sometimes leaks a single Tamil character into an otherwise-Hindi
    transcript (and vice-versa) when the model's audio context is biased.
    The previous implementation flipped the result on the first stray char,
    which caused us to inject the wrong language directive and made the
    assistant reply in the wrong script.

    Algorithm:
      1. Count characters per script.
      2. If any regional script has >= 30% of total letter chars (or simply
         dominates the other regional scripts by 2x), use it.
      3. Romanise the text and check for distinctively-Tamil keywords — this
         catches the common failure mode where Whisper writes Tamil audio in
         Devanagari or Gurmukhi script because it has drifted to a Hindi-biased
         language autodetect.
      4. Otherwise fall back to transliterated-word heuristics on Latin text.
      5. Default to en-IN.
    """
    if not text:
        return "en-IN"

    # Romanise once and check for distinctively-Tamil markers. These never
    # occur in real Hindi/Punjabi, so finding one means the audio was Tamil
    # regardless of which script Whisper chose.
    romanised = _romanise(text)
    has_tamil_marker = any(m in romanised for m in _TAMIL_MARKERS)
    has_hindi_marker = any(m in romanised for m in _HINDI_MARKERS)

    # Count letters (ignore digits/punctuation/whitespace)
    counts: Dict[str, int] = {tag: 0 for tag, _ in _SCRIPT_RANGES}
    total_letters = 0
    for ch in text:
        cp = ord(ch)
        if not ch.isalpha():
            continue
        total_letters += 1
        for tag, (lo, hi) in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[tag] += 1
                break

    script_lang: Optional[str] = None
    if total_letters > 0:
        best_tag, best_count = max(counts.items(), key=lambda kv: kv[1])
        if best_count > 0:
            ratio = best_count / total_letters
            second = max((v for k, v in counts.items() if k != best_tag), default=0)
            if ratio >= 0.30 or best_count >= 2 * max(second, 1):
                script_lang = best_tag

    # OVERRIDE: distinctively Tamil markers beat script detection. This is the
    # whole point of romanisation — recover from Whisper writing Tamil audio
    # in Devanagari / Gurmukhi script.
    if has_tamil_marker and not has_hindi_marker:
        logger.debug(f"[language] Tamil marker found in romanised='{romanised[:80]}', overriding script={script_lang} -> ta-IN")
        return "ta-IN"

    if script_lang is not None:
        # Gurmukhi (Punjabi) is almost certainly a Whisper mis-script for Hindi
        # or Tamil. We don't support Punjabi in this app, so map it sensibly.
        if script_lang not in ("hi-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "bn-IN", "en-IN"):
            return "hi-IN"
        return script_lang

    # Transliterated fallbacks on pure Latin text
    text_lower = text.lower()
    words = set(re.findall(r'\b\w+\b', text_lower))
    hinglish_words = {"haan", "nahi", "theek", "hai", "mujhe", "mera", "main", "kya", "accha", "thoda", "dard", "kar", "dunga", "matlab", "ho", "gaya", "yaar", "bhai", "ji", "kal", "aap", "aapka", "kaise"}
    tanglish_words = {"aama", "illai", "sari", "naan", "en", "enakku", "puriyuthu", "sollunga", "ippo", "aprom", "vanakkam", "romba", "nandri", "neenga", "ungaluku", "panniduven", "theriyuma", "theri", "epdi", "epadi"}
    hi_hits = len(words & hinglish_words)
    ta_hits = len(words & tanglish_words)
    if ta_hits > hi_hits and ta_hits >= 1:
        return "ta-IN"
    if hi_hits > ta_hits and hi_hits >= 1:
        return "hi-IN"

    return "en-IN"


def is_regional(lang: str) -> bool:
    return lang in ("hi-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "bn-IN")
