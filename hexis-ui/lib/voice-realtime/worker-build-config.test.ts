import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("explicit Worker and Talk dev build directory", () => {
  it.each([
    ["worker", ".next/voice-barge-in-unit", ".next/voice-barge-in-unit"],
    ["worker", "", undefined],
    ["talk", ".next/voice-barge-in-unit", ".next/voice-barge-in-unit"],
    ["talk", "", undefined],
    ["", ".next/voice-barge-in-unit", undefined],
    ["other", ".next/voice-barge-in-unit", undefined],
  ])("uses the directory only for an explicitly selected worker or talk (%s, %s)", async (target, directory, expected) => {
    vi.stubEnv("HEXIS_UI_TEST_TARGET", target);
    vi.stubEnv("HEXIS_UI_DIST_DIR", directory);
    vi.resetModules();
    const { default: config } = await import("../../next.config");
    expect(config.distDir).toBe(expected);
    expect(config.output).toBe("standalone");
  });
});
