"use client";

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Section, SectionHead } from "./Section";

const EASE = [0.16, 1, 0.3, 1] as const;

/**
 * Each channel is the same agent behind a different front door, and the only
 * real difference between them is how many hops the audio takes before it
 * reaches the model. So that is what this shows: the path, and what each hop
 * costs — rather than a snippet nobody can judge at a glance.
 */
const CHANNELS = [
  {
    id: "sip",
    label: "SIP",
    blurb: "Point your existing number at Appello. No new hardware, no port.",
    meta: "inbound + outbound · E.164",
    hops: [
      { label: "Caller", ms: 0 },
      { label: "SIP trunk", ms: 38 },
      { label: "Bridge", ms: 14 },
      { label: "Gemini Live", ms: 238 },
      { label: "First audio", ms: 0 },
    ],
    code: `sip:+914440001234@appello.io
  agent    = royal-plate
  codec    = PCMU, PCMA, opus
  fallback = +914440009999`,
  },
  {
    id: "web",
    label: "Web",
    blurb: "A widget on your site. Browser mic in, agent audio out.",
    meta: "WebRTC · one script tag",
    hops: [
      { label: "Browser mic", ms: 0 },
      { label: "WebRTC", ms: 11 },
      { label: "Bridge", ms: 14 },
      { label: "Gemini Live", ms: 238 },
      { label: "First audio", ms: 0 },
    ],
    code: `<script
  src="https://cdn.appello.io/widget.js"
  data-agent="royal-plate"
  data-language="en-IN"
  defer
></script>`,
  },
  {
    id: "sdk",
    label: "SDK",
    blurb: "Drive the socket yourself when you want the raw stream.",
    meta: "PCM16 · 24 kHz · WebSocket",
    hops: [
      { label: "Your app", ms: 0 },
      { label: "WebSocket", ms: 8 },
      { label: "Bridge", ms: 14 },
      { label: "Gemini Live", ms: 238 },
      { label: "First audio", ms: 0 },
    ],
    code: `const call = await appello.connect({
  agent: "royal-plate",
  language: "en-IN",
  accent: "indian",
});

call.on("transcript", ({ role, text }) => render(role, text));`,
  },
] as const;

/**
 * One pass of a call down the path — played once, not looped. It runs when the
 * card first comes into view and again when you switch channel, then rests at
 * the finished state. A permanent loop is just movement in the corner of your
 * eye once you have understood it.
 */
function useTravel(resetKey: string, ready: boolean) {
  const [p, setP] = useState(0);
  const [run, setRun] = useState(0);
  const raf = useRef(0);

  useEffect(() => {
    if (!ready) return;

    // Someone who has asked for less motion still needs to see the finished
    // path, so jump to it rather than leaving an empty track.
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      const id = setTimeout(() => setP(1), 0);
      return () => clearTimeout(id);
    }

    const start = performance.now();
    const DURATION = 2400;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / DURATION);
      setP(t);
      if (t < 1) raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf.current);
  }, [resetKey, ready, run]);

  return { p, replay: () => setRun((n) => n + 1) };
}

