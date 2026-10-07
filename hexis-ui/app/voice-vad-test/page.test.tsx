import { beforeEach, describe, expect, it, vi } from "vitest";
import VoiceVadTestPage from "./page";
import WorkerHarness from "./worker-harness";

const { notFound } = vi.hoisted(() => ({ notFound: vi.fn(() => { throw new Error("NEXT_NOT_FOUND"); }) }));
vi.mock("next/navigation", () => ({ notFound }));

describe("/voice-vad-test", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("is not available unless the runtime test-harness flag is explicit", () => {
    vi.stubEnv("HEXIS_VAD_TEST_HARNESS", "0");
    expect(() => VoiceVadTestPage()).toThrow("NEXT_NOT_FOUND");
    expect(notFound).toHaveBeenCalledOnce();
  });

  it("renders the local worker harness only when explicitly enabled", () => {
    vi.stubEnv("HEXIS_VAD_TEST_HARNESS", "1");
    const page = VoiceVadTestPage();
    expect(page.type).toBe(WorkerHarness);
  });
});
