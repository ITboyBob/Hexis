import { useEffect, useState } from "react";
import type { Composition } from "@/lib/avatar-preview/engine";
import type { Emotion } from "@/lib/avatar-preview/contracts";
import { Avatar } from "../avatar-preview/avatar";
import { VoiceEmotionBubble } from "./voice-emotion-bubble";
import { VoiceBubble } from "./voice-bubble";

export function VoiceAvatar({
  composition,
  bubble,
  emotionLabel,
  generation,
  bottomOffsetPx = 8,
  horizontalOffsetPx = 96,
}: {
  composition: Composition;
  bubble?: "thinking" | "recalling";
  emotionLabel?: Emotion;
  generation: number;
  bottomOffsetPx?: number;
  horizontalOffsetPx?: number;
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
      className="absolute left-1/2 z-20 flex -translate-x-1/2 flex-col items-center"
      style={{
        bottom: `calc(100% + ${bottomOffsetPx}px)`,
        left: `calc(50% + ${horizontalOffsetPx}px)`,
      }}
      data-testid="voice-avatar"
    >
      <VoiceEmotionBubble label={emotionLabel} />
      {bubble ? <div className="absolute bottom-full left-1/2 z-10 mb-1 -translate-x-1/2"><VoiceBubble stage={bubble} /></div> : null}
      <Avatar value={{ ...composition, reduced_motion: reducedMotion || composition.reduced_motion }} size={160} generation={generation} />
    </div>
  );
}
