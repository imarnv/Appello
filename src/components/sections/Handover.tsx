"use client";

import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Section, SectionHead } from "./Section";

const EASE = [0.16, 1, 0.3, 1] as const;

/**
 * The bands are the real ones from `GenderDetector` in voice_adapt.py. The gap
 * between them is deliberate: a pitch that lands there is undecidable, and the
 * detector abstains rather than guessing, which is what stops it flip-flopping
 * on low-voiced women and high-voiced men.
 */
const MALE = { lo: 85, hi: 155 };
const FEMALE = { lo: 165, hi: 255 };
const RANGE = { lo: 70, hi: 280 };

const pct = (hz: number) => ((hz - RANGE.lo) / (RANGE.hi - RANGE.lo)) * 100;

function verdictFor(hz: number): "male" | "female" | null {
  if (hz >= MALE.lo && hz <= MALE.hi) return "male";
  if (hz >= FEMALE.lo && hz <= FEMALE.hi) return "female";
  return null;
}

const STEPS = [
  { t: "0 ms", label: "Pitch crosses into a decided band" },
  { t: "+120 ms", label: "Agent offers the switch, in its current voice" },
  { t: "on yes", label: "New session opens with the chosen voice" },
  { t: "+40 ms", label: "Last 12 turns replayed, turn_complete false" },
  { t: "resume", label: "It carries on mid-sentence — no re-greeting" },
];

