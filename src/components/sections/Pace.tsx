"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "motion/react";
import { Section, SectionHead, Readout } from "./Section";

const EASE = [0.16, 1, 0.3, 1] as const;

/** Clamp from PACE_MIN / PACE_MAX in the bridge. */
const RATE_MIN = 0.9;
const RATE_MAX = 1.1;

/** Words per minute the tracker treats as the middle of the road. */
const WPM_MIN = 90;
const WPM_MAX = 210;
const WPM_MID = 150;

function rateFor(wpm: number) {
  const t = (wpm - WPM_MID) / (WPM_MAX - WPM_MID);
  return Math.min(RATE_MAX, Math.max(RATE_MIN, 1 + t * (RATE_MAX - 1)));
}

/**
 * The agent's delivery, drawn as syllables rather than as a carrier wave.
 *
 * Tempo is the thing being demonstrated, so tempo is what the picture encodes:
 * the number of syllable pulses across the width *is* the speaking rate. Drag
 * the slider and they pack in or spread out, which a smooth carrier could
 * never show — it would just look like a higher note.
 */
function Wave({ wpm, rate }: { wpm: number; rate: number }) {
  const [phase, setPhase] = useState(0);
  const raf = useRef(0);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = (now - last) / 1000;
      last = now;
      // Faster speech scrolls past faster, so the motion agrees with the shape.
      setPhase((p) => p + dt * (wpm / 150) * 0.9);
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf.current);
  }, [wpm]);

  // Roughly 1.4 syllables a word. The panel shows about four seconds of
  // speech, so that many syllables have to fit across the width — which is
  // what makes dragging the slider visibly pack them in.
  const perSecond = (wpm * 1.4) / 60;
  const WINDOW_SECONDS = 4;
  const humps = perSecond * WINDOW_SECONDS;

  const path = useMemo(() => {
    const W = 600;
    const H = 96;
    const mid = H / 2;
    const pts: string[] = [];
    for (let x = 0; x <= W; x += 2) {
      const u = x / W;
      // One hump per syllable, with a little variation so it reads as speech
      // and not as a metronome.
      const s = u * humps - phase;
      const hump = Math.sin((s - Math.floor(s)) * Math.PI);
      const vary = 0.55 + 0.45 * Math.abs(Math.sin(Math.floor(s) * 2.7));
      const env = Math.pow(hump, 1.4) * vary;
      const carrier = Math.sin(u * Math.PI * 130 - phase * 8);
      const y = mid + carrier * env * (mid - 8);
      pts.push(`${x === 0 ? "M" : "L"}${x} ${y.toFixed(1)}`);
    }
    return pts.join(" ");
  }, [humps, phase]);

  // Envelope outline, so the syllable count is legible even at a glance.
  const outline = useMemo(() => {
    const W = 600;
    const H = 96;
    const mid = H / 2;
    const top: string[] = [];
    for (let x = 0; x <= W; x += 2) {
      const u = x / W;
      const s = u * humps - phase;
      const hump = Math.sin((s - Math.floor(s)) * Math.PI);
      const vary = 0.55 + 0.45 * Math.abs(Math.sin(Math.floor(s) * 2.7));
      const env = Math.pow(hump, 1.4) * vary;
      top.push(
        `${x === 0 ? "M" : "L"}${x} ${(mid - env * (mid - 8)).toFixed(1)}`,
      );
    }
    return top.join(" ");
  }, [humps, phase]);

  return (
    <svg
      viewBox="0 0 600 96"
      className="h-[6rem] w-full"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="paceGrad" x1="0" x2="1">
          <stop offset="0%" stopColor="var(--color-s1)" />
          <stop offset="50%" stopColor="var(--color-s2)" />
          <stop offset="100%" stopColor="var(--color-s3)" />
        </linearGradient>
      </defs>
      <path
        d={outline}
        fill="none"
        stroke="url(#paceGrad)"
        strokeWidth="1"
        opacity="0.28"
      />
      <path
        d={path}
        fill="none"
        stroke="url(#paceGrad)"
        strokeWidth="1.4"
        strokeLinecap="round"
        style={{ transition: "opacity 200ms" }}
        opacity={0.55 + (rate - 0.9) * 2}
      />
    </svg>
  );
}

export default function Pace() {
  const [wpm, setWpm] = useState(150);
  const rate = rateFor(wpm);

  const verdict =
    wpm > 175
      ? "speeding up to match"
      : wpm < 120
        ? "slowing to match"
        : "holding";

  return (
    <Section id="pace">
      <SectionHead
        eyebrow="Dynamic pace"
        title="It talks at the speed you talk."
        lede="Being read at is the tell that gives away a bot. Appello measures how fast the caller is actually speaking and adjusts its own delivery to sit alongside them — quick with someone in a hurry, unhurried with someone who is not."
      />

      <motion.div
        initial={{ opacity: 0, y: 20 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: "-12%" }}
        transition={{ duration: 0.9, ease: EASE }}
        className="mt-10 overflow-hidden rounded-2xl border border-hairline"
      >
        <div className="flex flex-wrap items-baseline justify-between gap-3 border-b border-hairline px-7 py-5">
          <span className="type-meta text-ink-3">Agent delivery</span>
          <span className="type-data text-[0.8125rem] text-ink-3">
            {verdict}
          </span>
        </div>

        <div className="px-7 py-6">
          <Wave wpm={wpm} rate={rate} />
        </div>

        <div className="border-t border-hairline px-7 py-6">
          <div className="flex items-baseline justify-between">
            <label htmlFor="wpm" className="type-meta text-ink-3">
              Caller speaking rate
            </label>
            <span className="type-data text-[0.9375rem] text-ink">
              {wpm} wpm
            </span>
          </div>
          <input
            id="wpm"
            type="range"
            min={WPM_MIN}
            max={WPM_MAX}
            value={wpm}
            onChange={(e) => setWpm(Number(e.target.value))}
            className="mt-4 h-1.5 w-full cursor-pointer appearance-none rounded-full"
            style={{
              background: `linear-gradient(to right, var(--color-s2) ${
                ((wpm - WPM_MIN) / (WPM_MAX - WPM_MIN)) * 100
              }%, var(--color-hairline) 0%)`,
            }}
          />
          <div className="mt-2 flex justify-between">
            <span className="type-meta text-[0.5625rem] text-ink-3">
              deliberate
            </span>
            <span className="type-meta text-[0.5625rem] text-ink-3">
              in a hurry
            </span>
          </div>
        </div>
      </motion.div>

      <div className="mt-9 grid gap-8 sm:grid-cols-3">
        <Readout
          value={`${rate.toFixed(2)}×`}
          label="playback rate right now"
          tone="spectral"
        />
        <Readout value="0.90–1.10×" label="the whole range it will ever use" />
        <Readout
          value="per turn"
          label="re-measured every time the caller speaks"
        />
      </div>

      <p className="mt-8 max-w-[58ch] text-[0.9375rem] leading-relaxed text-ink-3">
        The range is deliberately narrow. Past about ten percent either way the
        voice stops sounding like a person adjusting and starts sounding like a
        recording being scrubbed — so that is where it stops.
      </p>
    </Section>
  );
}
