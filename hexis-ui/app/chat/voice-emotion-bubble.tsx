import type { Emotion } from "@/lib/avatar-preview/contracts";

export function VoiceEmotionBubble({ label }: { label?: Emotion }) {
  if (!label) return null;

  return (
    <div
      aria-label={`情绪标签 ${label}`}
      aria-live="polite"
      className="absolute right-full top-1/2 z-10 mr-2 flex h-[70px] min-w-[136px] -translate-y-1/2 items-center justify-center px-5 text-sm font-semibold text-[var(--foreground)]"
      data-testid="voice-emotion-bubble"
    >
      <svg aria-hidden="true" className="absolute inset-0 size-full" viewBox="0 0 136 70" preserveAspectRatio="none">
        <path
          d="M34 8C43 1 57 2 64 8C73 1 88 4 92 12C106 8 118 16 118 27C131 33 128 49 117 53C113 65 99 68 90 62C81 70 66 68 61 61C49 68 34 64 31 56C17 59 8 49 13 39C2 32 9 19 21 18C23 12 28 9 34 8Z"
          fill="white"
          stroke="var(--outline)"
          strokeWidth="1.5"
        />
      </svg>
      <span className="relative z-10">{label}</span>
    </div>
  );
}
