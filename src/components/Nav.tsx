"use client";

import { useEffect, useState } from "react";
import { motion } from "motion/react";
import { TOTAL_LANGUAGES } from "@/lib/signatures";

const LINKS = [
  { label: "Try it", href: "#try" },
  { label: "Grounding", href: "#grounding" },
  { label: "Voice", href: "#handover" },
  { label: "Pace", href: "#pace" },
  { label: "Channels", href: "#channels" },
];

/** Four-bar level meter — the only moving thing in the nav. */
function Meter({ active }: { active: boolean }) {
  return (
    <span
      className="flex h-4 items-end gap-[2px]"
      aria-hidden="true"
      style={{ opacity: active ? 1 : 0.55 }}
    >
      {[0.55, 1, 0.7, 0.35].map((h, i) => (
        <motion.span
          key={i}
          className="w-[2px] rounded-full bg-ink"
          initial={{ height: `${h * 60}%` }}
          animate={{
            height: [`${h * 40}%`, `${h * 100}%`, `${h * 55}%`],
          }}
          transition={{
            duration: 1.1 + i * 0.23,
            repeat: Infinity,
            repeatType: "mirror",
            ease: "easeInOut",
            delay: i * 0.08,
          }}
        />
      ))}
    </span>
  );
}

export default function Nav() {
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });

    const wide = window.matchMedia("(min-width: 768px)");
    const close = () => wide.matches && setOpen(false);
    wide.addEventListener("change", close);

    return () => {
      window.removeEventListener("scroll", onScroll);
      wide.removeEventListener("change", close);
    };
  }, []);

  return (
    <header
      className="fixed inset-x-0 top-0 z-50 transition-all duration-500"
      style={{
        backgroundColor: scrolled ? "rgba(255,255,255,0.72)" : "transparent",
        backdropFilter: scrolled ? "saturate(180%) blur(18px)" : "none",
        WebkitBackdropFilter: scrolled ? "saturate(180%) blur(18px)" : "none",
        borderBottom: `1px solid ${scrolled ? "var(--color-hairline)" : "transparent"}`,
      }}
    >
      <nav className="mx-auto flex h-16 max-w-[1240px] items-center gap-8 px-6 lg:px-10">
        <a
          href="#top"
          className="flex items-center gap-2.5 text-ink"
          aria-label="Appello, home"
        >
          <Meter active={scrolled} />
          <span
            className="type-h2 text-[1.35rem] leading-none"
            style={{ letterSpacing: "-0.045em" }}
          >
            appello
          </span>
        </a>

        <ul className="ml-2 hidden items-center gap-7 md:flex">
          {LINKS.map((l) => (
            <li key={l.label}>
              <a
                href={l.href}
                className="text-[0.875rem] text-ink-2 transition-colors duration-200 hover:text-ink"
              >
                {l.label}
              </a>
            </li>
          ))}
        </ul>

        <div className="ml-auto flex items-center gap-4 sm:gap-5">
          <span className="type-meta hidden text-ink-3 lg:block">
            {TOTAL_LANGUAGES} languages
          </span>
          <button
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            aria-controls="nav-menu"
            className="-mr-1 cursor-pointer p-1 text-ink md:hidden"
          >
            <span className="sr-only">{open ? "Close menu" : "Open menu"}</span>
            <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
              <path
                d={open ? "M5 5l10 10M15 5L5 15" : "M3 7h14M3 13h14"}
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                fill="none"
              />
            </svg>
          </button>
          <a
            href="#try"
            className="hidden text-[0.875rem] text-ink-2 transition-colors duration-200 hover:text-ink sm:block"
          >
            Sign in
          </a>
          <a
            href="#try"
            className="group relative inline-flex h-9 items-center rounded-full bg-ink px-4 text-[0.8125rem] font-medium text-white transition-transform duration-200 hover:scale-[1.03] active:scale-[0.98]"
          >
            Talk to an agent
          </a>
        </div>
      </nav>

      {open && (
        <div
          id="nav-menu"
          className="border-t border-hairline bg-paper px-6 pb-5 pt-3 md:hidden"
        >
          <ul className="flex flex-col">
            {LINKS.map((l) => (
              <li key={l.label}>
                <a
                  href={l.href}
                  onClick={() => setOpen(false)}
                  className="block py-2.5 text-[0.9375rem] text-ink-2 transition-colors duration-200 hover:text-ink"
                >
                  {l.label}
                </a>
              </li>
            ))}
            <li>
              <a
                href="#try"
                onClick={() => setOpen(false)}
                className="block py-2.5 text-[0.9375rem] text-ink-2 transition-colors duration-200 hover:text-ink"
              >
                Sign in
              </a>
            </li>
          </ul>
        </div>
      )}
    </header>
  );
}
