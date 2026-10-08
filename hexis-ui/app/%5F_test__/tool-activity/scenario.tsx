"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

type ToolLog = {
  id: string;
  category: "tool";
  title: string;
  detail: string;
  raw: Record<string, unknown>;
  ts: number;
  sessionId: string;
};

function parseLogEvents(sse: string, sessionId: string): ToolLog[] {
  const output: ToolLog[] = [];
  let completed = false;
  for (const part of sse.replace(/\r\n/g, "\n").split("\n\n")) {
    if (!part.trim()) continue;
    const lines = part.split("\n");
    const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
    const line = lines.find((item) => item.startsWith("data:"));
    if (!line) continue;
    const raw = JSON.parse(line.slice(5).trim()) as Record<string, unknown>;
    if (event === "error" || event === "failed") {
      throw new Error(typeof raw.message === "string" ? raw.message : "The production Chat endpoint failed.");
    }
    if (event === "done") completed = true;
    if (event === "log" && (raw.kind === "tool_call" || raw.kind === "tool_result")) {
      output.push({
        id: typeof raw.id === "string" ? raw.id : crypto.randomUUID(),
        category: "tool",
        title: typeof raw.title === "string" ? raw.title : "Tool",
        detail: typeof raw.detail === "string" ? raw.detail : "",
        raw,
        ts: Date.now(),
        sessionId,
      });
    }
  }
  if (!completed) throw new Error("The production Chat endpoint ended without a completion event.");
  if (!output.some((row) => row.raw.kind === "tool_call") || !output.some((row) => row.raw.kind === "tool_result")) {
    throw new Error("The production Chat endpoint did not emit the scripted tool request and result.");
  }
  return output;
}

export default function ControlledToolActivityScenario() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");

  const createSession = async () => {
    const sessionResponse = await fetch("/api/chat/session", { method: "POST", cache: "no-store" });
    const session = await sessionResponse.json();
    if (!sessionResponse.ok || typeof session.session_id !== "string") {
      throw new Error(session.error || `Session creation failed (${sessionResponse.status}).`);
    }
    sessionStorage.setItem("hexis-chat-session-id", session.session_id);
    return session.session_id as string;
  };

  const runRecall = async () => {
    setBusy(true);
    setStatus("Preparing the local scripted provider and owned chat session…");
    try {
      const sessionId = await createSession();
      const scriptResponse = await fetch("/api/__test__/tool-activity/script", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actions: [
            { tool_call: { id: "bob108-controlled-recall", name: "recall", arguments: { query: "Hexis" } } },
            { content: "受控 Provider 已返回；工具调用由当前 Hexis AgentLoop 执行。" },
          ],
        }),
        cache: "no-store",
      });
      if (!scriptResponse.ok) throw new Error(`Scripted provider setup failed (${scriptResponse.status}).`);

      const chatResponse = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: "请使用记忆召回工具查询 Hexis。",
          session_id: sessionId,
          client_message_id: crypto.randomUUID(),
        }),
      });
      if (!chatResponse.ok) throw new Error(`Chat request failed (${chatResponse.status}).`);
      const body = await chatResponse.text();
      const activity = parseLogEvents(body, sessionId);
      sessionStorage.setItem("hexis-chat-activity-events", JSON.stringify(activity));
      setStatus("The production Chat endpoint completed the scripted recall. Opening its Activity inspector…");
      router.push("/chat");
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "The controlled tool scenario failed.");
      setBusy(false);
    }
  };

  const runVoiceRecall = async () => {
    setBusy(true);
    setStatus("Creating an owned chat session and running the real read-only VoiceRecallService…");
    try {
      const sessionId = await createSession();
      const response = await fetch("/api/__test__/tool-activity/voice-recall", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
        cache: "no-store",
      });
      const result = await response.json();
      if (!response.ok || result.session_id !== sessionId || !result.call_id || !result.request_event_id || !result.result_event_id) {
        throw new Error(result.error || `Controlled voice recall did not persist its request and result (${response.status}).`);
      }
      setStatus(`Persisted voice_recall request/result for call ${result.call_id}. Opening Chat Activity…`);
      router.push("/chat");
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "The controlled voice recall failed.");
      setBusy(false);
    }
  };

  return (
    <main className="mx-auto max-w-2xl space-y-6 p-8">
      <a href="/chat" className="text-sm font-semibold text-[var(--teal)] underline">Back to Chat</a>
      <section className="rounded-xl border border-[var(--outline)] bg-white p-6 shadow-sm">
        <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--teal)]">BOB-108 controlled runtime</p>
        <h1 className="mt-2 font-display text-2xl">Run a scripted tool scenario</h1>
        <p className="mt-3 text-sm leading-6 text-[var(--ink-soft)]">
          A loopback Provider returns one recall tool call. Hexis handles it through the production Chat endpoint and AgentLoop; the database and Activity view belong to this isolated run.
        </p>
        <button
          type="button"
          onClick={() => void runRecall()}
          disabled={busy}
          className="mt-5 rounded-md bg-[var(--foreground)] px-4 py-2 text-sm font-semibold text-white hover:bg-[var(--teal)] disabled:opacity-50"
        >
          {busy ? "Running controlled recall…" : "Run controlled recall"}
        </button>
        <button
          type="button"
          onClick={() => void runVoiceRecall()}
          disabled={busy}
          className="ml-3 mt-5 rounded-md border border-[var(--outline)] px-4 py-2 text-sm font-semibold hover:border-[var(--teal)] disabled:opacity-50"
        >
          {busy ? "Running…" : "Run controlled voice_recall"}
        </button>
        {status ? <p role="status" className="mt-4 rounded-md bg-[var(--surface-strong)] p-3 text-sm">{status}</p> : null}
      </section>
    </main>
  );
}
