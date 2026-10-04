import { EMOTIONS, eyeKey } from "./contracts";
import type { Emotion } from "./contracts";

export type Composition = {
  eye: string;
  pose: "idle" | "listening" | "thinking" | "working";
  playing: boolean;
  amplitude: number;
  motion_limit_px: number;
  reduced_motion: boolean;
};

export type Character = {
  setComposition(value: Composition): void;
  resetVisual(): void;
  stopVisualEffect(): void;
  destroy(): void;
  bounceOnce(): void;
  burstOnce(): void;
};

declare global {
  interface Window {
    GrokCharacter: new (svg: SVGSVGElement, options: Record<string, unknown>) => Character;
    GROK_GEO: { viewBox: { width: number }; Re: number };
    GROK_META: { eyePlaylist: Record<string, number[]> };
  }
}

const names = ["geometry-data", "math", "tables", "pose", "tricks", "fx", "eyes", "character"];
let loading: Promise<void> | undefined;

function validatePlaylists(): void {
  const playlists = window.GROK_META?.eyePlaylist;
  const keys = new Set<Emotion>(EMOTIONS);
  keys.delete("neutral");
  for (const emotion of keys) {
    const playlist = playlists?.[eyeKey(emotion)];
    if (!Array.isArray(playlist) || playlist.length === 0) {
      throw new Error(`角色资源缺少 ${emotion} 眼形；请检查固定资源清单`);
    }
  }
  if (!Array.isArray(playlists?.idle) || playlists.idle.length === 0) {
    throw new Error("角色资源缺少 idle 眼形；请检查固定资源清单");
  }
}

export function loadEngine(): Promise<void> {
  if (!loading) {
    loading = (async () => {
      for (const name of names) {
        await new Promise<void>((resolve, reject) => {
          const script = document.createElement("script");
          script.src = `/avatar-preview/grok/${name}.js`;
          script.onload = () => resolve();
          script.onerror = () => {
            script.remove();
            reject(new Error(`角色资源 ${name} 加载失败；请检查静态资源后重试`));
          };
          document.head.appendChild(script);
        });
      }
      validatePlaylists();
      if (!window.GROK_GEO || typeof window.GrokCharacter !== "function") {
        throw new Error("角色引擎初始化不完整；请检查静态资源后重试");
      }
    })().catch((error: unknown) => {
      loading = undefined;
      throw error;
    });
  }
  return loading;
}

export async function mountEngine(
  svg: SVGSVGElement,
  alive: () => boolean,
): Promise<Character | null> {
  await loadEngine();
  if (!alive()) return null;
  return new window.GrokCharacter(svg, {
    mode: "hold",
    state: "idle",
    shape: "blob",
    followPointer: false,
  });
}
