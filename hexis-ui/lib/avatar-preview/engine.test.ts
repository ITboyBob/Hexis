import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Character } from "./engine";

const names = [
  "geometry-data", "math", "tables", "pose", "tricks", "fx", "eyes", "character",
];
const assetRoot = resolve(process.cwd(), "public/avatar-preview/grok");
type TestDom = {
  window: Window & {
    eval(source: string): unknown;
    close(): void;
    GROK_TABLES: { EYE_PLAYLIST: Record<string, number[]> };
  };
};
const JSDOM = (createRequire(import.meta.url)("jsdom") as {
  JSDOM: new (html: string, options: { runScripts: "outside-only" }) => TestDom;
}).JSDOM;
type InspectableCharacter = Character & {
  state: string;
  composition: { eye: string; pose: string; reduced_motion: boolean } | null;
  eyeScale: { t: number };
  blink: { t: number };
  eyeTo: number;
  blinkQueue: unknown[];
  _destroyed: boolean;
  eyeAt: number;
  eyeCtx: object;
  humDots: { t: number };
  overlay: { t: number };
  extras: { turn: number | null };
  playbackY: { x: number; t: number; v: number };
  reduceMotion: boolean;
  setState(name: string): void;
  _morphEyes(index: number, stiffness?: number): void;
  resetVisual(): void;
  stopVisualEffect(): void;
  bounceOnce(): void;
  burstOnce(): void;
  _tickComposed(now: number): void;
};

async function loadScripts(scripts: HTMLScriptElement[], start = 0) {
  let delivered = start;
  while (delivered < names.length + start) {
    await Promise.resolve();
    const script = scripts[delivered];
    if (script) {
      const name = names[delivered - start];
      if (name === "geometry-data") {
        Object.defineProperty(window, "GROK_GEO", {
          configurable: true,
          value: { viewBox: { width: 259 }, Re: 114 },
        });
      }
      if (name === "tables") {
        Object.defineProperty(window, "GROK_META", {
          configurable: true,
          value: {
            eyePlaylist: Object.fromEntries(
              ["idle", "happy", "excited", "curious", "surprised", "confused", "suspicious", "angry", "sad", "scared", "proud", "shy", "bored", "playful"]
                .map((key) => [key, [0]]),
            ),
          },
        });
      }
      if (name === "character" && typeof window.GrokCharacter !== "function") {
        Object.defineProperty(window, "GrokCharacter", {
          configurable: true,
          value: vi.fn(),
        });
      }
      delivered += 1;
      script.onload?.(new Event("load"));
    }
  }
}

