export type ToolMessageView =
  | {
      kind: "call";
      callId: string;
      toolName: string;
      summary: string;
      agentTurnId?: string;
    }
  | {
      kind: "result";
      callId: string;
      toolName: string;
      summary: string;
      failed: boolean;
      text?: string;
      agentTurnId?: string;
    }
  | {
      kind: "incomplete";
      callId: string | null;
      toolName: string;
      summary: string;
      failed: true;
      agentTurnId?: string;
    };

type JsonObject = Record<string, unknown>;

function asObject(value: unknown): JsonObject | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as JsonObject;
}

function asArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (typeof value !== "string") return [];
  try {
    const decoded: unknown = JSON.parse(value);
    return Array.isArray(decoded) ? decoded : [];
  } catch {
    return [];
  }
}

function textContentForDisplay(value: unknown): string | undefined {
  if (typeof value !== "string" || !value.trim()) return undefined;
  try {
    const decoded: unknown = JSON.parse(value);
    if (decoded && typeof decoded === "object") return undefined;
  } catch {
    // Plain text is kept visible; structured payload text stays out of the body.
  }
  return value;
}

function resultSummary(details: JsonObject | unknown[] | null, failed: boolean): string {
  if (failed) return "Tool execution failed";
  if (Array.isArray(details)) return "Structured list returned";
  if (details === null) return "Text result returned";

  const state = typeof details.state === "string" ? details.state : "";
  if (state === "accepted") return "Accepted";
  if (state === "pending") return "Pending confirmation";
  if (state === "stored") return "Saved";
  if (state === "selection_cancelled") return "Selection cancelled";

  const status = typeof details.status === "string" ? details.status : "";
  if (status === "found") return "Found relevant memories";
  if (status === "no_results") return "No matching memories";
  return "Structured result returned";
}

function incomplete(
  callId: string | null,
  toolName: string,
  metadata: JsonObject | null
): ToolMessageView {
  return {
    kind: "incomplete",
    callId,
    toolName: toolName || "unknown tool",
    summary: "Tool record is incomplete; call pairing could not be verified",
    failed: true,
    ...(typeof metadata?.agent_turn_id === "string"
      ? { agentTurnId: metadata.agent_turn_id }
      : {}),
  };
}

/** Maps DB tool messages into a UI-only model; never mixes them with presentation.blocks. */
export function toolMessageViewFromDbRecord(value: unknown): ToolMessageView | null {
  const record = asObject(value);
  if (!record) return null;
  const metadata = asObject(record.metadata);
  const blocks = asArray(record.content_blocks).map(asObject);

  if (record.role === "assistant") {
    const callBlocks = blocks.filter((block) => block?.type === "toolCall");
    if (callBlocks.length === 0) return null;
    const block = callBlocks[0];
    const callId = typeof block?.id === "string" ? block.id : "";
    const toolName = typeof block?.name === "string" ? block.name : "";
    if (
      callBlocks.length !== 1 ||
      !callId.trim() ||
      !toolName.trim() ||
      asObject(block?.arguments) === null
    ) {
      return incomplete(callId || null, toolName, metadata);
    }
    return {
      kind: "call",
      callId,
      toolName,
      summary: "Tool call",
      ...(typeof metadata?.agent_turn_id === "string"
        ? { agentTurnId: metadata.agent_turn_id }
        : {}),
    };
  }

  if (record.role !== "toolResult") return null;
  const result = asObject(metadata?.tool_result);
  const callId = typeof result?.toolCallId === "string" ? result.toolCallId : "";
  const toolName = typeof result?.toolName === "string" ? result.toolName : "";
  const details = result?.details;
  if (
    !callId.trim() ||
    !toolName.trim() ||
    typeof result?.isError !== "boolean" ||
    (details !== null && !Array.isArray(details) && asObject(details) === null)
  ) {
    return incomplete(callId || null, toolName, metadata);
  }
  const displayText = textContentForDisplay(record.content);
  return {
    kind: "result",
    callId,
    toolName,
    summary: resultSummary(details as JsonObject | unknown[] | null, result.isError),
    failed: result.isError,
    ...(displayText ? { text: displayText } : {}),
    ...(typeof metadata?.agent_turn_id === "string"
      ? { agentTurnId: metadata.agent_turn_id }
      : {}),
  };
}
