import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { VoiceAvatar } from "./voice-avatar";

vi.mock("../avatar-preview/avatar", () => ({
  Avatar: ({ value }: { value: { pose: string } }) => <div data-testid="avatar-engine">{value.pose}</div>,
}));

describe("VoiceAvatar", () => {
  it("anchors the character above Talk and keeps stage and emotion bubbles distinct", () => {
    render(<VoiceAvatar
      composition={{ eye: "idle", pose: "listening", playing: false, amplitude: 0, motion_limit_px: 8, reduced_motion: false }}
      bubble="thinking"
      emotionLabel="curious"
      generation={0}
    />);
    expect(screen.getByTestId("voice-avatar")).toBeInTheDocument();
    expect(screen.getByTestId("avatar-engine")).toHaveTextContent("listening");
    expect(screen.getByTestId("voice-stage-bubble")).toHaveTextContent("thinking");
    expect(screen.getByTestId("voice-emotion-bubble")).toHaveTextContent("curious");
  });
});
