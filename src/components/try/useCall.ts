"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  BACKEND_LANGUAGE,
  greeting,
  optionKey,
  type CallOption,
  type Turn,
  type Vertical,
} from "@/lib/verticals";
import { scene } from "@/lib/sceneState";
import {
  VoiceCall,
  bridgeUrl,
  checkBridge,
  type PaymentState,
} from "@/lib/voiceClient";

export type CallStatus = "idle" | "ringing" | "live" | "ended";
export type CallMode = "live" | "scripted";
export type BridgeState = "checking" | "ready" | "offline";

/** How long a scripted turn takes to speak, from its length. */
const speakMs = (text: string) => Math.min(5200, 950 + text.length * 34);

/**
 * Drives one call.
 *
 * With NEXT_PUBLIC_VOICE_BRIDGE_URL set, this opens a real Gemini Live session
 * against `backend/` — microphone in, agent audio out, transcript as it is
 * spoken. Without it, or if the mic or the bridge is unavailable, it falls back
 * to the scripted conversation so the page is never broken.
 *
 * Either way it feeds the 3D field the same two signals: loudness and whose
 * turn it is.
 */
export function useCall(vertical: Vertical, option: CallOption) {
  const langCode = option.code;
  const [status, setStatus] = useState<CallStatus>("idle");
  const [mode, setMode] = useState<CallMode>("scripted");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [elapsed, setElapsed] = useState(0);
  const [latency, setLatency] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [bridge, setBridge] = useState<BridgeState>("checking");
  const [payment, setPayment] = useState<PaymentState | null>(null);

  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const raf = useRef(0);
  const call = useRef<VoiceCall | null>(null);
  /** Wall clock for the timer, and the start of the agent's current turn. */
  const startedAt = useRef(0);
  const turnStart = useRef(0);

  const clearAll = useCallback(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
    cancelAnimationFrame(raf.current);
    raf.current = 0;
    const active = call.current;
    call.current = null;
    void active?.stop();
  }, []);

  // A new business or a new language means a new call. Clearing during render
  // rather than in an effect means the panel never paints the previous call's
  // transcript for a frame before dropping it.
  const callKey = `${vertical.id}|${optionKey(option)}`;
  const [activeKey, setActiveKey] = useState(callKey);
  if (callKey !== activeKey) {
    setActiveKey(callKey);
    setStatus("idle");
    setTurns([]);
    setElapsed(0);
    setLatency(null);
    setNotice(null);
    setPayment(null);
  }

  // Timers, the socket and the 3D field are outside React, so they are
  // synchronised here.
  useEffect(() => {
    clearAll();
    scene.energy = 0.3;
    scene.turn = 1;
  }, [activeKey, clearAll]);

  useEffect(() => () => clearAll(), [clearAll]);

  useEffect(() => {
    let alive = true;
    void checkBridge().then((ok) => {
      if (alive) setBridge(ok ? "ready" : "offline");
    });
    return () => {
      alive = false;
    };
  }, []);

  const startClock = useCallback(() => {
    startedAt.current = performance.now();
    const tick = () => {
      setElapsed((performance.now() - startedAt.current) / 1000);
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
  }, []);

  // ── Scripted playback ─────────────────────────────────────────────────────
  const startScripted = useCallback(
    (why?: string | null) => {
      clearAll();
      setTurns([]);
      setElapsed(0);
      setMode("scripted");
      setNotice(why ?? null);
      setStatus("ringing");

      const at = (ms: number, fn: () => void) => {
        timers.current.push(setTimeout(fn, ms));
      };

      at(1150, () => {
        setStatus("live");
        startClock();
      });

      let cursor = 1150;
      vertical.turns.forEach((turn, i) => {
        const text =
          i === 0 && !turn.text
            ? greeting(langCode, vertical.business)
            : turn.text;
        cursor += turn.who === "agent" ? (turn.latency ?? 250) : 420;

        at(cursor, () => {
          setTurns((prev) => [...prev, { ...turn, text }]);
          setLatency(turn.who === "agent" ? (turn.latency ?? null) : null);
          scene.turn = turn.who === "agent" ? 1 : -1;
          scene.energy = 0.9;
        });

        cursor += speakMs(text);
        at(cursor, () => {
          scene.energy = 0.3;
        });
      });

      at(cursor + 900, () => {
        setStatus("ended");
        scene.energy = 0.3;
        cancelAnimationFrame(raf.current);
      });
    },
    [vertical, langCode, clearAll, startClock],
  );

  // ── Real call ─────────────────────────────────────────────────────────────
  const startLive = useCallback(
    async (url: string, language: string) => {
      clearAll();
      setTurns([]);
      setElapsed(0);
      setLatency(null);
      setNotice(null);
      setPayment(null);
      setMode("live");
      setStatus("ringing");

      const client = new VoiceCall(
        { url, scenario: vertical.scenario, language, accent: option.accent },
        {
          onTranscript: (role, text) => {
            if (!text.trim()) return;
            const who = role === "assistant" ? "agent" : "caller";
            if (who === "caller") {
              // They have just finished a turn; the clock runs from here.
              turnStart.current = performance.now();
            } else if (turnStart.current) {
              setLatency(Math.round(performance.now() - turnStart.current));
              turnStart.current = 0;
            }
            // The bridge streams partial transcripts, so consecutive fragments
            // from the same speaker extend that speaker's turn instead of
            // stacking up as separate bubbles.
            setTurns((prev) => {
              const last = prev[prev.length - 1];
              if (last && last.who === who) {
                const merged = [...prev];
                merged[merged.length - 1] = {
                  ...last,
                  text: `${last.text} ${text}`.trim(),
                };
                return merged;
              }
              return [...prev, { who, text: text.trim() }];
            });
          },
          onStatus: (s) => {
            if (s === "speaking") {
              scene.turn = 1;
            } else {
              scene.turn = -1;
              scene.energy = 0.3;
            }
          },
          onLevel: (level) => {
            scene.energy = 0.3 + level * 0.7;
          },
          onPayment: (update) =>
            setPayment((prev) =>
              // A settlement carries no URL, so it merges onto the link that
              // opened it rather than replacing it.
              update.url
                ? { ...(prev ?? {}), ...update, url: update.url }
                : prev
                  ? { ...prev, ...update }
                  : null,
            ),
          onError: (message) => setNotice(message),
          onClose: () => {
            setStatus("ended");
            scene.energy = 0.3;
            cancelAnimationFrame(raf.current);
          },
        },
      );

      try {
        // Playback first. If the mic is refused you can still hear the agent,
        // which is far more useful than swapping in a recording.
        await client.connect();
        call.current = client;
        setStatus("live");
        startClock();
      } catch (e) {
        await client.stop();
        setStatus("ended");
        setNotice(
          e instanceof Error
            ? `${e.message}.`
            : "Could not reach the voice bridge.",
        );
        return;
      }

      try {
        await client.enableMic();
      } catch (e) {
        setNotice(
          e instanceof DOMException && e.name === "NotAllowedError"
            ? "Microphone blocked, so the agent cannot hear you — you will still hear it speak."
            : "No microphone available — listen-only.",
        );
      }
    },
    [vertical, option.accent, clearAll, startClock],
  );

  const start = useCallback(() => {
    const url = bridgeUrl();
    // ggs_support parses `language` as a BCP-47 code and keys its greeting and
    // language-override off it. The other four take a bare language name.
    const language =
      vertical.languageFormat === "bcp47"
        ? (option.backendLanguage ?? langCode)
        : BACKEND_LANGUAGE[langCode];

    if (bridge === "ready" && url && language) {
      void startLive(url, language);
      return;
    }

    // Anything else plays the recording — but always says why, so a scripted
    // call can never be mistaken for a real one.
    startScripted(
      bridge === "offline"
        ? "Voice bridge unreachable — this is a recorded call, not a live agent."
        : !language
          ? "This agent has no live branch for that language yet — this is a recorded call."
          : "Checking the voice bridge — this is a recorded call.",
    );
  }, [
    bridge,
    langCode,
    vertical.languageFormat,
    option.backendLanguage,
    startLive,
    startScripted,
  ]);

  /** Explicitly ask for the recording, whatever the bridge is doing. */
  const playRecording = useCallback(() => startScripted(null), [startScripted]);

  const hangUp = useCallback(() => {
    clearAll();
    setStatus("ended");
    scene.energy = 0.3;
  }, [clearAll]);

  const reset = useCallback(() => {
    clearAll();
    setStatus("idle");
    setTurns([]);
    setElapsed(0);
    setLatency(null);
    setNotice(null);
    setPayment(null);
    scene.energy = 0.3;
    scene.turn = 1;
  }, [clearAll]);

  // Idle breath, so the orb is never inert.
  useEffect(() => {
    if (status === "idle") scene.energy = 0.3;
  }, [status]);

  return {
    status,
    mode,
    bridge,
    notice,
    payment,
    playRecording,
    turns,
    elapsed,
    latency,
    start,
    hangUp,
    reset,
  };
}
