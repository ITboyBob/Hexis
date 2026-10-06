import { useEffect, useState } from "react";
import type { Composition } from "@/lib/avatar-preview/engine";
import { Avatar } from "../avatar-preview/avatar";
import { VoiceBubble } from "./voice-bubble";

export function VoiceAvatar({
  composition,
  bubble,
  generation,
}: {
  composition: Composition;
  bubble?: "thinking" | "recalling";
  generation: number;
}) {
  const [reducedMotion, setReducedMotion] = useState(false);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return (
    <div
      aria-label="Talk 对话形象"
      className="absolute bottom-[calc(100%+0.5rem)] left-1/2 z-20 flex -translate-x-1/2 flex-col items-center"
      data-testid="voice-avatar"
    >
      {bubble ? <div className="absolute bottom-full left-1/2 z-10 mb-1 -translate-x-1/2"><VoiceBubble stage={bubble} /></div> : null}
      <Avatar value={{ ...composition, reduced_motion: reducedMotion || composition.reduced_motion }} size={160} generation={generation} />
    </div>
  );
}
