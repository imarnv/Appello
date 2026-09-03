"""Adaptive voice helpers for the Gemini Live bridge.

Two small, dependency-light utilities used by test_realtime_gemini.py:

* GenderDetector — pitch (F0) based speaker-gender estimate, computed on the
  same 16kHz PCM we already forward to Gemini. Uses FFT autocorrelation so a
  frame costs tens of microseconds; safe to run inline on the event loop.
* PaceTracker — turns the user's observed speaking rate into a playback-rate
  hint the browser applies to Gemini's audio.

Neither talks to the network and neither holds state across sessions.
"""

import logging
import re
from collections import deque
from typing import Optional

import numpy as np

logger = logging.getLogger("appello-gemini-live")


# ─── Speaker gender via fundamental frequency ────────────────────────────────

class GenderDetector:
    """Estimates speaker gender from the pitch of incoming PCM16 audio.

    Feed it raw 16kHz mono PCM16 as it arrives. It returns a gender string only
    on the frames where its verdict *changes* (including the first verdict),
    and None otherwise — so the caller can treat a non-None return as "act now".

    Typical adult F0: male 85-155 Hz, female 165-255 Hz. The band in between is
    treated as undecidable and simply doesn't vote, which is what keeps this
    from flip-flopping on low-voiced women and high-voiced men.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 32,
        hop_ms: int = 64,
        f0_min: float = 70.0,
        f0_max: float = 320.0,
        male_max: float = 155.0,
        female_min: float = 175.0,
        window: int = 48,
        min_votes: int = 24,
        commit_ratio: float = 0.65,
        switch_ratio: float = 0.80,
        rms_floor: float = 0.012,
        voiced_peak: float = 0.35,
    ):
        self.sample_rate = sample_rate
        self.male_max = male_max
        self.female_min = female_min
        self.min_votes = min_votes
        self.commit_ratio = commit_ratio
        self.switch_ratio = switch_ratio
        self.rms_floor = rms_floor
        self.voiced_peak = voiced_peak

        self._frame = int(sample_rate * frame_ms / 1000)
        # hop > frame on purpose: we deliberately skip audio between analysis
        # frames, which is plenty for a pitch verdict and keeps the cost flat.
        self._hop = int(sample_rate * hop_ms / 1000)
        self._min_lag = max(1, int(sample_rate / f0_max))
        self._max_lag = int(sample_rate / f0_min)
        self._nfft = 1 << int(2 * self._frame - 1).bit_length()
        self._win = np.hanning(self._frame).astype(np.float32)

        self._buf = np.zeros(0, dtype=np.float32)
        self._f0s: deque = deque(maxlen=window)
        self.gender: Optional[str] = None

    def feed(self, pcm16: bytes) -> Optional[str]:
        """Consume a chunk of 16kHz PCM16. Returns "male"/"female" on a change."""
        if not pcm16:
            return None

        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        self._buf = np.concatenate((self._buf, samples)) if self._buf.size else samples

        verdict = None
        while self._buf.size >= self._frame:
            f0 = self._estimate_f0(self._buf[: self._frame])
            self._buf = self._buf[self._hop:] if self._buf.size > self._hop else np.zeros(0, dtype=np.float32)
            if f0 <= 0.0:
                continue
            self._f0s.append(f0)
            decided = self._decide()
            if decided:
                verdict = decided
        return verdict

    def _decide(self) -> Optional[str]:
        if len(self._f0s) < self.min_votes:
            return None

        arr = np.fromiter(self._f0s, dtype=np.float32)
        male = float(np.mean(arr <= self.male_max))
        female = float(np.mean(arr >= self.female_min))

        # A first verdict is cheaper to earn than a switch away from one.
        needed = self.commit_ratio if self.gender is None else self.switch_ratio
        if female >= needed and female > male:
            candidate = "female"
        elif male >= needed and male > female:
            candidate = "male"
        else:
            return None

        if candidate == self.gender:
            return None

        previous = self.gender
        self.gender = candidate
        # Clearing forces a full fresh window before the next flip is possible.
        self._f0s.clear()
        logger.info(
            f"[gender] {previous or 'unknown'} → {candidate} "
            f"(median F0 {float(np.median(arr)):.0f} Hz, male {male:.0%} / female {female:.0%})"
        )
        return candidate

    def _estimate_f0(self, frame: np.ndarray) -> float:
        """Normalised-autocorrelation pitch estimate. Returns 0.0 if unvoiced."""
        frame = frame - float(frame.mean())
        rms = float(np.sqrt(np.mean(frame * frame)))
        if rms < self.rms_floor:
            return 0.0

        spec = np.fft.rfft(frame * self._win, self._nfft)
        acf = np.fft.irfft(spec * np.conj(spec), self._nfft)[: self._max_lag + 1]
        if acf.size <= self._min_lag or acf[0] <= 0:
            return 0.0
        acf = acf / acf[0]

        segment = acf[self._min_lag: self._max_lag + 1]
        if segment.size == 0:
            return 0.0
        idx = int(np.argmax(segment))
        peak = float(segment[idx])
        if peak < self.voiced_peak:
            return 0.0
        lag = self._min_lag + idx

        # Octave guard: autocorrelation loves to lock onto 2x the true period,
        # which reads a woman as a man. If half the lag is nearly as strong,
        # take it.
        half = lag // 2
        if half >= self._min_lag:
            lo, hi = max(self._min_lag, half - 2), min(self._max_lag, half + 2)
            sub = acf[lo: hi + 1]
            if sub.size and float(sub.max()) > 0.85 * peak:
                lag = lo + int(np.argmax(sub))

        return self.sample_rate / float(lag)


# ─── Speaking pace ───────────────────────────────────────────────────────────

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_WS = re.compile(r"\s+")

# Units per second at a neutral conversational pace. Words for most languages,
# characters for Japanese (where whitespace tokens mean nothing).
_BASELINE_UPS = {
    "en-IN": 2.6, "en-US": 2.7,
    "hi": 2.4, "hi-IN": 2.4, "ta": 2.2, "ta-IN": 2.2, "te": 2.2, "te-IN": 2.2,
    "fi": 2.2, "sv": 2.5, "de": 2.3, "de-DE": 2.3, "nl": 2.4, "fr": 2.6,
    "ja": 6.0, "ja-JP": 6.0,
}


class PaceTracker:
    """Derives a playback-rate multiplier from how fast the user is talking.

    The caller marks every input-transcription chunk and then calls finish()
    at end of turn. Rate is a *ratio* against a per-language baseline, so 1.0
    means "average pace" — the browser multiplies it into whatever base rate
    that language already uses.
    """

    def __init__(
        self,
        language: str = "en-IN",
        min_rate: float = 0.9,
        max_rate: float = 1.1,
        alpha: float = 0.4,
        tail_pad: float = 0.4,
        min_duration: float = 1.0,
        min_units: int = 3,
        send_threshold: float = 0.03,
        max_step: float = 0.05,
    ):
        self.baseline = _BASELINE_UPS.get(language, 2.5)
        self._cjk = language.startswith("ja")
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.alpha = alpha
        self.tail_pad = tail_pad
        self.min_duration = min_duration
        self.min_units = min_units
        self.send_threshold = send_threshold
        # playbackRate resamples, so every rate change shifts pitch. Capping how
        # far it can move in one turn keeps the agent sounding like one person
        # rather than jumping timbre between replies.
        self.max_step = max_step

        self.rate = 1.0
        self._last_sent = 1.0
        self._first_ts: Optional[float] = None
        self._last_ts: Optional[float] = None

    def mark_chunk(self, ts: float) -> None:
        """Record the arrival of an input-transcription chunk."""
        if self._first_ts is None:
            self._first_ts = ts
        self._last_ts = ts

    def reset_turn(self) -> None:
        self._first_ts = None
        self._last_ts = None

    def finish(self, text: str) -> Optional[float]:
        """End of user turn. Returns a new rate if it moved enough to be worth sending."""
        first, last = self._first_ts, self._last_ts
        self.reset_turn()
        if first is None or last is None or last <= first:
            return None

        # Transcription streams a little behind the audio and stops at the last
        # word, so pad for the trailing word we never see a chunk for.
        duration = (last - first) + self.tail_pad
        units = self._count_units(text)
        if duration < self.min_duration or units < self.min_units:
            return None

        observed = units / duration
        # Clamp the raw observation before it enters the EMA so one clipped or
        # mis-transcribed turn can't yank the running average around.
        raw = min(1.6, max(0.6, observed / self.baseline))
        target = self.alpha * raw + (1 - self.alpha) * self.rate
        # Move towards the target gradually — a big jump is audible as a pitch shift.
        step = min(self.max_step, max(-self.max_step, target - self.rate))
        self.rate = min(self.max_rate, max(self.min_rate, self.rate + step))

        if abs(self.rate - self._last_sent) < self.send_threshold:
            return None
        self._last_sent = self.rate
        return round(self.rate, 3)

    def _count_units(self, text: str) -> int:
        if self._cjk:
            return len(_CJK.findall(text))
        return len([w for w in _WS.split(text.strip()) if w])
