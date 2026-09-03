"use client";

import { motion } from "motion/react";

const EASE = [0.16, 1, 0.3, 1] as const;

/** Shared heading block, so every section below the hero is set the same way. */
export function SectionHead({
  eyebrow,
  title,
  lede,
  align = "left",
}: {
  eyebrow: string;
  title: React.ReactNode;
  lede?: React.ReactNode;
  align?: "left" | "center";
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-12%" }}
      transition={{ duration: 0.9, ease: EASE }}
      className={align === "center" ? "mx-auto max-w-[46rem] text-center" : ""}
    >
      <p className="type-meta text-ink-3">{eyebrow}</p>
      <h2 className="type-h2 mt-4 max-w-[24ch] text-[clamp(1.9rem,3.8vw,2.9rem)]">
        {title}
      </h2>
      {lede && (
        <p
          className={`mt-4 text-[1rem] leading-[1.6] text-ink-2 ${
            align === "center" ? "mx-auto max-w-[52ch]" : "max-w-[52ch]"
          }`}
        >
          {lede}
        </p>
      )}
    </motion.div>
  );
}

export function Section({
  id,
  children,
  className = "",
}: {
  id?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section id={id} className={`relative z-10 py-16 md:py-24 ${className}`}>
      <div className="mx-auto w-full max-w-[1240px] px-6 lg:px-10">
        {children}
      </div>
    </section>
  );
}

/** Small numeric readout used across the feature sections. */
export function Readout({
  value,
  label,
  tone = "ink",
}: {
  value: string;
  label: string;
  tone?: "ink" | "spectral";
}) {
  return (
    <div>
      <p
        className="type-data text-[1.375rem]"
        style={{ color: tone === "spectral" ? "var(--color-s2)" : undefined }}
      >
        {value}
      </p>
      <p className="mt-1 text-[0.8125rem] leading-snug text-ink-3">{label}</p>
    </div>
  );
}
