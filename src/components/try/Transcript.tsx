"use client";

import { useEffect, useRef } from "react";
import { AnimatePresence, motion } from "motion/react";
import { RTL, type Turn } from "@/lib/verticals";
import type { CallStatus } from "./useCall";

const EASE = [0.16, 1, 0.3, 1] as const;

function Citation({ source, span }: { source: string; span: string }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: EASE, delay: 0.25 }}
      className="mt-2.5 flex flex-wrap items-center gap-x-2 gap-y-1"
    >
      <span
        className="type-meta text-[0.625rem] text-s2"
        style={{ color: "var(--color-s2)" }}
      >
        grounded in
      </span>
      <span
        className="type-data rounded-[4px] px-1.5 py-0.5 text-[0.75rem]"
        style={{
          background: "color-mix(in srgb, var(--color-s2) 9%, transparent)",
          color: "var(--color-s1)",
        }}
      >
        {source}
      </span>
      <span className="text-[0.75rem] leading-snug text-ink-3">{span}</span>
    </motion.div>
  );
}

export default function Transcript({
  turns,
  status,
  langCode,
  business,
  persona,
}: {
  turns: Turn[];
  status: CallStatus;
  langCode: string;
  business: string;
  persona: string;
}) {
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [turns.length]);

  const rtl = RTL.has(langCode);
  const glossed = langCode !== "en-IN";

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-baseline justify-between border-b border-hairline px-6 py-4">
        <span className="type-meta text-ink-3">Transcript</span>
        {glossed && (
          <span className="type-meta text-[0.625rem] text-ink-3">
            opening line in native · rest glossed to English
          </span>
        )}
      </div>

      <div
        ref={scroller}
        className="min-h-0 flex-1 space-y-6 overflow-y-auto px-6 py-6"
      >
        {turns.length === 0 && (
          <p className="max-w-[34ch] text-[0.9375rem] leading-relaxed text-ink-3">
            {status === "ringing"
              ? `Connecting to ${business}…`
              : "Start the call and the conversation appears here, with the document behind every answer."}
          </p>
        )}

        <AnimatePresence initial={false}>
          {turns.map((t, i) => (
            <motion.div
              key={i}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.55, ease: EASE }}
              className={t.who === "caller" ? "pl-8 md:pl-14" : ""}
            >
              <div className="flex items-center gap-2">
                <span
                  className="type-meta text-[0.625rem]"
                  style={{
                    color:
                      t.who === "agent"
                        ? "var(--color-s1)"
                        : "var(--color-ink-3)",
                  }}
                >
                  {t.who === "agent" ? persona : "Caller"}
                </span>
                {t.latency && t.who === "agent" && (
                  <span className="type-data text-[0.6875rem] text-ink-3">
                    {t.latency} ms
                  </span>
                )}
              </div>

              <p
                dir={i === 0 && rtl ? "rtl" : "ltr"}
                className={`mt-1.5 text-[0.9375rem] leading-[1.6] ${
                  t.who === "agent" ? "text-ink" : "text-ink-2"
                }`}
              >
                {t.text}
              </p>

              {t.cite && <Citation {...t.cite} />}

              {t.action && (
                <motion.div
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ delay: 0.3, duration: 0.5 }}
                  className="mt-2.5 inline-flex items-center gap-2 rounded-full border border-hairline px-2.5 py-1"
                >
                  <span
                    className="size-1.5 rounded-full"
                    style={{ background: "var(--color-s3)" }}
                  />
                  <span className="type-data text-[0.75rem] text-ink-2">
                    {t.action}
                  </span>
                </motion.div>
              )}
            </motion.div>
          ))}
        </AnimatePresence>

        {status === "ended" && turns.length > 0 && (
          <motion.p
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="type-meta border-t border-hairline pt-5 text-ink-3"
          >
            Call ended · {turns.filter((t) => t.cite).length} answers cited
          </motion.p>
        )}
      </div>
    </div>
  );
}
