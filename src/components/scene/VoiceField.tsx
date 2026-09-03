"use client";

import { useMemo, useRef } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import * as THREE from "three";
import { fragmentShader, vertexShader } from "./voiceField.glsl";
import { blend, scene } from "@/lib/sceneState";

/** Grid density, stepped down on small screens so phones stay at 60fps. */
function gridSize() {
  const w = typeof window === "undefined" ? 1440 : window.innerWidth;
  if (w < 640) return { cols: 208, rows: 88 };
  if (w < 1024) return { cols: 288, rows: 116 };
  return { cols: 384, rows: 152 };
}

/** Distance from camera at which the orb is parked during the demo handoff. */
const ORB_DISTANCE = 13;
/** Largest radius the orb reaches, in its own units — used to fit it in the ring. */
const ORB_MAX_R = 1.62;
/** Fraction of the panel ring the orb is allowed to fill. */
const ORB_FILL = 0.94;

/** Deterministic per-point noise — stable across renders, unlike Math.random. */
const jitterAt = (n: number) => {
  const x = Math.sin(n * 12.9898) * 43758.5453;
  return x - Math.floor(x);
};

const damp = (current: number, target: number, lambda: number, dt: number) =>
  THREE.MathUtils.lerp(current, target, 1 - Math.exp(-lambda * dt));

export default function VoiceField() {
  const material = useRef<THREE.ShaderMaterial>(null);
  const { camera, size } = useThree();

  const smooth = useRef({
    mode: 0,
    heroExit: 0,
    orbIn: 0,
    px: 0,
    py: 0,
    energy: 0,
    turn: 1,
  });
  const clock = useRef(0);
  const dir = useMemo(() => new THREE.Vector3(), []);

  const geometry = useMemo(() => {
    const { cols: COLS, rows: ROWS } = gridSize();
    const count = COLS * ROWS;
    const grid = new Float32Array(count * 2);
    const jitter = new Float32Array(count);
    const sphere = new Float32Array(count * 3);
    const positions = new Float32Array(count * 3); // written by the shader

    // Fibonacci sphere: even coverage with no dense poles, which a lat/lon grid
    // cannot give you.
    const golden = Math.PI * (3 - Math.sqrt(5));

    let i = 0;
    for (let y = 0; y < ROWS; y++) {
      for (let x = 0; x < COLS; x++) {
        // Slight per-row offset breaks up the regular lattice, which otherwise
        // produces moiré at grazing angles.
        grid[i * 2] = (x + (y % 2) * 0.5) / COLS;
        grid[i * 2 + 1] = y / (ROWS - 1);
        jitter[i] = jitterAt(i + 1);

        const yy = 1 - (i / (count - 1)) * 2;
        const r = Math.sqrt(Math.max(0, 1 - yy * yy));
        const theta = golden * i;
        sphere[i * 3] = Math.cos(theta) * r;
        sphere[i * 3 + 1] = yy;
        sphere[i * 3 + 2] = Math.sin(theta) * r;

        i++;
      }
    }

    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    g.setAttribute("aGrid", new THREE.BufferAttribute(grid, 2));
    g.setAttribute("aJitter", new THREE.BufferAttribute(jitter, 1));
    g.setAttribute("aSphere", new THREE.BufferAttribute(sphere, 3));
    g.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 60);
    return g;
  }, []);

  const uniforms = useMemo(
    () => ({
      uTime: { value: 0 },
      uMode: { value: 0 },
      uHeroExit: { value: 0 },
      uOrbIn: { value: 0 },
      uEnergy: { value: 0 },
      uTurn: { value: 1 },
      uPointer: { value: new THREE.Vector2() },
      uSize: { value: 5.4 },
      uFade: { value: 1 },
      uOrbCenter: { value: new THREE.Vector3(0, 0, 0) },
      uOrbScale: { value: 1 },
      uRate: { value: scene.to.rate },
      uF1: { value: scene.to.f1 },
      uF2: { value: scene.to.f2 },
      uF3: { value: scene.to.f3 },
      uGlide: { value: scene.to.glide },
      uBurst: { value: scene.to.burst },
      uBreadth: { value: scene.to.breadth },
    }),
    [],
  );

  useFrame((_, rawDelta) => {
    const m = material.current;
    if (!m) return;

    const dt = Math.min(rawDelta, 0.05);
    const s = smooth.current;
    const u = m.uniforms;

    // Reduced motion: hold one expressive frame rather than animating.
    clock.current += scene.reduced ? 0 : dt;
    u.uTime.value = scene.reduced ? 2.4 : clock.current;

    // Advance the language morph.
    if (scene.morph < 1) scene.morph = Math.min(1, scene.morph + dt * 1.15);
    const eased =
      scene.morph < 0.5
        ? 4 * scene.morph ** 3
        : 1 - (-2 * scene.morph + 2) ** 3 / 2;
    const sig = blend(scene.from, scene.to, eased);
    u.uRate.value = sig.rate;
    u.uF1.value = sig.f1;
    u.uF2.value = sig.f2;
    u.uF3.value = sig.f3;
    u.uGlide.value = sig.glide;
    u.uBurst.value = sig.burst;
    u.uBreadth.value = sig.breadth;

    // The pose only ever changes while the field is hidden, so this can move
    // quickly without anything visibly morphing.
    s.mode = damp(s.mode, scene.mode, 10, dt);
    s.heroExit = damp(s.heroExit, scene.heroExit, 5, dt);
    s.orbIn = damp(s.orbIn, scene.orbIn, 5, dt);
    s.px = damp(s.px, scene.pointerX, 3.5, dt);
    s.py = damp(s.py, scene.pointerY, 3.5, dt);
    // Fast attack, slow release: responsive to a new turn without snapping.
    s.energy = damp(
      s.energy,
      scene.energy,
      scene.energy > s.energy ? 9 : 2.4,
      dt,
    );
    s.turn = damp(s.turn, scene.turn, 2.2, dt);

    u.uMode.value = s.mode;
    u.uHeroExit.value = s.heroExit;
    u.uOrbIn.value = s.orbIn;
    u.uEnergy.value = s.energy;
    u.uTurn.value = s.turn;
    u.uPointer.value.set(s.px, s.py);

    // Place the orb over the panel's ring. Done here rather than by moving the
    // object, so the hero pose is never dragged across the page with it.
    if (scene.anchor) {
      const a = scene.anchor;
      const ndcX = (a.x / size.width) * 2 - 1;
      const ndcY = -((a.y / size.height) * 2 - 1);

      dir
        .set(ndcX, ndcY, 0.5)
        .unproject(camera)
        .sub(camera.position)
        .normalize();
      u.uOrbCenter.value
        .copy(camera.position)
        .addScaledVector(dir, ORB_DISTANCE);

      const fov = ((camera as THREE.PerspectiveCamera).fov * Math.PI) / 180;
      const worldPerPixel =
        (2 * ORB_DISTANCE * Math.tan(fov / 2)) / size.height;
      const wantRadius = a.size * 0.5 * ORB_FILL * worldPerPixel;
      u.uOrbScale.value = wantRadius / ORB_MAX_R;
    }

    // Points must shrink as the orb does, or the ball turns into a blob.
    u.uSize.value = THREE.MathUtils.lerp(5.4, 1.85, s.orbIn);
  });

  return (
    <points geometry={geometry} frustumCulled={false}>
      <shaderMaterial
        ref={material}
        uniforms={uniforms}
        vertexShader={vertexShader}
        fragmentShader={fragmentShader}
        transparent
        depthWrite={false}
        blending={THREE.NormalBlending}
      />
    </points>
  );
}
