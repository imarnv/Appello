"""
Appello — Audio Utilities
TTS synthesis, STT transcription, PCM resampling, WAV header, and sentence splitting.
All audio constants and processing functions used across the voice pipeline.
"""

import base64
import logging
import os
import re
import struct
import time
from typing import Optional

import aiohttp
import numpy as np

from language_detect import detect_language

logger = logging.getLogger("appello")

# ─── Audio Constants ─────────────────────────────────────────────────────
SAMPLE_RATE = 24000
CHUNK_DURATION_MS = 100
CHUNK_SIZE_BYTES = int(SAMPLE_RATE * 2 * CHUNK_DURATION_MS / 1000)  # 4800 bytes per 100ms

# ─── Sarvam TTS Config ──────────────────────────────────────────────────
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
SARVAM_SPEAKER = os.getenv("SARVAM_TTS_SPEAKER", "kabir")
SARVAM_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")


# ─── PCM Resampling ─────────────────────────────────────────────────────

def resample_pcm16(pcm_bytes: bytes, in_rate: int, out_rate: int) -> bytes:
    """Linear-interp resampler for 16-bit signed PCM (mono).
    Handles arbitrary rate ratios. Returns input unchanged when rates match.
    """
    if not pcm_bytes or in_rate == out_rate:
        return pcm_bytes
    arr = np.frombuffer(pcm_bytes, dtype=np.int16)
    if arr.size == 0:
        return pcm_bytes
    n_out = max(1, int(round(arr.size * out_rate / in_rate)))
    x_in = np.arange(arr.size, dtype=np.float32)
    x_out = np.linspace(0.0, arr.size - 1, n_out, dtype=np.float32)
    out = np.interp(x_out, x_in, arr.astype(np.float32))
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


# Backward-compat shims (kept so other code paths still import cleanly).
def upsample_8k_to_24k(pcm_bytes: bytes) -> bytes:
    return resample_pcm16(pcm_bytes, 8000, 24000)


def downsample_24k_to_8k(pcm_bytes: bytes) -> bytes:
    return resample_pcm16(pcm_bytes, 24000, 8000)


# ─── WAV Header ──────────────────────────────────────────────────────────

def pcm_to_wav(pcm_data: bytes, sample_rate: int = 24000, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    """Prepend a 44-byte WAV header to raw PCM data."""
    num_samples = len(pcm_data) // (bits_per_sample // 8)
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)

    header = bytearray(44)
    struct.pack_into('<4sI4s4sIHHIIHH4sI', header, 0,
                      b'RIFF',
                      36 + len(pcm_data),
                      b'WAVE',
                      b'fmt ',
                      16,  # Subchunk1Size
                      1,   # AudioFormat (1 = PCM)
                      channels,
                      sample_rate,
                      byte_rate,
                      block_align,
                      bits_per_sample,
                      b'data',
                      len(pcm_data))
    return bytes(header) + pcm_data


# ─── Sarvam TTS Synthesis ────────────────────────────────────────────────

async def synthesize_tts(
    text: str,
    speaker: str,
    http_session: aiohttp.ClientSession,
    tracker=None,
    pace: float = 1.03,
) -> Optional[bytes]:
    """Synthesize speech using Sarvam Bulbul v3 TTS. Returns raw PCM16 audio bytes."""
    t0 = time.monotonic()
    if tracker:
        tracker.mark("tts_request_sent")
    lang = detect_language(text)

    try:
        async with http_session.post(
            "https://api.sarvam.ai/text-to-speech/stream",
            headers={
                "Content-Type": "application/json",
                "api-subscription-key": SARVAM_API_KEY,
            },
            json={
                "text": text,
                "target_language_code": lang,
                "speaker": speaker,
                "model": SARVAM_MODEL,
                "output_audio_codec": "linear16",
                "speech_sample_rate": SAMPLE_RATE,
                "pace": pace,
                "enable_preprocessing": True,
            },
        ) as resp:
            if resp.status != 200:
                logger.error(f"Sarvam TTS error: HTTP {resp.status}")
                return None
            audio = await resp.read()
            t1 = time.monotonic()
            elapsed = (t1 - t0) * 1000
            if tracker:
                tracker.mark("tts_audio_received")
                tracker.mark_tts(text, t0, t1)
            logger.info(f"[tts] Synthesized in {elapsed:.0f}ms for: \"{text[:40]}...\"")
            return audio
    except Exception as e:
        logger.error(f"[tts] Synthesis failed: {e}")
        return None


