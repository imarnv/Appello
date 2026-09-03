"use client";

import { useMemo, useRef, useState } from "react";
import { motion } from "motion/react";
import { VERTICALS, optionKey, type CallOption } from "@/lib/verticals";
import { TOTAL_LANGUAGES, byCode } from "@/lib/signatures";
import { setSignature } from "@/lib/sceneState";
import { useFieldHandoff } from "@/components/SmoothScroll";
import { useCall } from "./useCall";
import Transcript from "./Transcript";

const EASE = [0.16, 1, 0.3, 1] as const;

function formatClock(s: number) {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

export default function TrySection() {
  const [verticalId, setVerticalId] = useState(VERTICALS[0].id);
  const [optKey, setOptKey] = useState(() =>
    optionKey(VERTICALS[0].options[0]),
  );

  const vertical = useMemo(
    () => VERTICALS.find((v) => v.id === verticalId) ?? VERTICALS[0],
    [verticalId],
  );
  // Each agent offers its own set. Deriving the active option from the agent
  // means switching agents can never leave a selection the agent cannot serve.
  const available = vertical.options;
  // One code can appear twice when only the accent differs.
  const distinctLanguages = new Set(available.map((o) => o.code)).size;
  const active = available.find((o) => optionKey(o) === optKey) ?? available[0];
  const lang = byCode(active.code);

  const orbSlot = useRef<HTMLDivElement>(null);
  // The picker is what cues the orb to gather, so it is settled by the time
  // you reach the panel.
  const picker = useRef<HTMLDivElement>(null);
  useFieldHandoff(orbSlot, picker);

  const {
    status,
    mode,
    bridge,
    notice,
    playRecording,
    turns,
    elapsed,
    latency,
    start,
    hangUp,
    reset,
  } = useCall(vertical, active);

  const chooseOption = (o: CallOption) => {
    setOptKey(optionKey(o));
    setSignature(byCode(o.code));
  };

  return (
    <section id="try" className="relative z-10 pb-32 pt-24 md:pt-32">
      <div className="mx-auto w-full max-w-[1240px] px-6 lg:px-10">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-15%" }}
          transition={{ duration: 0.9, ease: EASE }}
        >
          <p className="type-meta text-ink-3">Try it</p>
          <h2 className="type-h2 mt-5 max-w-[20ch] text-[clamp(2.1rem,4.6vw,3.6rem)]">
            Pick a business. Pick a language. Make it ring.
          </h2>
          <p className="mt-5 max-w-[52ch] text-[1.0625rem] leading-[1.62] text-ink-2">
            These are the agents businesses actually deploy first. Change the
            language and watch the voice field change shape with it — every
            language leaves a different mark on a spectrogram.
          </p>
        </motion.div>

        {/* Which business is answering */}
        <div
          ref={picker}
          className="mt-12 flex flex-wrap gap-2"
          role="tablist"
          aria-label="Choose a business"
        >
          {VERTICALS.map((v) => {
            const on = v.id === verticalId;
            return (
              <button
                key={v.id}
                role="tab"
                aria-selected={on}
                onClick={() => {
                  setVerticalId(v.id);
                  const next =
                    v.options.find((o) => optionKey(o) === optKey) ??
                    v.options[0];
                  setSignature(byCode(next.code));
                }}
                className={`cursor-pointer rounded-full border px-4 py-2 text-[0.875rem] transition-all duration-200 ${
                  on
                    ? "border-ink bg-ink text-white"
                    : "border-hairline text-ink-2 hover:border-ink/25 hover:text-ink"
                }`}
              >
                {v.label}
              </button>
            );
          })}
        </div>

        <div className="mt-8 grid overflow-hidden rounded-2xl border border-hairline lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
          {/* ── Call panel. Deliberately unpainted so the voice field, which
                flies in from the hero, shows through the orb slot. ────────── */}
          <div className="flex flex-col justify-between gap-8 overflow-y-auto border-b border-hairline p-7 lg:h-[40rem] lg:border-b-0 lg:border-r">
            <div>
              <div className="flex items-center gap-2">
                <p className="type-meta text-ink-3">{vertical.channel}</p>
                <span
                  className="type-meta text-[0.5625rem]"
                  style={{
                    color:
                      (status === "idle" && bridge === "ready") ||
                      (status !== "idle" && mode === "live")
                        ? "var(--color-s2)"
                        : "var(--color-ink-3)",
                  }}
                >
                  {status === "idle"
                    ? bridge === "ready"
                      ? "· live agent ready"
                      : bridge === "offline"
                        ? "· bridge offline"
                        : "· checking bridge"
                    : mode === "live"
                      ? "· live · your mic"
                      : "· recorded"}
                </span>
              </div>
              <h3 className="type-h2 mt-2 text-[1.5rem]">
                {vertical.business}
              </h3>
              <p className="type-meta mt-1.5 text-ink-3">
                answered by {vertical.persona}
              </p>
              <p className="mt-2 max-w-[30ch] text-[0.875rem] leading-relaxed text-ink-2">
                {vertical.premise}
              </p>
            </div>

            {/* The orb lands here. No background — the fixed canvas is behind. */}
            <div
              ref={orbSlot}
              className="relative mx-auto aspect-square w-full max-w-[15rem]"
              aria-hidden="true"
            >
              <div
                className="absolute inset-0 rounded-full border transition-colors duration-500"
                style={{
                  borderColor:
                    status === "live"
                      ? "color-mix(in srgb, var(--color-s2) 26%, transparent)"
                      : "var(--color-hairline)",
                }}
              />
              {status === "live" && (
                <motion.div
                  className="absolute inset-0 rounded-full border"
                  style={{
                    borderColor:
                      "color-mix(in srgb, var(--color-s3) 30%, transparent)",
                  }}
                  animate={{ scale: [1, 1.16], opacity: [0.7, 0] }}
                  transition={{
                    duration: 2.2,
                    repeat: Infinity,
                    ease: "easeOut",
                  }}
                />
              )}
            </div>

            <div>
              <div className="flex items-center justify-between border-t border-hairline pt-4">
                <span className="type-data text-[0.8125rem] text-ink-2">
                  {status === "idle" && "Ready"}
                  {status === "ringing" && "Ringing…"}
                  {status === "live" && formatClock(elapsed)}
                  {status === "ended" && `Ended · ${formatClock(elapsed)}`}
                </span>
                <span className="type-data text-[0.8125rem] text-ink-3">
                  {latency ? `${latency} ms` : "—"}
                </span>
              </div>

              {notice && (
                <p className="mt-3 text-[0.8125rem] leading-snug text-ink-3">
                  {notice}
                </p>
              )}

              <div className="mt-4 flex gap-2">
                {status === "live" || status === "ringing" ? (
                  <button
                    onClick={hangUp}
                    className="h-11 flex-1 cursor-pointer rounded-full border border-hairline text-[0.9375rem] font-medium text-ink transition-colors duration-200 hover:border-ink/25 hover:bg-hairline-2"
                  >
                    End call
                  </button>
                ) : (
                  <button
                    onClick={status === "ended" ? reset : start}
                    className="h-11 flex-1 cursor-pointer rounded-full bg-ink text-[0.9375rem] font-medium text-white transition-transform duration-200 hover:scale-[1.02] active:scale-[0.98]"
                  >
                    {status === "ended"
                      ? "Call again"
                      : `Call ${vertical.business}`}
                  </button>
                )}
              </div>

              {status === "idle" && bridge !== "ready" && (
                <button
                  onClick={playRecording}
                  className="mt-2 h-11 w-full cursor-pointer rounded-full border border-hairline text-[0.9375rem] text-ink-2 transition-colors duration-200 hover:border-ink/25 hover:text-ink"
                >
                  Play a recorded call instead
                </button>
              )}

              {status === "ended" && mode === "scripted" && (
                <button
                  onClick={playRecording}
                  className="mt-2 h-11 w-full cursor-pointer rounded-full border border-hairline text-[0.9375rem] text-ink-2 transition-colors duration-200 hover:border-ink/25 hover:text-ink"
                >
                  Replay this call
                </button>
              )}
            </div>
          </div>

          {/* ── Transcript ──────────────────────────────────────────────── */}
          <div className="h-[28rem] min-h-0 bg-paper lg:h-[40rem]">
            <Transcript
              turns={turns}
              status={status}
              langCode={active.code}
              business={vertical.business}
              persona={vertical.persona}
            />
          </div>
        </div>

        {/* ── Language picker ─────────────────────────────────────────────── */}
        <div className="mt-10 grid gap-8 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)] lg:gap-px">
          <div>
            <p className="type-meta text-ink-3">Caller&rsquo;s language</p>
            <p className="mt-2 max-w-[32ch] text-[0.875rem] leading-relaxed text-ink-2">
              {available.length > distinctLanguages
                ? `${vertical.persona} speaks English, in whichever accent your callers expect.`
                : distinctLanguages === 1
                  ? `${vertical.persona} is scoped to one language, and speaks its regional variant rather than a flattened standard.`
                  : `${vertical.persona} speaks ${distinctLanguages} of ${TOTAL_LANGUAGES}, each in its regional variant rather than a flattened standard.`}
            </p>
            <p className="type-data mt-4 text-[0.8125rem] text-ink-3">
              {lang.code} · {active.dialect ?? lang.dialect}
            </p>
          </div>

          <div className="flex flex-wrap gap-2">
            {available.map((o) => {
              const sig = byCode(o.code);
              const on = optionKey(o) === optionKey(active);
              return (
                <button
                  key={optionKey(o)}
                  onClick={() => chooseOption(o)}
                  aria-pressed={on}
                  className={`group cursor-pointer rounded-xl border px-3.5 py-2.5 text-left transition-all duration-200 ${
                    on
                      ? "border-ink/20 bg-hairline-2"
                      : "border-hairline hover:border-ink/20"
                  }`}
                >
                  <span className="block text-[0.9375rem] leading-tight text-ink">
                    {sig.native}
                  </span>
                  <span className="type-meta mt-1 block text-[0.5625rem] text-ink-3">
                    {o.dialect ?? sig.dialect}
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        {/* ── What the agent is grounded in ───────────────────────────────── */}
        <div className="mt-14 border-t border-hairline pt-6">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
            <span className="type-meta text-ink-3">
              {vertical.persona} answers from
            </span>
            {vertical.sources.map((src) => (
              <span key={src} className="type-data text-[0.8125rem] text-ink-2">
                {src}
              </span>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
