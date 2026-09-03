"use client";

import { motion } from "motion/react";
import { TOTAL_LANGUAGES } from "@/lib/signatures";

const EASE = [0.16, 1, 0.3, 1] as const;

const LINKS = [
  { label: "Try it", href: "#try" },
  { label: "Grounding", href: "#grounding" },
  { label: "Voice", href: "#handover" },
  { label: "Channels", href: "#channels" },
];

/** Outline marks, so the row stays ink-on-paper rather than importing brand colour. */
const SOCIAL = [
  {
    label: "LinkedIn",
    path: "M4.5 6.5A1.5 1.5 0 1 1 4.5 3.5a1.5 1.5 0 0 1 0 3ZM3.4 8h2.2v8.6H3.4V8Zm4 0h2.1v1.2h.03c.3-.55 1.03-1.2 2.2-1.2 2.35 0 2.78 1.45 2.78 3.35v5.25h-2.2v-4.65c0-1.11-.02-2.54-1.6-2.54-1.6 0-1.85 1.2-1.85 2.46v4.73H7.4V8Z",
  },
  {
    label: "GitHub",
    path: "M10 2.5a7.5 7.5 0 0 0-2.37 14.62c.37.07.5-.16.5-.36v-1.25c-2.09.45-2.53-1-2.53-1-.34-.87-.83-1.1-.83-1.1-.68-.47.05-.46.05-.46.75.05 1.15.77 1.15.77.67 1.15 1.76.82 2.19.63.07-.49.26-.82.48-1.01-1.67-.19-3.42-.84-3.42-3.72 0-.82.29-1.5.77-2.02-.08-.19-.34-.95.07-1.99 0 0 .63-.2 2.06.77a7.1 7.1 0 0 1 3.75 0c1.43-.97 2.06-.77 2.06-.77.41 1.04.15 1.8.08 1.99.48.52.77 1.2.77 2.02 0 2.89-1.76 3.53-3.43 3.71.27.23.51.69.51 1.4v2.07c0 .2.13.44.51.36A7.5 7.5 0 0 0 10 2.5Z",
  },
  {
    label: "X",
    path: "M13.9 3.5h2.1l-4.6 5.25 5.4 7.75h-4.23l-3.3-4.7-3.8 4.7H3.4l4.9-5.6L3.1 3.5h4.34l3 4.3 3.46-4.3Zm-.74 11.6h1.16L6.9 4.8H5.66l7.5 10.3Z",
  },
];

export default function Close() {
  return (
    <footer className="relative z-10 mt-8 overflow-hidden border-t border-hairline">
      <div className="mx-auto w-full max-w-[1240px] px-6 pt-12 lg:px-10 md:pt-16">
        <motion.div
          initial={{ opacity: 0, y: 18 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-10%" }}
          transition={{ duration: 0.9, ease: EASE }}
        >
          <h2 className="type-display max-w-[20ch] text-[clamp(1.9rem,4.2vw,3.1rem)]">
            <span className="block text-ink">Let&rsquo;s put it</span>
            <span className="block text-ink-3">on a real number.</span>
          </h2>

          <a
            href="mailto:hello@appello.io"
            className="mt-6 inline-block text-[0.9375rem] text-ink-2 transition-colors duration-200 hover:text-ink"
          >
            hello@appello.io
          </a>
        </motion.div>

        <div className="mt-8 flex flex-wrap items-center justify-between gap-5">
          <ul className="flex flex-wrap items-center gap-7">
            {LINKS.map((l) => (
              <li key={l.label}>
                <a
                  href={l.href}
                  className="text-[0.9375rem] text-ink transition-colors duration-200 hover:text-ink-2"
                >
                  {l.label}
                </a>
              </li>
            ))}
          </ul>

          <ul className="flex items-center gap-2.5">
            {SOCIAL.map((s) => (
              <li key={s.label}>
                <a
                  href="#top"
                  aria-label={s.label}
                  className="flex size-8 items-center justify-center rounded-full border border-hairline text-ink-2 transition-colors duration-200 hover:border-ink/25 hover:bg-hairline-2 hover:text-ink"
                >
                  <svg
                    width="20"
                    height="20"
                    viewBox="0 0 20 20"
                    aria-hidden="true"
                  >
                    <path d={s.path} fill="currentColor" />
                  </svg>
                </a>
              </li>
            ))}
          </ul>
        </div>

        <div className="mt-8 flex flex-wrap items-center justify-between gap-4 border-t border-hairline pt-5">
          <span className="text-[0.8125rem] text-ink-3">
            {TOTAL_LANGUAGES} languages · SIP, web and SDK · answers that cite
            their source
          </span>
          <span className="type-data text-[0.75rem] text-ink-3">
            © {new Date().getFullYear()} Appello
          </span>
        </div>
      </div>

      {/* The wordmark as a graphic: oversized, cropped by the page edge, and
          pale enough to sit under the content rather than shout over it. */}
      <div
        aria-hidden="true"
        className="pointer-events-none mt-4 select-none overflow-hidden"
      >
        <motion.span
          initial={{ opacity: 0, y: "18%" }}
          whileInView={{ opacity: 1, y: "0%" }}
          viewport={{ once: true, margin: "-5%" }}
          transition={{ duration: 1.2, ease: EASE }}
          className="type-display block whitespace-nowrap text-center leading-[0.72] text-[19vw]"
          style={{ color: "var(--color-hairline)", marginBottom: "-0.28em" }}
        >
          appello
        </motion.span>
      </div>
    </footer>
  );
}