async def synthesize_tts_streaming(
    text: str,
    speaker: str,
    http_session: aiohttp.ClientSession,
    tracker=None,
    pace: float = 1.03,
):
    """
    Streaming TTS: yields PCM16 audio chunks as they arrive from Sarvam.
    This lets us start playing audio to the user ~300ms into the TTS request
    instead of waiting for the full ~1000ms response.
    """
    t0 = time.monotonic()
    if tracker:
        tracker.mark("tts_request_sent")
    lang = detect_language(text)
    first_chunk = True
    total_bytes = 0

    try:
        async with http_session.post(
            "https://api.sarvam.ai/text-to-speech/stream",
            headers={
                "Content-Type": "application/json",
                "api-subscription-key": SARVAM_API_KEY,
            },
            json={
                "text": text,
                "target_language_code": lang,
                "speaker": speaker,
                "model": SARVAM_MODEL,
                "output_audio_codec": "linear16",
                "speech_sample_rate": SAMPLE_RATE,
                "pace": pace,
                "enable_preprocessing": True,
            },
        ) as resp:
            if resp.status != 200:
                logger.error(f"Sarvam TTS streaming error: HTTP {resp.status}")
                return

            # Read response in chunks as they arrive from Sarvam
            async for chunk in resp.content.iter_chunked(CHUNK_SIZE_BYTES):
                if first_chunk:
                    ttfb = (time.monotonic() - t0) * 1000
                    logger.info(f"[tts-stream] First chunk in {ttfb:.0f}ms for: \"{text[:40]}...\"")
                    if tracker:
                        tracker.mark("tts_first_chunk")
                    first_chunk = False
                total_bytes += len(chunk)
                yield chunk

            t1 = time.monotonic()
            elapsed = (t1 - t0) * 1000
            if tracker:
                tracker.mark("tts_audio_received")
                tracker.mark_tts(text, t0, t1)
            logger.info(f"[tts-stream] Complete in {elapsed:.0f}ms ({total_bytes} bytes) for: \"{text[:40]}...\"")
    except Exception as e:
        logger.error(f"[tts-stream] Streaming synthesis failed: {e}")


# ─── Sarvam STT Transcription ───────────────────────────────────────────

async def transcribe_audio_sarvam(pcm_data: bytes, http_session: aiohttp.ClientSession, sample_rate: int = 24000) -> Optional[str]:
    """Transcribe user speech asynchronously using Sarvam's Speech-to-Text API."""
    if not pcm_data or len(pcm_data) < 3200:  # less than 100ms is too short/empty
        return None
    try:
        wav_data = pcm_to_wav(pcm_data, sample_rate=sample_rate)
        data = aiohttp.FormData()
        data.add_field('file', wav_data, filename='audio.wav', content_type='audio/wav')
        data.add_field('model', 'saaras:v3')
        data.add_field('mode', 'transcribe')

        async with http_session.post(
            "https://api.sarvam.ai/speech-to-text",
            headers={"api-subscription-key": SARVAM_API_KEY},
            data=data
        ) as resp:
            if resp.status == 200:
                resp_json = await resp.json()
                transcript = resp_json.get("transcript", "").strip()
                return transcript
            else:
                resp_text = await resp.text()
                logger.error(f"[sarvam-stt] Error response {resp.status}: {resp_text}")
                return None
    except Exception as e:
        logger.error(f"[sarvam-stt] Transcription failed: {e}")
        return None


# ─── Text Chunking (Sentence Splitter) ───────────────────────────────────

def extract_sentences(buffer: str, aggressive_first_flush: bool = False) -> tuple[list[str], str]:
    """
    Extract complete sentences from a text buffer.
    Returns (list_of_sentences, remaining_buffer).
    Splits on sentence terminators (. ; ! ? ।) but NOT commas,
    and protects decimals (8500.00) and abbreviations (Mr., Ms.) from triggering splits.

    When `aggressive_first_flush=True` (caller indicates this is the very first
    chunk of a response), we additionally flush at the FIRST comma if there are
    ≥3 words before it — this lets us start TTS on "Sure, thanks for asking"
    while the rest of the sentence is still being generated, saving ~300ms TTFA.
    """
    # 1. Protect decimals in numbers (e.g. 8500.00 -> 8500_DEC_00)
    protected = re.sub(r'(\d)\.(\d)', r'\1_DEC_\2', buffer)

    # 2. Protect common abbreviations
    abbrevs = ["mr", "ms", "dr", "mrs", "etc", "vs", "co", "ltd", "inc", "approx", "appt", "min", "sec"]
    for abbrev in abbrevs:
        protected = re.compile(rf'\b{abbrev}\.', re.IGNORECASE).sub(f'{abbrev}_DOT_', protected)

    sentences = []
    # Match sentences ending with . ; ! ? । followed by space or end of string
    pattern = re.compile(r"[^.;!?।\n]*[.;!?।\n](?=\s|$)")
    last_end = 0

    for match in pattern.finditer(protected):
        sentence = match.group().strip()
        if len(sentence) >= 2:
            # Restore decimal points and abbreviations
            sentence = sentence.replace('_DEC_', '.').replace('_DOT_', '.')
            sentences.append(sentence)
        last_end = match.end()

    remaining = protected[last_end:] if last_end > 0 else protected
    remaining = remaining.replace('_DEC_', '.').replace('_DOT_', '.')

    # AGGRESSIVE FIRST FLUSH: only at the very start of a response, cut early
    # to start TTS sooner. Two triggers:
    #   (a) at the first comma if ≥2 words precede it and ≥2 follow, OR
    #   (b) once we've accumulated ≥8 words without any punctuation at all.
    # Massively reduces time-to-first-audio.
    if aggressive_first_flush and not sentences:
        m = re.match(r"([^,;\n]+?),\s+(.+)", remaining)
        flushed = False
        if m:
            before = m.group(1).strip()
            after = m.group(2).strip()
            if len(before.split()) >= 2 and len(after.split()) >= 2:
                sentences.append(before)
                remaining = after
                flushed = True
        if not flushed:
            w = remaining.split()
            if len(w) >= 8:
                sentences.append(" ".join(w[:8]))
                remaining = " ".join(w[8:])

    # Word-count fallback: if 18+ words accumulated without punctuation, flush
    words = remaining.split()
    if len(words) >= 18:
        flush_text = " ".join(words[:18])
        sentences.append(flush_text)
        remaining = " ".join(words[18:])

    return sentences, remaining
