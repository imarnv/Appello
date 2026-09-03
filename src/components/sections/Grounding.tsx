"use client";

import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Section, SectionHead } from "./Section";
import Retrieval from "./Retrieval";

const EASE = [0.16, 1, 0.3, 1] as const;

/**
 * How a document becomes an answer. The four stages are the real pipeline, and
 * each one is shown with the artefact it actually produces — a file, chunks,
 * vectors, a cited sentence — rather than an icon standing in for it.
 */
const STAGES = [
  {
    id: "ingest",
    label: "Ingest",
    caption: "Point it at what you already have.",
    detail:
      "PDFs, spreadsheets, a website, a database view. No schema, no rewriting into some knowledge-base format.",
    artefact: [
      "allergen-matrix.xlsx",
      "menu-winter.pdf",
      "reservations-policy.md",
      "hours.json",
    ],
  },
  {
    id: "chunk",
    label: "Chunk",
    caption: "Split on meaning, not on length.",
    detail:
      "A clause that answers a question stays whole. Splitting every 500 characters is what makes retrieval return halves of sentences.",
    artefact: [
      "peanut · 6 of 41 dishes · substitution available",
      "terrace seats 6–8 · released at 45-minute intervals",
      "Dinner 7–11 PM · last seating 10:15",
    ],
  },
  {
    id: "embed",
    label: "Embed",
    caption: "Every chunk gets a position.",
    detail:
      "Chunks that mean similar things land near each other, so a caller asking about nut allergies finds the peanut row without ever using the word.",
    artefact: ["1,024 dimensions", "41 chunks", "cosine similarity"],
  },
  {
    id: "cite",
    label: "Cite",
    caption: "The answer carries its source.",
    detail:
      "Every factual sentence the agent speaks is traceable to the chunk it came from. If the chunk is wrong, you can fix the document.",
    artefact: [
      "“Six of the winter dishes are finished in peanut oil.”",
      "← allergen-matrix.xlsx · row 12",
    ],
  },
] as const;

export default function Grounding() {
  const [stage, setStage] = useState(0);
  const current = STAGES[stage];

  return (
    <Section id="grounding">
      <SectionHead
        eyebrow="Grounding"
        title="An agent that can cite its source."
        lede="A voice agent that invents a policy is worse than no agent at all. Appello answers from your documents and shows which one it read — so a wrong answer is a document you can fix, not a black box you have to trust."
      />

      <motion.div
        initial={{ opacity: 0, y: 16 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: "-10%" }}
        transition={{ duration: 0.8, ease: EASE }}
        className="mt-10"
      >
        <Retrieval />
      </motion.div>

      {/* Pipeline */}
      <div className="mt-10 grid gap-8 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
        <div>
          <ol className="flex flex-col">
            {STAGES.map((s, i) => {
              const on = i === stage;
              return (
                <li key={s.id}>
                  <button
                    onClick={() => setStage(i)}
                    aria-current={on}
                    className="group flex w-full items-baseline gap-4 border-b border-hairline py-3.5 text-left"
                  >
                    <span
                      className="type-data text-[0.8125rem] transition-colors duration-200"
                      style={{
                        color: on ? "var(--color-s2)" : "var(--color-ink-3)",
                      }}
                    >
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <span className="flex-1">
                      <span
                        className={`block text-[1.0625rem] transition-colors duration-200 ${
                          on ? "text-ink" : "text-ink-2 group-hover:text-ink"
                        }`}
                      >
                        {s.label}
                      </span>
                      <span className="mt-0.5 block text-[0.875rem] leading-snug text-ink-3">
                        {s.caption}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>
        </div>

        <div className="min-h-[12rem]">
          <AnimatePresence mode="wait">
            <motion.div
              key={current.id}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.45, ease: EASE }}
            >
              <p className="max-w-[46ch] text-[1.0625rem] leading-[1.6] text-ink-2">
                {current.detail}
              </p>

              <div className="mt-7 flex flex-wrap gap-2">
                {current.artefact.map((a, i) => (
                  <motion.span
                    key={a}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{
                      duration: 0.5,
                      ease: EASE,
                      delay: 0.08 + i * 0.06,
                    }}
                    className="type-data rounded-lg border border-hairline px-3 py-2 text-[0.8125rem] text-ink-2"
                    style={
                      current.id === "cite" && i === 1
                        ? {
                            color: "var(--color-s1)",
                            background:
                              "color-mix(in srgb, var(--color-s2) 8%, transparent)",
                            borderColor:
                              "color-mix(in srgb, var(--color-s2) 22%, transparent)",
                          }
                        : undefined
                    }
                  >
                    {a}
                  </motion.span>
                ))}
              </div>
            </motion.div>
          </AnimatePresence>
        </div>
      </div>
    </Section>
  );
}