describe("Avatar engine resources and loader", () => {
  beforeEach(() => {
    vi.resetModules();
    document.head.replaceChildren();
  });

  it("ships the eight fixed resources and a nonempty playlist for every emotion key", async () => {
    const keys = ["idle", "happy", "excited", "curious", "surprised", "confused", "suspicious", "angry", "sad", "scared", "proud", "shy", "bored", "playful"];
    for (const name of names) {
      expect(readFileSync(resolve(assetRoot, `${name}.js`), "utf8").length).toBeGreaterThan(100);
    }
    const source = readFileSync(resolve(assetRoot, "tables.js"), "utf8");
    for (const key of keys) {
      expect(source).toMatch(new RegExp(`(?:^|[\\s,{])${key}\\s*:\\s*\\[[^\\]]+\\]`));
    }
    const manifest = JSON.parse(readFileSync(resolve(assetRoot, "upstream.json"), "utf8"));
    expect(manifest.commit).toBe("647e9bd7c60290c42a738fad586589b3f36a4680");
  });

  it("loads scripts once in dependency order and reuses the in-flight promise", async () => {
    const scripts: HTMLScriptElement[] = [];
    const append = document.head.appendChild.bind(document.head);
    vi.spyOn(document.head, "appendChild").mockImplementation((node) => {
      scripts.push(node as HTMLScriptElement);
      return append(node);
    });
    const engine = await import("./engine");
    const first = engine.loadEngine();
    const second = engine.loadEngine();
    expect(first).toBe(second);
    const loaded = loadScripts(scripts);
    await Promise.all([first, second, loaded]);
    expect(scripts.map((script) => new URL(script.src).pathname)).toEqual(
      names.map((name) => `/avatar-preview/grok/${name}.js`),
    );
  });

  it("removes a failed script and allows a clean retry", async () => {
    const scripts: HTMLScriptElement[] = [];
    const append = document.head.appendChild.bind(document.head);
    vi.spyOn(document.head, "appendChild").mockImplementation((node) => {
      scripts.push(node as HTMLScriptElement);
      return append(node);
    });
    const engine = await import("./engine");
    const failed = engine.loadEngine();
    expect(scripts[0].isConnected).toBe(true);
    scripts[0].onerror?.(new Event("error"));
    await expect(failed).rejects.toThrow("geometry-data");
    expect(scripts[0].isConnected).toBe(false);

    const retry = engine.loadEngine();
    const loaded = loadScripts(scripts, 1);
    await Promise.all([retry, loaded]);
    expect(scripts).toHaveLength(names.length + 1);
  });

  it("does not create a character when the host unmounts during loading", async () => {
    const scripts: HTMLScriptElement[] = [];
    vi.spyOn(document.head, "appendChild").mockImplementation((node) => {
      scripts.push(node as HTMLScriptElement);
      return node;
    });
    const constructor = vi.fn();
    Object.defineProperty(window, "GrokCharacter", { configurable: true, value: constructor });
    const engine = await import("./engine");
    let alive = true;
    const mounted = engine.mountEngine(document.createElementNS("http://www.w3.org/2000/svg", "svg"), () => alive);
    alive = false;
    const loaded = loadScripts(scripts);
    expect(await mounted).toBeNull();
    await loaded;
    expect(constructor).not.toHaveBeenCalled();
  });

  it("creates exactly one held, pointer-independent character for a live host", async () => {
    const scripts: HTMLScriptElement[] = [];
    vi.spyOn(document.head, "appendChild").mockImplementation((node) => {
      scripts.push(node as HTMLScriptElement);
      return node;
    });
    const instance = { destroy: vi.fn() };
    const constructor = vi.fn((...args: [SVGSVGElement, Record<string, unknown>]) => {
      void args;
      return instance;
    });
    Object.defineProperty(window, "GrokCharacter", { configurable: true, value: constructor });
    const engine = await import("./engine");
    const mounted = engine.mountEngine(document.createElementNS("http://www.w3.org/2000/svg", "svg"), () => true);
    const loaded = loadScripts(scripts);
    expect(await mounted).toBe(instance);
    await loaded;
    expect(constructor).toHaveBeenCalledTimes(1);
    expect(constructor.mock.calls[0][1]).toMatchObject({ mode: "hold", followPointer: false });
  });

  it("supports controlled composition without changing the legacy setState path", () => {
    const dom = new JSDOM("<!doctype html><body></body>", { runScripts: "outside-only" });
    const view = dom.window;
    const callbacks: FrameRequestCallback[] = [];
    const raf = vi.fn((callback: FrameRequestCallback) => {
      callbacks.push(callback);
      return callbacks.length;
    });
    const cancelRaf = vi.fn();
    view.requestAnimationFrame = raf;
    view.cancelAnimationFrame = cancelRaf;
    for (const name of names) {
      const source = readFileSync(resolve(assetRoot, `${name}.js`), "utf8");
      view.eval(source);
    }
    expect(view.GrokCharacter.prototype.setComposition).toBeTypeOf("function");
    const svg = view.document.createElementNS("http://www.w3.org/2000/svg", "svg");
    view.document.body.append(svg);
    const character = new view.GrokCharacter(svg, {
      mode: "hold", state: "idle", followPointer: false, reduceMotion: false,
    }) as InspectableCharacter;
    expect(typeof character.setComposition).toBe("function");
    expect(typeof character.stopVisualEffect).toBe("function");
    expect(typeof character.resetVisual).toBe("function");

    character.setState("happy");
    expect(character.state).toBe("happy");
    character.setComposition({
      eye: "happy", pose: "listening", playing: true, amplitude: 1,
      motion_limit_px: 12, reduced_motion: false,
    });
    const eyeAt = character.eyeAt;
    const eyeContext = character.eyeCtx;
    character.setComposition({
      eye: "happy", pose: "thinking", playing: true, amplitude: 0.5,
      motion_limit_px: 12, reduced_motion: false,
    });
    expect(character.eyeAt).toBe(eyeAt);
    expect(character.eyeCtx).toBe(eyeContext);
    expect(character.composition).toMatchObject({ pose: "thinking" });
    character._tickComposed(performance.now() + 16);
    expect(character.humDots.t).toBe(0);
    expect(character.overlay.t).toBe(0);
    expect(character.extras.turn).toBeNull();

    character.playbackY = { x: 5, t: 7, v: 9 };
    character.setComposition({
      eye: "happy", pose: "thinking", playing: false, amplitude: 1,
      motion_limit_px: 12, reduced_motion: false,
    });
    expect(character.playbackY).toEqual({ x: 0, t: 0, v: 0 });
    character.setComposition({
      eye: "happy", pose: "thinking", playing: true, amplitude: 1,
      motion_limit_px: 12, reduced_motion: true,
    });
    expect(character.reduceMotion).toBe(true);
    expect(character.playbackY).toEqual({ x: 0, t: 0, v: 0 });
    character.setComposition({
      eye: "sad", pose: "thinking", playing: false, amplitude: 0,
      motion_limit_px: 12, reduced_motion: true,
    });
    character._tickComposed(performance.now() + 16);
    expect(character.eyeScale.t).toBe(0.97);
    expect(character.blink.t).toBe(0.7);

    const representativeEyes: Record<string, number> = {
      idle: 0, happy: 2, excited: 17, playful: 11, curious: 15,
      surprised: 3, scared: 21, confused: 14, suspicious: 23,
      angry: 7, sad: 13, bored: 4, proud: 8, shy: 24,
    };
    const morph = vi.spyOn(character, "_morphEyes");
    for (const [eye, index] of Object.entries(representativeEyes)) {
      expect(view.GROK_TABLES.EYE_PLAYLIST[eye]).toContain(index);
      character.setComposition({
        eye, pose: "thinking", playing: false, amplitude: 0,
        motion_limit_px: 12, reduced_motion: true,
      });
      expect(character.eyeTo).toBe(index);
      character._tickComposed(performance.now() + 16);
      if (eye === "surprised") {
        const stableScale = character.eyeScale.t;
        character._tickComposed(performance.now() + 5000);
        expect(character.eyeScale.t).toBe(stableScale);
      }
    }
    const morphCount = morph.mock.calls.length;
    character.setComposition({
      eye: "shy", pose: "thinking", playing: false, amplitude: 0,
      motion_limit_px: 12, reduced_motion: true,
    });
    character._tickComposed(performance.now() + 16);
    character._tickComposed(performance.now() + 32);
    expect(morph).toHaveBeenCalledTimes(morphCount);
    character.setComposition({
      eye: "happy", pose: "thinking", playing: false, amplitude: 0,
      motion_limit_px: 12, reduced_motion: false,
    });
    expect(character.reduceMotion).toBe(false);
    const queuedFrames = callbacks.length;
    character.destroy();
    character.destroy();
    expect(character._destroyed).toBe(true);
    expect(character.composition).toBeNull();
    expect(character.blinkQueue).toEqual([]);
    expect(cancelRaf).toHaveBeenCalledTimes(1);
    callbacks.at(-1)?.(performance.now() + 64);
    character.resetVisual();
    character.stopVisualEffect();
    character.bounceOnce();
    character.burstOnce();
    character.setComposition({
      eye: "happy", pose: "idle", playing: false, amplitude: 0,
      motion_limit_px: 12, reduced_motion: true,
    });
    expect(callbacks).toHaveLength(queuedFrames);
    expect(character.composition).toBeNull();
    view.document.body.replaceChildren();
    dom.window.close();
  });
});
