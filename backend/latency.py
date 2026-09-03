"""
Appello — Per-turn Latency Profiler
Records monotonic timestamps for every pipeline stage so we can identify bottlenecks.
"""

import logging
import time
from typing import Optional, Dict, Any

logger = logging.getLogger("appello")


class LatencyTracker:
    """
    Per-turn latency profiler. Records monotonic timestamps for every
    pipeline stage so we can identify the real bottleneck.

    Pipeline stages (in order):
      1. speech_start        – VAD fires "user is speaking"
      2. speech_end           – VAD fires "silence detected" (end of user turn)
      3. stt_complete         – Whisper transcription arrives
      4. response_created     – Azure starts generating a response
      5. first_text_token     – First text delta from LLM (TTFT)
      6. first_sentence_ready – First full sentence extracted from buffer
      7. tts_request_sent     – TTS HTTP request dispatched for first sentence
      8. tts_audio_received   – TTS audio bytes received for first sentence
      9. first_audio_to_client – First audio chunk sent to browser WebSocket
      10. response_done        – Azure signals response.done
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.turn_count = 0
        self._timestamps: Dict[str, float] = {}
        self._tts_timings: list = []  # list of (sentence, request_ms, synthesis_ms)
        self._active = False

    def new_turn(self):
        """Reset for a new user turn."""
        self.turn_count += 1
        self._timestamps = {}
        self._tts_timings = []
        self._active = True

    def mark(self, stage: str):
        """Record a timestamp for a pipeline stage (only first occurrence)."""
        if stage not in self._timestamps:
            self._timestamps[stage] = time.monotonic()

    def mark_tts(self, sentence: str, request_sent: float, audio_received: float):
        """Record TTS timing for a specific sentence."""
        self._tts_timings.append({
            "sentence": sentence[:60],
            "tts_latency_ms": round((audio_received - request_sent) * 1000, 1),
        })

    def _delta(self, start: str, end: str) -> Optional[float]:
        """Return ms between two stages, or None if either is missing."""
        s = self._timestamps.get(start)
        e = self._timestamps.get(end)
        if s is not None and e is not None:
            return round((e - s) * 1000, 1)
        return None

    def report(self) -> Dict[str, Any]:
        """Build a human-readable latency report for this turn."""
        r: Dict[str, Any] = {
            "turn": self.turn_count,
            "session": self.session_id,
        }

        # ── Key deltas ───────────────────────────────────────────
        r["user_speech_duration_ms"] = self._delta("speech_start", "speech_end")
        r["vad_to_stt_ms"] = self._delta("speech_end", "stt_complete")
        r["vad_to_response_created_ms"] = self._delta("speech_end", "response_created")
        r["vad_to_first_token_ms (TTFT)"] = self._delta("speech_end", "first_text_token")
        r["first_token_to_first_sentence_ms"] = self._delta("first_text_token", "first_sentence_ready")
        r["first_sentence_to_tts_request_ms"] = self._delta("first_sentence_ready", "tts_request_sent")
        r["tts_request_to_audio_received_ms"] = self._delta("tts_request_sent", "tts_audio_received")
        r["tts_audio_to_client_send_ms"] = self._delta("tts_audio_received", "first_audio_to_client")

        # ── End-to-end ───────────────────────────────────────────
        r["E2E_speech_end_to_first_audio_ms"] = self._delta("speech_end", "first_audio_to_client")
        r["E2E_speech_end_to_response_done_ms"] = self._delta("speech_end", "response_done")

        # ── Per-sentence TTS breakdown ───────────────────────────
        r["tts_sentences"] = self._tts_timings

        return r

    def log_report(self):
        """Log a formatted latency report."""
        if not self._active:
            return
        rpt = self.report()
        lines = [
            f"\n{'='*70}",
            f"  LATENCY REPORT — Turn #{rpt['turn']}  (Session: {rpt['session']})",
            f"{'='*70}",
        ]
        # Print deltas
        for key, val in rpt.items():
            if key in ("turn", "session", "tts_sentences"):
                continue
            display = f"{val}ms" if val is not None else "—"
            label = key.replace("_", " ").replace(" ms", "")
            lines.append(f"  {label:.<50s} {display}")

        # TTS per-sentence
        if rpt["tts_sentences"]:
            lines.append(f"  {'─'*50}")
            lines.append(f"  TTS per-sentence breakdown:")
            for i, s in enumerate(rpt["tts_sentences"], 1):
                lines.append(f"    [{i}] {s['tts_latency_ms']}ms — \"{s['sentence']}\"")

        lines.append(f"{'='*70}\n")
        logger.info("\n".join(lines))
        self._active = False
