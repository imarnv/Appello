import { DEFAULT_SIGNATURE, type Signature } from "./signatures";

/**
 * Mutable bridge between the React tree and the render loop.
 *
 * The 3D field reads this every frame. Writing to it never triggers a React
 * render, which is what keeps scroll and pointer input off the reconciler.
 */
export type SceneState = {
  /** hero pose: 0 terrain, 1 filament — alternates on each return to the top */
  mode: number;
  /** 0 in the hero, 1 once the hero field has scrolled away and dispersed */
  heroExit: number;
  /** 0 before the demo panel is in view, 1 once the orb has gathered */
  orbIn: number;
  /** normalised pointer, -1..1 */
  pointerX: number;
  pointerY: number;
  /** signature we are morphing away from / toward, and how far along */
  from: Signature;
  to: Signature;
  morph: number;
  /** screen-space rect of the demo panel's orb slot, in CSS pixels */
  anchor: { x: number; y: number; size: number } | null;
  /** 0 idle, 1 mid-utterance — driven by the demo transcript */
  energy: number;
  /** who currently holds the floor: -1 caller, +1 agent */
  turn: number;
  /** user has asked for reduced motion */
  reduced: boolean;
};

export const scene: SceneState = {
  mode: 0,
  heroExit: 0,
  orbIn: 0,
  pointerX: 0,
  pointerY: 0,
  from: DEFAULT_SIGNATURE,
  to: DEFAULT_SIGNATURE,
  morph: 1,
  anchor: null,
  energy: 0,
  turn: 1,
  reduced: false,
};

/** Begin a morph toward a new language signature. */
export function setSignature(next: Signature) {
  if (next.code === scene.to.code) return;
  // Freeze the current blend as the new starting point so rapid switches
  // interpolate from what is actually on screen, not from a stale pose.
  scene.from = blend(scene.from, scene.to, scene.morph);
  scene.to = next;
  scene.morph = 0;
}

export function blend(a: Signature, b: Signature, t: number): Signature {
  const m = (x: number, y: number) => x + (y - x) * t;
  return {
    ...b,
    rate: m(a.rate, b.rate),
    f1: m(a.f1, b.f1),
    f2: m(a.f2, b.f2),
    f3: m(a.f3, b.f3),
    glide: m(a.glide, b.glide),
    burst: m(a.burst, b.burst),
    breadth: m(a.breadth, b.breadth),
  };
}
