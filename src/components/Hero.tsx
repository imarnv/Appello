"use client";

import { useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { TOTAL_LANGUAGES } from "@/lib/signatures";

const EASE = [0.16, 1, 0.3, 1] as const;

/**
 * Four answers to the question in the eyebrow. "You don't." holds; this line
 * turns over, and each phrase carries a different reason it is true — picking
 * up, dialect, grounding, hours.
 */
const ANSWERS = [
  "Appello picks up.", // it answers at all
  "Appello answers first.", // on the first ring, not the fourth
  "Appello speaks 94 ways.", // every language, in its own dialect
  "Appello shows its work.", // every fact traced to a document
  "Appello books the table.", // it acts, not just talks
  "Appello works nights.", // no shift to cover
];

/** Longest phrase — reserves the line box so nothing below ever shifts. */
const SIZER = "Appello books the table.";

const HOLD_MS = 3600;

/**
 * Words leave and arrive one at a time, drifting a little and blurring as they
 * go. Softer than a mask wipe, and it reads as speech being replaced rather
 * than a slide transition.
 */

/** A line that rises out from behind a mask, the way a caption card would. */
function Line({
  children,
  delay,
  className = "",
}: {
  children: React.ReactNode;
  delay: number;
  className?: string;
}) {
  return (
    <span className="block overflow-hidden pb-[0.08em]">
      <motion.span
        className={`block ${className}`}
        initial={{ y: "108%" }}
        animate={{ y: "0%" }}
        transition={{ duration: 1.15, ease: EASE, delay }}
      >
        {children}
      </motion.span>
    </span>
  );
}

/** The answer line, turning over in place behind the same mask as the reveal. */
function RotatingAnswer() {
  const [i, setI] = useState(0);
  // Hold the line back until "You don't." has finished arriving.
  const [ready, setReady] = useState(false);
  const reduced = useReducedMotion();

  useEffect(() => {
    const kickoff = setTimeout(() => setReady(true), 420);
    return () => clearTimeout(kickoff);
  }, []);

  useEffect(() => {
    if (reduced || !ready) return;
    const id = setInterval(
      () => setI((v) => (v + 1) % ANSWERS.length),
      HOLD_MS,
    );
    return () => clearInterval(id);
  }, [reduced, ready]);

  return (
    <span className="relative block pb-[0.08em]">
      <span className="invisible block" aria-hidden="true">
        {SIZER}
      </span>
      <AnimatePresence mode="wait" initial>
        {ready && (
          <motion.span key={i} className="absolute inset-0 block">
            {ANSWERS[i].split(" ").map((word, k, all) => (
              <motion.span
                key={k}
                className="inline-block will-change-[transform,opacity,filter]"
                initial={{ opacity: 0, y: "0.3em", filter: "blur(10px)" }}
                animate={{
                  opacity: 1,
                  y: "0em",
                  filter: "blur(0px)",
                  transition: { duration: 0.75, ease: EASE, delay: k * 0.06 },
                }}
                exit={{
                  opacity: 0,
                  y: "-0.24em",
                  filter: "blur(10px)",
                  transition: {
                    duration: 0.42,
                    ease: "easeIn",
                    delay: k * 0.04,
                  },
                }}
              >
                {word}
                {k < all.length - 1 ? "\u00A0" : ""}
              </motion.span>
            ))}
          </motion.span>
        )}
      </AnimatePresence>
    </span>
  );
}

const SPECS = [
  { value: "290 ms", label: "median response" },
  { value: String(TOTAL_LANGUAGES), label: "languages, native dialect" },
  { value: "SIP · WebRTC · SDK", label: "wherever calls arrive" },
  { value: "Cited", label: "every factual answer" },
];

export default function Hero() {
  return (
    <section
      id="top"
      className="relative flex min-h-[100svh] flex-col justify-between pb-0 pt-28 md:pt-40"
    >
      <div className="mx-auto w-full max-w-[1240px] px-6 lg:px-10">
        {/* The question the page exists to answer, asked plainly. */}
        <motion.p
          className="type-meta text-ink-3"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.9, ease: EASE, delay: 0.15 }}
        >
          Why do you still need a person on the phone?
        </motion.p>

        <h1 className="type-display mt-6 text-[clamp(2.3rem,7.2vw,6.1rem)] md:mt-7">
          <Line delay={0.32}>You don&rsquo;t.</Line>
          <RotatingAnswer />
        </h1>

        <div className="mt-7 grid gap-8 md:grid-cols-[minmax(0,30rem)_auto] md:items-start">
          <motion.p
            className="text-[1rem] leading-[1.6] text-ink-2 md:text-[1.0625rem]"
            initial={{ opacity: 0, y: 14 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 1, ease: EASE, delay: 0.72 }}
          >
            Voice agents for SIP lines and for your website. They answer on the
            first ring, speak {TOTAL_LANGUAGES} languages in the dialect the
            caller actually uses, and take every fact from your own documents —
            so you can see where each answer came from.
          </motion.p>

          <motion.div
            className="flex flex-wrap items-center gap-3 md:pt-1"
            initial={{ opacity: 0, y: 14 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 1, ease: EASE, delay: 0.84 }}
          >
            <a
              href="#try"
              className="inline-flex h-12 items-center rounded-full bg-ink px-7 text-[0.9375rem] font-medium text-white transition-transform duration-200 hover:scale-[1.03] active:scale-[0.98]"
            >
              Try a live agent
            </a>
            <a
              href="#try"
              className="inline-flex h-12 items-center rounded-full border border-hairline px-7 text-[0.9375rem] font-medium text-ink transition-colors duration-200 hover:border-ink/25 hover:bg-hairline-2"
            >
              Talk to sales
            </a>
          </motion.div>
        </div>
      </div>

      {/* The voice field occupies this space — nothing is painted over it. */}
      <div className="pointer-events-none min-h-[26vh] flex-1" />

      <motion.div
        className="mx-auto w-full max-w-[1240px] px-6 lg:px-10"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 1.1, ease: EASE, delay: 1.05 }}
      >
        <dl className="grid grid-cols-2 border-t border-hairline md:grid-cols-4">
          {SPECS.map((s, i) => (
            <div
              key={s.label}
              className={`px-0 py-5 md:px-6 ${i > 0 ? "md:border-l md:border-hairline" : ""} ${i % 2 === 1 ? "border-l border-hairline pl-5 md:pl-6" : ""}`}
            >
              <dt className="type-data text-[0.9375rem] text-ink">{s.value}</dt>
              <dd className="mt-1 text-[0.8125rem] text-ink-3">{s.label}</dd>
            </div>
          ))}
        </dl>
      </motion.div>
    </section>
  );
}
