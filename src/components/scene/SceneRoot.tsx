"use client";

import { Canvas } from "@react-three/fiber";
import { useEffect } from "react";
import VoiceField from "./VoiceField";
import { scene } from "@/lib/sceneState";

/**
 * One persistent canvas behind the whole page. Sections that should occlude it
 * paint their own background; sections that should reveal it stay transparent.
 */
export default function SceneRoot() {
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const apply = () => {
      scene.reduced = mq.matches;
    };
    apply();
    mq.addEventListener("change", apply);

    const onPointer = (e: PointerEvent) => {
      if (scene.reduced) return;
      scene.pointerX = (e.clientX / window.innerWidth) * 2 - 1;
      scene.pointerY = -((e.clientY / window.innerHeight) * 2 - 1);
    };
    window.addEventListener("pointermove", onPointer, { passive: true });

    return () => {
      mq.removeEventListener("change", apply);
      window.removeEventListener("pointermove", onPointer);
    };
  }, []);

  return (
    <div className="field-in fixed inset-0 z-0" aria-hidden="true">
      {/* Soft points gain nothing from a 3x buffer; the cap keeps phones cool. */}
      <Canvas
        dpr={[1, 1.75]}
        gl={{
          antialias: false,
          alpha: true,
          powerPreference: "high-performance",
        }}
        camera={{ position: [0, 4.6, 13], fov: 46, near: 0.1, far: 140 }}
        onCreated={({ camera }) => camera.lookAt(0, 2.3, -1)}
      >
        <VoiceField />
      </Canvas>

      {/* Keeps the top of the page as clean paper, so the headline never sits
          on top of the field's texture. The field emerges below it. */}
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "linear-gradient(to bottom, #fff 0%, #fff 22%, rgba(255,255,255,0.94) 32%, rgba(255,255,255,0.55) 42%, rgba(255,255,255,0) 52%, rgba(255,255,255,0) 84%, rgba(255,255,255,0.8) 93%, #fff 100%)",
        }}
      />
    </div>
  );
}
