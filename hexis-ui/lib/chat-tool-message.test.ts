import { describe, expect, it } from "vitest";

import { toolMessageViewFromDbRecord } from "./chat-tool-message";

describe("toolMessageViewFromDbRecord", () => {
  it("maps a toolCall without exposing raw arguments", () => {
    expect(toolMessageViewFromDbRecord({
      role: "assistant",
      content: "",
      content_blocks: [{
        type: "toolCall",
        id: "call-submit",
        name: "submit",
        arguments: { task_description: "private task text" },
      }],
      metadata: { agent_turn_id: "turn-1" },
    })).toEqual({
      kind: "call",
      callId: "call-submit",
      toolName: "submit",
      summary: "Tool call",
      agentTurnId: "turn-1",
    });
  });

  it("uses the native M5 receipt state without claiming accepted work is complete", () => {
    const view = toolMessageViewFromDbRecord({
      role: "toolResult",
      content: JSON.stringify({
        contract_version: "m5-tool-v1",
        tool_name: "submit",
        call_id: "call-submit",
        operation_id: "operation-1",
        ok: true,
        state: "accepted",
        data: { task_id: "task-1" },
        error: null,
      }),
      content_blocks: [{ type: "text", text: "" }],
      metadata: {
        agent_turn_id: "turn-1",
        tool_result: {
          toolCallId: "call-submit",
          toolName: "submit",
          isError: false,
          details: {
            contract_version: "m5-tool-v1",
            tool_name: "submit",
            call_id: "call-submit",
            operation_id: "operation-1",
            ok: true,
            state: "accepted",
            data: { task_id: "task-1" },
            error: null,
          },
        },
      },
    });

    expect(view).toEqual({
      kind: "result",
      callId: "call-submit",
      toolName: "submit",
      summary: "Accepted",
      failed: false,
      agentTurnId: "turn-1",
    });
    expect(JSON.stringify(view)).not.toContain("task-1");
    expect(JSON.stringify(view)).not.toContain("operation-1");
  });

  it("shows errors and natural text while keeping details out of the body", () => {
    const view = toolMessageViewFromDbRecord({
      role: "toolResult",
      content: "Could not read that file.",
      content_blocks: [{ type: "text", text: "Could not read that file." }],
      metadata: {
        tool_result: {
          toolCallId: "call-read",
          toolName: "read_file",
          isError: true,
          details: { code: "FILE_NOT_FOUND", path: "/private/path" },
        },
      },
    });

    expect(view).toEqual({
      kind: "result",
      callId: "call-read",
      toolName: "read_file",
      summary: "Tool execution failed",
      failed: true,
      text: "Could not read that file.",
    });
    expect(JSON.stringify(view)).not.toContain("/private/path");
  });

  it("keeps malformed historical tool rows visible as incomplete", () => {
    expect(toolMessageViewFromDbRecord({
      role: "toolResult",
      content: "",
      content_blocks: [],
      metadata: { agent_turn_id: "turn-2" },
    })).toEqual({
      kind: "incomplete",
      callId: null,
      toolName: "unknown tool",
      summary: "Tool record is incomplete; call pairing could not be verified",
      failed: true,
      agentTurnId: "turn-2",
    });
  });
});