export default function Channels() {
  const [tab, setTab] = useState(0);
  const active = CHANNELS[tab];

  // Nothing moves until the card is actually on screen.
  const card = useRef<HTMLDivElement>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = card.current;
    if (!el) return;
    const io = new IntersectionObserver(
      ([e]) => e.isIntersecting && setSeen(true),
      { rootMargin: "-15% 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  const { p, replay } = useTravel(active.id, seen);

  const total = active.hops.reduce((n, h) => n + h.ms, 0);
  const elapsed = Math.round(p * total);
  const nodeAt = (i: number) => (i / (active.hops.length - 1)) * 100;

  return (
    <Section id="channels">
      <SectionHead
        eyebrow="Where it runs"
        title="Wherever the call already arrives."
        lede="Most businesses do not want a new phone system. They want the number on their door answered. Appello sits behind whichever channel you already publish — the agent is identical, only the first hop changes."
      />

      <div className="mt-9 flex flex-wrap gap-2">
        {CHANNELS.map((c, i) => {
          const on = i === tab;
          return (
            <button
              key={c.id}
              onClick={() => setTab(i)}
              aria-selected={on}
              role="tab"
              className={`cursor-pointer rounded-full border px-4 py-2 text-[0.875rem] transition-all duration-200 ${
                on
                  ? "border-ink bg-ink text-white"
                  : "border-hairline text-ink-2 hover:border-ink/25 hover:text-ink"
              }`}
            >
              {c.label}
            </button>
          );
        })}
      </div>

      {/* ── The path a call actually takes ────────────────────────────────── */}
      <div
        ref={card}
        className="mt-7 overflow-hidden rounded-2xl border border-hairline"
      >
        <div className="flex flex-wrap items-baseline justify-between gap-3 border-b border-hairline px-6 py-3.5">
          <span className="type-meta text-ink-3">{active.meta}</span>
          <div className="flex items-center gap-4">
            <span className="type-data text-[0.8125rem] tabular-nums text-ink-3">
              {elapsed} / {total} ms to first audio
            </span>
            <button
              onClick={replay}
              className="type-meta cursor-pointer text-[0.5625rem] text-ink-3 transition-colors duration-200 hover:text-ink"
            >
              replay
            </button>
          </div>
        </div>

        <div className="px-8 pb-10 pt-12 md:px-12">
          <div className="relative h-1">
            <div className="absolute inset-0 rounded-full bg-hairline-2" />
            <div
              className="absolute inset-y-0 left-0 rounded-full"
              style={{
                width: `${p * 100}%`,
                background:
                  "linear-gradient(to right, var(--color-s1), var(--color-s3))",
              }}
            />
            <div
              className="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full transition-opacity duration-500"
              style={{
                left: `${p * 100}%`,
                opacity: p >= 1 ? 0 : 1,
                background: "var(--color-s3)",
                boxShadow:
                  "0 0 0 5px color-mix(in srgb, var(--color-s3) 16%, transparent)",
              }}
            />

            {active.hops.map((h, i) => {
              const at = nodeAt(i);
              const passed = p * 100 >= at - 0.5;
              return (
                <div
                  key={h.label}
                  className="absolute top-1/2 -translate-y-1/2"
                  style={{ left: `${at}%` }}
                >
                  <div
                    className="size-2.5 -translate-x-1/2 rounded-full border transition-colors duration-300"
                    style={{
                      background: passed ? "var(--color-s2)" : "#fff",
                      borderColor: passed
                        ? "var(--color-s2)"
                        : "var(--color-hairline)",
                    }}
                  />
                  <div
                    className="absolute -top-9 -translate-x-1/2 whitespace-nowrap transition-opacity duration-300"
                    style={{ opacity: passed ? 1 : 0.4 }}
                  >
                    <span className="type-meta text-[0.5625rem] text-ink-2">
                      {h.label}
                    </span>
                  </div>
                  {h.ms > 0 && (
                    <div className="absolute top-5 -translate-x-1/2 whitespace-nowrap">
                      <span className="type-data text-[0.6875rem] text-ink-3">
                        +{h.ms}
                      </span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div className="grid border-t border-hairline lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
          <div className="border-b border-hairline p-6 lg:border-b-0 lg:border-r">
            <AnimatePresence mode="wait">
              <motion.p
                key={active.id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ duration: 0.35, ease: EASE }}
                className="max-w-[28ch] text-[1rem] leading-[1.55] text-ink"
              >
                {active.blurb}
              </motion.p>
            </AnimatePresence>
          </div>

          <div className="overflow-x-auto bg-paper-2 p-6">
            <AnimatePresence mode="wait">
              <motion.pre
                key={active.id}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.3, ease: EASE }}
                className="type-data text-[0.78125rem] leading-[1.7] text-ink-2"
              >
                <code>{active.code}</code>
              </motion.pre>
            </AnimatePresence>
          </div>
        </div>
      </div>

      <p className="mt-6 max-w-[60ch] text-[0.875rem] leading-relaxed text-ink-3">
        The model is the budget. Every hop Appello controls costs tens of
        milliseconds against its two hundred and thirty-eight — which is why the
        work goes into never adding an extra one, not into shaving the ones that
        are already small.
      </p>
    </Section>
  );
}