export default function Handover() {
  const [hz, setHz] = useState(120);
  const verdict = verdictFor(hz);
  const [voice, setVoice] = useState<"male" | "female">("male");
  const [swapping, setSwapping] = useState(false);

  const applySwap = () => {
    if (!verdict || verdict === voice) return;
    setSwapping(true);
    window.setTimeout(() => {
      setVoice(verdict);
      setSwapping(false);
    }, 620);
  };

  return (
    <Section id="handover">
      <SectionHead
        eyebrow="Voice handover"
        title="It hears who picked up, and offers to match."
        lede="Appello reads the caller's pitch from the same audio it is already streaming — no extra model, no extra round trip. If a different person answers, it offers to switch voice, and carries the conversation across intact."
      />

      <div className="mt-10 grid gap-10 lg:grid-cols-[minmax(0,1fr)_minmax(0,24rem)] lg:gap-14">
        {/* ── Interactive pitch reading ─────────────────────────────────── */}
        <div>
          <div className="flex items-baseline justify-between">
            <span className="type-meta text-ink-3">
              Caller fundamental (F0)
            </span>
            <span className="type-data text-[0.9375rem] text-ink">{hz} Hz</span>
          </div>

          <div className="relative mt-6 h-24">
            {/* Bands */}
            <div className="absolute inset-x-0 top-8 h-10 rounded-lg border border-hairline" />
            <div
              className="absolute top-8 h-10 rounded-l-lg"
              style={{
                left: `${pct(MALE.lo)}%`,
                width: `${pct(MALE.hi) - pct(MALE.lo)}%`,
                background:
                  "color-mix(in srgb, var(--color-s1) 10%, transparent)",
              }}
            />
            <div
              className="absolute top-8 h-10"
              style={{
                left: `${pct(FEMALE.lo)}%`,
                width: `${pct(FEMALE.hi) - pct(FEMALE.lo)}%`,
                background:
                  "color-mix(in srgb, var(--color-s3) 12%, transparent)",
              }}
            />

            {/* Reading */}
            <motion.div
              className="absolute top-4 w-px"
              style={{
                left: `${pct(hz)}%`,
                height: "4.5rem",
                background: verdict ? "var(--color-s2)" : "var(--color-ink-3)",
              }}
              animate={{ opacity: verdict ? 1 : 0.5 }}
            />

            <input
              type="range"
              min={RANGE.lo}
              max={RANGE.hi}
              value={hz}
              onChange={(e) => setHz(Number(e.target.value))}
              aria-label="Caller fundamental frequency"
              className="absolute inset-x-0 top-8 h-10 w-full cursor-pointer opacity-0"
            />

            <div className="absolute inset-x-0 top-20 flex justify-between">
              <span className="type-meta text-[0.5625rem] text-ink-3">
                {RANGE.lo} Hz
              </span>
              <span className="type-meta text-[0.5625rem] text-ink-3">
                {RANGE.hi} Hz
              </span>
            </div>
          </div>

          <div className="mt-8 flex flex-wrap items-center gap-3">
            <span className="type-meta text-ink-3">Verdict</span>
            <AnimatePresence mode="wait">
              <motion.span
                key={verdict ?? "none"}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ duration: 0.28, ease: EASE }}
                className="type-data rounded-full border px-3 py-1.5 text-[0.8125rem]"
                style={{
                  color: verdict ? "var(--color-s1)" : "var(--color-ink-3)",
                  borderColor: verdict
                    ? "color-mix(in srgb, var(--color-s2) 26%, transparent)"
                    : "var(--color-hairline)",
                  background: verdict
                    ? "color-mix(in srgb, var(--color-s2) 7%, transparent)"
                    : "transparent",
                }}
              >
                {verdict ?? "abstains — between the bands"}
              </motion.span>
            </AnimatePresence>
          </div>

          <p className="mt-5 max-w-[48ch] text-[0.9375rem] leading-relaxed text-ink-2">
            Between 155 and 165 Hz the detector deliberately says nothing. A
            model that guesses in that gap spends the call switching back and
            forth, which is far worse than never switching at all.
          </p>
        </div>

        {/* ── The agent's voice, and the handover ───────────────────────── */}
        <div className="rounded-2xl border border-hairline p-6">
          <span className="type-meta text-ink-3">Agent voice</span>

          <div className="relative mt-5 h-16">
            <AnimatePresence mode="wait">
              <motion.div
                key={voice + String(swapping)}
                initial={{ opacity: 0, y: 14, filter: "blur(8px)" }}
                animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
                exit={{ opacity: 0, y: -14, filter: "blur(8px)" }}
                transition={{ duration: 0.42, ease: EASE }}
                className="absolute inset-0 flex items-center"
              >
                <span className="type-h2 text-[1.75rem]">
                  {swapping
                    ? "handing over"
                    : voice === "male"
                      ? "Charon"
                      : "Aoede"}
                </span>
              </motion.div>
            </AnimatePresence>
          </div>

          <p className="text-[0.875rem] leading-relaxed text-ink-3">
            {swapping
              ? "Reconnecting, replaying context…"
              : voice === "male"
                ? "Indian male · the default for this agent"
                : "Indian female"}
          </p>

          <button
            onClick={applySwap}
            disabled={!verdict || verdict === voice || swapping}
            className="mt-6 h-11 w-full cursor-pointer rounded-full bg-ink text-[0.9375rem] font-medium text-white transition-all duration-200 hover:scale-[1.02] active:scale-[0.98] disabled:cursor-not-allowed disabled:bg-hairline-2 disabled:text-ink-3"
          >
            {!verdict
              ? "No verdict to act on"
              : verdict === voice
                ? "Already matched"
                : `Switch to ${verdict === "male" ? "Charon" : "Aoede"}`}
          </button>

          <ol className="mt-6 border-t border-hairline">
            {STEPS.map((s) => (
              <li
                key={s.label}
                className="flex gap-4 border-b border-hairline py-2.5"
              >
                <span className="type-data w-16 shrink-0 text-[0.75rem] text-ink-3">
                  {s.t}
                </span>
                <span className="text-[0.8125rem] leading-snug text-ink-2">
                  {s.label}
                </span>
              </li>
            ))}
          </ol>

          <p className="mt-5 text-[0.8125rem] leading-relaxed text-ink-3">
            Capped at three switches a call, so a borderline speaker can never
            thrash the session.
          </p>
        </div>
      </div>
    </Section>
  );
}
