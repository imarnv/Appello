import dynamic from "next/dynamic";
import Nav from "@/components/Nav";
import Hero from "@/components/Hero";
import SmoothScroll from "@/components/SmoothScroll";
import TrySection from "@/components/try/TrySection";
import Grounding from "@/components/sections/Grounding";
import Handover from "@/components/sections/Handover";
import Pace from "@/components/sections/Pace";
import Channels from "@/components/sections/Channels";
import Close from "@/components/sections/Close";

const SceneRoot = dynamic(() => import("@/components/scene/SceneRoot"));

export default function Home() {
  return (
    <SmoothScroll>
      <SceneRoot />
      <Nav />
      <main className="relative z-10 flex-1">
        <Hero />
        <TrySection />
        <Grounding />
        <Handover />
        <Pace />
        <Channels />
        <Close />
      </main>
    </SmoothScroll>
  );
}
