import { describe, expect, it, vi } from "vitest";
import { EmotionWindowHost } from "./emotion-window";
import type { Identity } from "./contracts";

const identity: Identity = {
  session_id: "session-a",
  input_turn_id: "turn-a",
  input_order: 4,
  stimulus_revision: 2,
};

describe("单宿主视觉资格裁定", () => {
  it("接受截止前结果并使残留计时器失效", () => {
    let now = 100;
    let timer: (() => void) | undefined;
    const cancel = vi.fn();
    const host = new EmotionWindowHost({
      owner: "local",
      now: () => now,
      schedule: (callback) => {
        timer = callback;
        return 1;
      },
      cancel,
    });

    const opened = host.open(identity, 50);
    now = 149;
    const decision = host.acceptResult(identity);
    timer?.();

    expect(opened).toMatchObject({ status: "open", owner: "local", decision_seq: 0 });
    expect(decision).toMatchObject({ accepted: true, fact: { status: "accepted", decision_seq: 1 } });
    expect(cancel).toHaveBeenCalledWith(1);
    expect(host.current(identity)?.status).toBe("accepted");
  });

  it("截止先裁定后，晚到合法结果仍关闭且不重开", () => {
    let now = 200;
    let timer: (() => void) | undefined;
    const changes: unknown[] = [];
    const host = new EmotionWindowHost({
      owner: "local",
      now: () => now,
      schedule: (callback) => {
        timer = callback;
        return 2;
      },
      cancel: vi.fn(),
      onChange: (changedIdentity, fact) => changes.push({ changedIdentity, fact }),
    });

    host.open(identity, 25);
    now = 225;
    timer?.();
    const late = host.acceptResult(identity);

    expect(changes).toEqual([{ changedIdentity: identity, fact: {
      status: "closed", owner: "local", decision_seq: 1, reason: "model_timeout",
    } }]);
    expect(late).toMatchObject({
      accepted: false,
      fact: { status: "closed", reason: "model_timeout", decision_seq: 1 },
    });
    expect(host.open(identity, 500).status).toBe("closed");
  });

  it("绑定完整身份与所有者，不能通过重复 open 延长期限", () => {
    let now = 10;
    const scheduled: number[] = [];
    const host = new EmotionWindowHost({
      owner: "service",
      now: () => now,
      schedule: (_callback, delay) => {
        scheduled.push(delay);
        return scheduled.length;
      },
      cancel: vi.fn(),
    });
    const first = host.open(identity, 100);
    now = 20;
    const repeated = host.open(identity, 500);

    expect(repeated).toBe(first);
    expect(scheduled).toEqual([100]);
    expect(host.current({ ...identity, input_order: identity.input_order + 1 })).toBeUndefined();
  });

  it("即使调度器尚未触发，单调时钟到期也拒绝接受并裁定关闭", () => {
    let now = 0;
    const host = new EmotionWindowHost({ owner: "local", now: () => now, schedule: vi.fn(() => 3), cancel: vi.fn() });
    host.open(identity, 10);
    now = 10;
    expect(host.acceptResult(identity)).toMatchObject({ accepted: false, fact: { status: "closed", reason: "model_timeout" } });
  });
});
