"use client";

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";

const EASE = [0.16, 1, 0.3, 1] as const;

/**
 * One retrieval, played out. A caller's question, the chunks that came back
 * with their similarity scores, and the sentence the agent then spoke.
 *
 * The scores are the point: the agent did not pick the peanut row because
 * someone said "peanut", it picked it because that row sits closest to the
 * question in embedding space.
 */
const QUERIES = [
  {
    ask: "does anything have peanuts in it?",
    hits: [
      {
        src: "allergen-matrix.xlsx",
        span: "peanut · 6 of 41 dishes",
        score: 0.91,
      },
      {
        src: "menu-winter.pdf",
        span: "lamb korma · paneer lababdar",
        score: 0.58,
      },
      {
        src: "reservations-policy.md",
        span: "terrace seating rules",
        score: 0.12,
      },
    ],
    answer: "Six of the winter dishes are finished in peanut oil.",
  },
  {
    ask: "how late is the kitchen open?",
    hits: [
      { src: "hours.json", span: "dinner 19:00–23:00", score: 0.94 },
      {
        src: "reservations-policy.md",
        span: "last seating 22:15",
        score: 0.77,
      },
      { src: "menu-winter.pdf", span: "chef's special · all day", score: 0.21 },
    ],
    answer:
      "Dinner runs until eleven, and the last seating is quarter past ten.",
  },
  {
    ask: "can you do something for a birthday?",
    hits: [
      {
        src: "occasions.md",
        span: "birthday → dessert + decoration",
        score: 0.89,
      },
      {
        src: "menu-winter.pdf",
        span: "gulab jamun · included in thali",
        score: 0.44,
      },
      { src: "hours.json", span: "lunch 12:00–15:00", score: 0.08 },
    ],
    answer:
      "We'll arrange a complimentary dessert and decorate the table for you.",
  },
] as const;

type Phase = "asking" | "scoring" | "answering";

export default function Retrieval() {
  const [index, setIndex] = useState(0);
  const [phase, setPhase] = useState<Phase>("asking");
  const [typed, setTyped] = useState("");
  const [run, setRun] = useState(0);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  // Nothing starts until the card is on screen, and it stops after the last
  // question rather than cycling — an endless loop is movement you stop
  // reading and start ignoring.
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

  const q = QUERIES[index];
  const last = index === QUERIES.length - 1;

  useEffect(() => {
    if (!seen) return;
    const at = (ms: number, fn: () => void) => {
      timers.current.push(setTimeout(fn, ms));
    };
    const clear = () => {
      timers.current.forEach(clearTimeout);
      timers.current = [];
    };

    clear();

    // With reduced motion, show the last question already answered instead of
    // typing anything out.
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      at(0, () => {
        setTyped(q.ask);
        setPhase("answering");
      });
      return clear;
    }

    // Scheduled rather than set inline: the reset belongs on the same timeline
    // as everything else in the cycle.
    at(0, () => {
      setPhase("asking");
      setTyped("");
    });

    // Type the question a character at a time — this is the only place on the
    // page where a typewriter earns its keep, because the question arriving is
    // literally what starts a retrieval.
    const text = q.ask;
    for (let i = 1; i <= text.length; i++) {
      at(i * 26, () => setTyped(text.slice(0, i)));
    }
    const typedFor = text.length * 26;

    at(typedFor + 320, () => setPhase("scoring"));
    at(typedFor + 1750, () => setPhase("answering"));
    if (!last) at(typedFor + 4400, () => setIndex((n) => n + 1));

    return clear;
  }, [index, q.ask, seen, last, run]);

  const replay = () => {
    setIndex(0);
    setRun((n) => n + 1);
  };

  return (
    <div
      ref={card}
      className="overflow-hidden rounded-2xl border border-hairline"
    >
      <div className="flex items-center justify-between border-b border-hairline px-6 py-3.5">
        <span className="type-meta text-ink-3">Retrieval</span>
        <div className="flex items-center gap-4">
          <span className="type-meta text-[0.5625rem] text-ink-3">
            {index + 1} of {QUERIES.length} · 41 chunks
          </span>
          {last && phase === "answering" && (
            <button
              onClick={replay}
              className="type-meta cursor-pointer text-[0.5625rem] text-ink-3 transition-colors duration-200 hover:text-ink"
            >
              replay
            </button>
          )}
        </div>
      </div>

      <div className="px-6 py-6">
        {/* Question */}
        <div className="flex items-baseline gap-3">
          <span className="type-meta shrink-0 text-[0.5625rem] text-ink-3">
            caller
          </span>
          <p className="text-[1.0625rem] leading-snug text-ink">
            {typed}
            {phase === "asking" && (
              <motion.span
                aria-hidden="true"
                className="ml-0.5 inline-block h-[1.05em] w-[2px] translate-y-[0.16em] bg-ink"
                animate={{ opacity: [1, 1, 0, 0] }}
                transition={{
                  duration: 0.9,
                  repeat: Infinity,
                  times: [0, 0.5, 0.5, 1],
                }}
              />
            )}
          </p>
        </div>

        {/* Scored chunks */}
        <ul className="mt-6 flex flex-col gap-2.5">
          {q.hits.map((h, i) => {
            const shown = phase !== "asking";
            const top = i === 0;
            return (
              <motion.li
                key={`${index}-${h.src}`}
                initial={{ opacity: 0, y: 8 }}
                animate={shown ? { opacity: 1, y: 0 } : { opacity: 0, y: 8 }}
                transition={{ duration: 0.45, ease: EASE, delay: i * 0.11 }}
                className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-4"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-baseline gap-x-2.5">
                    <span
                      className="type-data text-[0.8125rem]"
                      style={{
                        color:
                          top && phase === "answering"
                            ? "var(--color-s1)"
                            : "var(--color-ink-2)",
                      }}
                    >
                      {h.src}
                    </span>
                    <span className="truncate text-[0.8125rem] text-ink-3">
                      {h.span}
                    </span>
                  </div>
                  <div className="mt-1.5 h-[3px] w-full overflow-hidden rounded-full bg-hairline-2">
                    <motion.div
                      className="h-full rounded-full"
                      initial={{ width: "0%" }}
                      animate={{ width: shown ? `${h.score * 100}%` : "0%" }}
                      transition={{
                        duration: 0.8,
                        ease: EASE,
                        delay: i * 0.11,
                      }}
                      style={{
                        background: top
                          ? "linear-gradient(to right, var(--color-s1), var(--color-s3))"
                          : "var(--color-s0)",
                      }}
                    />
                  </div>
                </div>
                <span className="type-data text-[0.8125rem] tabular-nums text-ink-3">
                  {h.score.toFixed(2)}
                </span>
              </motion.li>
            );
          })}
        </ul>

        {/* Answer */}
        <div className="mt-6 min-h-[3.25rem] border-t border-hairline pt-5">
          <AnimatePresence mode="wait">
            {phase === "answering" && (
              <motion.div
                key={`${index}-answer`}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ duration: 0.5, ease: EASE }}
                className="flex items-baseline gap-3"
              >
                <span
                  className="type-meta shrink-0 text-[0.5625rem]"
                  style={{ color: "var(--color-s1)" }}
                >
                  david
                </span>
                <p className="text-[1.0625rem] leading-snug text-ink">
                  {q.answer}
                </p>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}
