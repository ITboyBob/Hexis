"use client";

import { useEffect, useRef, useState } from "react";
import { mountEngine } from "@/lib/avatar-preview/engine";
import type { Character, Composition } from "@/lib/avatar-preview/engine";

export function Avatar({
  value,
  size,
  generation,
  onReady,
}: {
  value: Composition;
  size: number;
  generation: number;
  onReady?: (character: Character | null) => void;
}) {
  const svg = useRef<SVGSVGElement>(null);
  const character = useRef<Character | null>(null);
  const latest = useRef(value);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let alive = true;
    mountEngine(svg.current!, () => alive).then((instance) => {
      if (!instance) return;
      if (!alive) {
        instance.destroy();
        return;
      }
      character.current = instance;
      instance.setComposition(latest.current);
      onReady?.(instance);
    }).catch((cause: unknown) => {
      if (alive) setError(cause instanceof Error ? cause.message : "角色资源加载失败；请检查网络与静态资源后重试");
    });
    return () => {
      alive = false;
      character.current?.destroy();
      character.current = null;
      onReady?.(null);
    };
  }, [retry, onReady]);

  useEffect(() => {
    latest.current = value;
    character.current?.setComposition(value);
  }, [value]);

  useEffect(() => {
    const instance = character.current;
    if (!instance) return;
    instance.resetVisual();
    instance.setComposition(latest.current);
  }, [generation]);

  return (
    <section aria-label="二维角色">
      <svg
        ref={svg}
        role="img"
        aria-label={`眼形 ${value.eye}，活动 ${value.pose}`}
        style={{ width: size, height: size }}
      />
      {error ? (
        <p role="alert">
          {error}
          <button type="button" onClick={() => { setError(""); setRetry((current) => current + 1); }}>
            重试加载角色
          </button>
        </p>
      ) : null}
    </section>
  );
}
