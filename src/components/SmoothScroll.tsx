"use client";

import { useEffect } from "react";
import Lenis from "lenis";
import { scene } from "@/lib/sceneState";

export default function SmoothScroll({
  children,
}: {
  children: React.ReactNode;
}) {
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const lenis = new Lenis({
      duration: 1.05,
      easing: (t) => 1 - Math.pow(1 - t, 3),
      wheelMultiplier: 0.9,
    });

    let frame = 0;
    const raf = (time: number) => {
      lenis.raf(time);
      frame = requestAnimationFrame(raf);
    };
    frame = requestAnimationFrame(raf);

    return () => {
      cancelAnimationFrame(frame);
      lenis.destroy();
    };
  }, []);

  return <>{children}</>;
}

/**
 * Drives the two scroll moments the field responds to, independently:
 * the hero field dispersing as you leave the hero, and the orb gathering once
 * the demo panel is actually on screen. Nothing travels between them.
 */
export function useFieldHandoff(
  anchorRef: React.RefObject<HTMLElement | null>,
  gateRef: React.RefObject<HTMLElement | null>,
) {
  useEffect(() => {
    let frame = 0;
    const clamp01 = (n: number) => Math.max(0, Math.min(1, n));

    // Arms once the hero field has fully dispersed, so returning to the top
    // swaps the pose exactly once rather than on every scroll wobble.
    let armed = false;

    const measure = () => {
      const vh = window.innerHeight;

      // The hero field sinks away over the first three-quarters of a screen.
      const exit = clamp01(window.scrollY / (vh * 0.75));
      scene.heroExit = exit;

      // Alternate terrain and filament on each return to the top. The swap is
      // made at 0.8 rather than at 0, because the field is already invisible by
      // 0.72 — so the pose changes behind your back and you only ever see it
      // reform in its new shape.
      if (exit >= 0.98) armed = true;
      if (armed && exit <= 0.8) {
        scene.mode = scene.mode > 0.5 ? 0 : 1;
        armed = false;
      }

      const el = anchorRef.current;
      if (!el) {
        scene.orbIn = 0;
        frame = requestAnimationFrame(measure);
        return;
      }

      // The orb is always drawn over its slot, wherever that currently is.
      const r = el.getBoundingClientRect();
      scene.anchor = {
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
        size: Math.min(r.width, r.height),
      };

      // But it gathers on the business picker, not on its own slot — so by the
      // time you have scrolled to the panel it is already in its settled form
      // and the only thing left moving is the call itself.
      const gate = gateRef.current ?? el;
      const g = gate.getBoundingClientRect();
      const enter = clamp01((vh * 0.92 - g.top) / (vh * 0.34));

      // Let it go once the slot itself has left the screen behind you.
      const leave = clamp01((r.bottom + vh * 0.12) / (vh * 0.3));

      scene.orbIn = Math.min(enter, leave);

      frame = requestAnimationFrame(measure);
    };
    frame = requestAnimationFrame(measure);

    return () => cancelAnimationFrame(frame);
  }, [anchorRef, gateRef]);
}
