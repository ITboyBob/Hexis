const visibleStages = new Set(["thinking", "recalling"]);

export function voiceBubbleText(stage: string | undefined): "thinking" | "recalling" | undefined {
  return stage && visibleStages.has(stage) ? stage as "thinking" | "recalling" : undefined;
}

export function VoiceBubble({ stage }: { stage?: string }) {
  const text = voiceBubbleText(stage);
  if (!text) return null;
  return (
    <div
      aria-label={`对话阶段 ${text}`}
      aria-live="polite"
      className="rounded-full border border-[var(--outline)] bg-white/95 px-3 py-1 text-xs font-medium text-[var(--ink-soft)] shadow-md"
      data-testid="voice-stage-bubble"
    >
      {text}
    </div>
  );
}
