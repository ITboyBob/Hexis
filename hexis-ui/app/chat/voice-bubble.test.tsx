import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { VoiceBubble, voiceBubbleText } from "./voice-bubble";

describe("VoiceBubble", () => {
  it("renders only the two supported foreground stages", () => {
    expect(voiceBubbleText("thinking")).toBe("thinking");
    expect(voiceBubbleText("recalling")).toBe("recalling");
    for (const value of ["waiting_for_user", "responding", "running", undefined]) {
      expect(voiceBubbleText(value)).toBeUndefined();
    }
  });

  it("keeps hidden for legacy states and exposes the English stage to assistive tech", () => {
    const { rerender } = render(<VoiceBubble stage="waiting_for_user" />);
    expect(screen.queryByTestId("voice-stage-bubble")).not.toBeInTheDocument();
    rerender(<VoiceBubble stage="recalling" />);
    expect(screen.getByRole("generic", { name: "对话阶段 recalling" })).toHaveTextContent("recalling");
  });
});
