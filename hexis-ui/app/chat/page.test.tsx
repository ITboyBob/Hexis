import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { isImageAttachmentFile, uploadFileName } from "./attachment-helpers";
import ChatPage from "./page";

describe("chat attachment helpers", () => {
  it("detects pasted clipboard images by mime type", () => {
    const file = new File(["pixels"], "", { type: "image/png" });

    expect(isImageAttachmentFile(file)).toBe(true);
  });

  it("adds an image extension to unnamed clipboard images", () => {
    const file = new File(["pixels"], "", { type: "image/png" });

    expect(uploadFileName(file, "pasted-image-1")).toBe("pasted-image-1.png");
  });

  it("keeps a named upload filename unchanged", () => {
    const file = new File(["pixels"], "diagram.webp", { type: "image/webp" });

    expect(uploadFileName(file, "pasted-image-1")).toBe("diagram.webp");
  });

});

describe("ChatPage attachments", () => {
  const ARTIFACT_ID = "22222222-2222-4222-8222-222222222222";

  const eventStream = (events: string[]) =>
    new Response(
      new ReadableStream({
        start(controller) {
          const encoder = new TextEncoder();
          for (const event of events) controller.enqueue(encoder.encode(event));
          controller.close();
        },
      }),
      { headers: { "Content-Type": "text/event-stream" } }
    );

  const eventStreamThatErrorsAfterDone = () =>
    new Response(
      new ReadableStream({
        pull(controller) {
          const encoder = new TextEncoder();
          controller.enqueue(
            encoder.encode(
              'event: done\ndata: {"assistant":"","session_id":"00000000-0000-4000-8000-000000000001"}\n\n'
            )
          );
          controller.error(new Error("network error"));
        },
      }),
      { headers: { "Content-Type": "text/event-stream" } }
    );

  beforeEach(() => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({
            configured: true,
            agent_name: "Samantha",
            mood: "Ready",
            valence: 0,
          });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.endsWith("/api/attachments")) {
          return Response.json({ prepared: true, artifact_id: ARTIFACT_ID, text: "", readable: false });
        }
        if (url.endsWith("/ingest")) {
          return Response.json({ accepted: true });
        }
        if (url.endsWith("/api/chat")) {
          return eventStream([
            'event: done\ndata: {"assistant":"","session_id":"00000000-0000-4000-8000-000000000001"}\n\n',
          ]);
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("restores tool calls and accepted results from the DB without creating a blank assistant bubble", async () => {
    const sessionId = "11111111-1111-4111-8111-111111111111";
    sessionStorage.setItem("hexis-chat-session-id", sessionId);
    const messages = [
      { role: "user", content: "Start a task", message_id: "user-1" },
      {
        role: "assistant",
        content: "",
        content_blocks: [{
          type: "toolCall",
          id: "call-submit",
          name: "submit",
          arguments: { task_description: "DO_NOT_RENDER_RAW_ARGUMENTS" },
        }],
        metadata: { agent_turn_id: "turn-1" },
        message_id: "call-message",
      },
      {
        role: "toolResult",
        content: JSON.stringify({
          contract_version: "m5-tool-v1",
          tool_name: "submit",
          call_id: "call-submit",
          operation_id: "DO_NOT_RENDER_RAW_DETAILS",
          ok: true,
          state: "accepted",
          data: { task_id: "DO_NOT_RENDER_RAW_DETAILS" },
          error: null,
        }),
        content_blocks: [{ type: "text", text: "" }],
        metadata: {
          agent_turn_id: "turn-1",
          model_visual_attachments: [{
            name: "proof.png",
            mime_type: "image/png",
            data_url: "data:image/png;base64,aGVsbG8=",
          }],
          tool_result: {
            toolCallId: "call-submit",
            toolName: "submit",
            isError: false,
            details: {
              ok: true,
              state: "accepted",
              operation_id: "DO_NOT_RENDER_RAW_DETAILS",
              data: { task_id: "DO_NOT_RENDER_RAW_DETAILS" },
            },
          },
        },
        message_id: "result-message",
      },
      { role: "assistant", content: "The task was accepted.", message_id: "assistant-final" },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({ configured: true, agent_name: "Samantha" });
        }
        if (url.endsWith("/api/outbox")) return Response.json({ unread: 0, messages: [] });
        if (url === `/api/chat/session/${sessionId}`) return Response.json({ messages });
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);

    expect(await screen.findByRole("group", { name: "Tool call: submit" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Tool result: submit" })).toHaveTextContent("Accepted");
    expect(await screen.findByAltText("proof.png")).toHaveAttribute(
      "src",
      "data:image/png;base64,aGVsbG8="
    );
    expect(screen.getByText("The task was accepted.")).toBeInTheDocument();
    expect(screen.queryByText("DO_NOT_RENDER_RAW_ARGUMENTS")).not.toBeInTheDocument();
    expect(screen.queryByText("DO_NOT_RENDER_RAW_DETAILS")).not.toBeInTheDocument();
    expect(screen.queryByText("Thinking...")).not.toBeInTheDocument();
  });

  it("does not send internal toolResult roles from display cache as provider chat history", async () => {
    const bodies: Record<string, unknown>[] = [];
    sessionStorage.setItem("hexis-chat-display-messages", JSON.stringify([
      { id: "user-1", role: "user", content: "Start the task" },
      {
        id: "call-1",
        role: "assistant",
        content: "",
        toolMessage: {
          kind: "call",
          callId: "call-1",
          toolName: "submit",
          summary: "Tool call",
        },
      },
      {
        id: "result-1",
        role: "toolResult",
        content: "RAW_TOOL_RESULT_SENTINEL",
        toolMessage: {
          kind: "result",
          callId: "call-1",
          toolName: "submit",
          summary: "Accepted",
          failed: false,
        },
      },
      { id: "assistant-1", role: "assistant", content: "Accepted." },
    ]));
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({ configured: true, agent_name: "Samantha" });
        }
        if (url.endsWith("/api/outbox")) return Response.json({ unread: 0, messages: [] });
        if (url.endsWith("/api/chat")) {
          bodies.push(JSON.parse(String(init?.body || "{}")));
          return eventStream([
            'event: done\ndata: {"assistant":"Done.","session_id":"22222222-2222-4222-8222-222222222222"}\n\n',
          ]);
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);
    expect(await screen.findByRole("group", { name: "Tool result: submit" })).toBeInTheDocument();
    fireEvent.change(await screen.findByLabelText("Message Samantha"), {
      target: { value: "Continue" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    const history = bodies[0].history as Array<Record<string, unknown>>;
    expect(history.map((message) => message.role)).toEqual(["user", "assistant"]);
    expect(JSON.stringify(history)).not.toContain("RAW_TOOL_RESULT_SENTINEL");
  });

  it("turns pasted clipboard images into sendable file attachments", async () => {
    render(<ChatPage />);

    const composer = await screen.findByLabelText("Message Samantha");
    const image = new File(["pixels"], "", { type: "image/png" });
    fireEvent.paste(composer, {
      clipboardData: {
        getData: () => "",
        files: [],
        items: [
          {
            kind: "file",
            getAsFile: () => image,
          },
        ],
      },
    });

    await waitFor(() => {
      expect(screen.getByText(/pasted-image-.*\.png/)).toBeInTheDocument();
    });
  });

  it("sends pasted images as live visual attachments instead of OCR-only notes", async () => {
    const chatBodies: Record<string, unknown>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({
            configured: true,
            agent_name: "Samantha",
            mood: "Ready",
            valence: 0,
          });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.endsWith("/api/attachments")) {
          return Response.json({ prepared: true, artifact_id: ARTIFACT_ID, text: "", readable: false });
        }
        if (url.endsWith("/ingest")) {
          return Response.json({ accepted: true });
        }
        if (url.endsWith("/api/chat")) {
          chatBodies.push(JSON.parse(String(init?.body || "{}")));
          return eventStream([
            'event: done\ndata: {"assistant":"","session_id":"00000000-0000-4000-8000-000000000001"}\n\n',
          ]);
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);

    const composer = await screen.findByLabelText("Message Samantha");
    const image = new File(["pixels"], "", { type: "image/png" });
    fireEvent.paste(composer, {
      clipboardData: {
        getData: () => "",
        files: [],
        items: [
          {
            kind: "file",
            getAsFile: () => image,
          },
        ],
      },
    });

    await waitFor(() => {
      expect(screen.getByText(/pasted-image-.*\.png/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(chatBodies.length).toBe(1);
    });
    const body = chatBodies[0];
    expect(body.client_message_id).toMatch(/^[0-9a-f-]{36}$/i);
    expect(body.session_id).toMatch(/^[0-9a-f-]{36}$/i);
    const visualAttachments = body.visual_attachments as Record<string, unknown>[];
    expect(visualAttachments).toHaveLength(1);
    expect(visualAttachments[0].data_url).toMatch(/^data:image\/png;base64,/);
    const addenda = (body.prompt_addenda as string[]).join("\n");
    expect(addenda).toContain("inspect the image directly in this turn");
    expect(addenda).not.toContain("OCR");
    // The message the user sees is theirs alone — no bracketed system notes.
    expect(String(body.message)).not.toContain("[Attached");
    expect(await screen.findByAltText(/pasted-image-.*\.png/)).toBeInTheDocument();
  });

  it("reads an attached PDF at attach time and answers from it in the same turn", async () => {
    const chatBodies: Record<string, unknown>[] = [];
    const ingestCalls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({
            configured: true,
            agent_name: "Samantha",
            mood: "Ready",
            valence: 0,
          });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.endsWith("/api/attachments")) {
          return Response.json({
            prepared: true,
            artifact_id: ARTIFACT_ID,
            filename: "Hartford.pdf",
            mime_type: "application/pdf",
            byte_size: 12,
            kind: "document",
            text: "[Page 1]\nThis Agreement is between Manning and the Author.",
            text_chars: 55,
            truncated: false,
            readable: true,
          });
        }
        if (url.endsWith("/ingest")) {
          ingestCalls.push(url);
          return Response.json({ accepted: true });
        }
        if (url.endsWith("/api/chat")) {
          chatBodies.push(JSON.parse(String(init?.body || "{}")));
          return eventStream([
            'event: done\ndata: {"assistant":"","session_id":"00000000-0000-4000-8000-000000000001"}\n\n',
          ]);
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    const { container } = render(<ChatPage />);
    const composer = await screen.findByLabelText("Message Samantha");
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const pdf = new File(["%PDF-1.7 ..."], "Hartford.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [pdf] } });

    // The chip names the file and says what it is — no ingestion vocabulary.
    expect(await screen.findByText("Hartford.pdf")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(/^PDF · /)).toBeInTheDocument();
    });

    fireEvent.change(composer, { target: { value: "what do u think about this?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(chatBodies.length).toBe(1);
    });
    const body = chatBodies[0];
    // The agent has the text in hand this turn.
    const addenda = (body.prompt_addenda as string[]).join("\n");
    expect(addenda).toContain("This Agreement is between Manning and the Author.");
    expect(addenda).not.toMatch(/ingest/i);
    // The message stays the user's own words.
    expect(body.message).toBe("what do u think about this?");
    expect(String(body.message)).not.toMatch(/ingest/i);
    // The file travels with the turn so a reloaded conversation still shows it.
    expect(body.attachments).toEqual([
      {
        name: "Hartford.pdf",
        mime_type: "application/pdf",
        byte_size: pdf.size,
        kind: "document",
        artifact_id: ARTIFACT_ID,
      },
    ]);
    // Sending is what files it into memory.
    expect(ingestCalls).toEqual([`/api/attachments/${ARTIFACT_ID}/ingest`]);
  });

  it("says plainly when an attached file could not be read", async () => {
    const chatBodies: Record<string, unknown>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({ configured: true, agent_name: "Samantha", mood: "Ready", valence: 0 });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.endsWith("/api/attachments")) {
          return Response.json({
            prepared: true,
            artifact_id: ARTIFACT_ID,
            kind: "document",
            text: "",
            readable: false,
            reason: "too_large",
          });
        }
        if (url.endsWith("/ingest")) return Response.json({ accepted: true });
        if (url.endsWith("/api/chat")) {
          chatBodies.push(JSON.parse(String(init?.body || "{}")));
          return eventStream([
            'event: done\ndata: {"assistant":"","session_id":"00000000-0000-4000-8000-000000000001"}\n\n',
          ]);
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    const { container } = render(<ChatPage />);
    await screen.findByLabelText("Message Samantha");
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["x"], "huge.pdf", { type: "application/pdf" })] },
    });

    expect(await screen.findByText("Too large to read in this message")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => {
      expect(chatBodies.length).toBe(1);
    });
    const addenda = (chatBodies[0].prompt_addenda as string[]).join("\n");
    expect(addenda).toContain("You have not read it");
  });

  it("does not show a network error after the chat stream already completed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({
            configured: true,
            agent_name: "Samantha",
            mood: "Ready",
            valence: 0,
          });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.endsWith("/api/chat")) {
          return eventStreamThatErrorsAfterDone();
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);

    const composer = await screen.findByLabelText("Message Samantha");
    fireEvent.change(composer, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(screen.queryByText("Chat error")).not.toBeInTheDocument();
      expect(screen.queryByText("network error")).not.toBeInTheDocument();
    });
  });

  it("marks a partial failed response as incomplete", async () => {
    const chatBodies: Record<string, unknown>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({
            configured: true,
            agent_name: "Samantha",
            mood: "Ready",
            valence: 0,
          });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.endsWith("/api/chat")) {
          chatBodies.push(JSON.parse(String(init?.body || "{}")));
          if (chatBodies.length > 1) {
            return eventStream([
              [
                "event: done",
                'data: {"assistant":"ok","session_id":"00000000-0000-4000-8000-000000000001"}',
                "",
                "",
              ].join("\n"),
            ]);
          }
          return eventStream([
            'event: token\ndata: {"phase":"conscious_final","text":"partial"}\n\n',
            'event: error\ndata: {"message":"stream disconnected"}\n\n',
            [
              "event: failed",
              'data: {"assistant":"partial","incomplete":true}',
              "",
              "",
            ].join("\n"),
          ]);
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);

    const composer = await screen.findByLabelText("Message Samantha");
    fireEvent.change(composer, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    expect(
      await screen.findByText(
        "Response incomplete: stream disconnected — not added to conversation history. You can retry."
      )
    ).toBeInTheDocument();
    expect(
      screen
        .getAllByRole("status")
        .some((status) => status.textContent?.includes("stream disconnected"))
    ).toBe(true);

    fireEvent.change(composer, { target: { value: "again" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(chatBodies).toHaveLength(2));
    expect(chatBodies[1].history).toBeUndefined();
  });

  it("shows live reasoning activity before answer tokens arrive", async () => {
    let finishStream: (() => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({
            configured: true,
            agent_name: "Samantha",
            mood: "Ready",
            valence: 0,
          });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.endsWith("/api/chat")) {
          return new Response(
            new ReadableStream({
              start(controller) {
                const encoder = new TextEncoder();
                controller.enqueue(encoder.encode("event: reasoning\ndata: {}\n\n"));
                finishStream = () => {
                  controller.enqueue(
                    encoder.encode(
                      'event: token\ndata: {"phase":"conscious_final","text":"Visible answer"}\n\n'
                    )
                  );
                  controller.enqueue(
                    encoder.encode(
                      'event: done\ndata: {"assistant":"Visible answer","session_id":"00000000-0000-4000-8000-000000000001"}\n\n'
                    )
                  );
                  controller.close();
                };
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } }
          );
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);

    const composer = await screen.findByLabelText("Message Samantha");
    fireEvent.change(composer, { target: { value: "think first" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    expect(await screen.findByText(/Reasoning\.\.\./)).toBeInTheDocument();
    expect(screen.queryByText("Visible answer")).not.toBeInTheDocument();

    finishStream?.();

    expect(await screen.findByText("Visible answer")).toBeInTheDocument();
  });

  it("marks a cleanly truncated stream incomplete instead of leaving a spinner", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({
            configured: true,
            agent_name: "Samantha",
            mood: "Ready",
            valence: 0,
          });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.endsWith("/api/chat")) {
          return eventStream([
            'event: token\ndata: {"phase":"conscious_final","text":"partial"}\n\n',
          ]);
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);

    const composer = await screen.findByLabelText("Message Samantha");
    fireEvent.change(composer, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    expect(
      await screen.findByText(
        "Response incomplete: The response stream ended before Hexis finished — not added to conversation history. You can retry."
      )
    ).toBeInTheDocument();
    expect(screen.queryByText("Thinking...")).not.toBeInTheDocument();
  });

  it("excludes an HTTP-failed turn from the next request history", async () => {
    const chatBodies: Record<string, unknown>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({
            configured: true,
            agent_name: "Samantha",
            mood: "Ready",
            valence: 0,
          });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.endsWith("/api/chat")) {
          chatBodies.push(JSON.parse(String(init?.body || "{}")));
          if (chatBodies.length === 1) {
            return Response.json({ error: "upstream unavailable" }, { status: 502 });
          }
          return eventStream([
            'event: done\ndata: {"assistant":"ok","session_id":"00000000-0000-4000-8000-000000000001"}\n\n',
          ]);
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);

    const composer = await screen.findByLabelText("Message Samantha");
    fireEvent.change(composer, { target: { value: "first" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    expect(
      await screen.findByText(
        "Response incomplete — Failed to reach chat endpoint (502). You can retry."
      )
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("status").some((status) => status.textContent?.includes("502"))
    ).toBe(true);

    fireEvent.change(composer, { target: { value: "second" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(chatBodies).toHaveLength(2));
    expect(chatBodies[1].history).toBeUndefined();
  });

  it("renders a streamed clarification card and resumes with the selected answer", async () => {
    const answers: Record<string, unknown>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({ configured: true, agent_name: "Samantha" });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.endsWith("/api/questions/answer")) {
          answers.push(JSON.parse(String(init?.body || "{}")));
          return Response.json({
            ok: true,
            status: "answered",
            answer: "The Hartford one",
          });
        }
        if (url.endsWith("/api/chat")) {
          return eventStream([
            'event: question\ndata: {"kind":"question","id":"11111111-1111-4111-8111-111111111111","prompt":"Which contract should I review?","choices":["The Manning one","The Hartford one"],"allow_free_text":true,"status":"pending"}\n\n',
            'event: done\ndata: {"assistant":"","session_id":"00000000-0000-4000-8000-000000000001"}\n\n',
          ]);
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);

    const composer = await screen.findByLabelText("Message Samantha");
    fireEvent.change(composer, { target: { value: "Review the contract" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    expect(await screen.findByText("Which contract should I review?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "The Hartford one" }));

    await waitFor(() => {
      expect(answers).toEqual([
        {
          id: "11111111-1111-4111-8111-111111111111",
          choice_index: 2,
        },
      ]);
      expect(screen.getByText(/Answer sent: The Hartford one/)).toBeInTheDocument();
    });
  });
});

describe("ChatPage recall activity", () => {
  const eventStream = (events: string[]) =>
    new Response(
      new ReadableStream({
        start(controller) {
          const encoder = new TextEncoder();
          for (const event of events) controller.enqueue(encoder.encode(event));
          controller.close();
        },
      }),
      { headers: { "Content-Type": "text/event-stream" } }
    );

  const logEvent = (payload: Record<string, unknown>) =>
    `event: log\ndata: ${JSON.stringify(payload)}\n\n`;

  const recallResult = (detail: string) => ({
    kind: "tool_result",
    title: "recall",
    detail,
  });

  const stubFetchWithChatEvents = (
    chatEvents: string[],
    responseForSession?: (sessionId: string) => Response,
  ) => {
    // Desktop viewport: the activity inspector mounts open, matching the
    // real >=1024px layout the recall body is verified against.
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({
            configured: true,
            agent_name: "Samantha",
            mood: "Ready",
            valence: 0,
          });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.endsWith("/api/chat")) {
          const { session_id: sessionId } = JSON.parse(String(init?.body));
          if (responseForSession) return responseForSession(sessionId);
          const doneEvent = `event: done\ndata: ${JSON.stringify({ assistant: "", session_id: sessionId })}\n\n`;
          return eventStream([...chatEvents, doneEvent]);
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );
  };

  const detailParagraphs = (container: HTMLElement, detail: string) =>
    Array.from(container.querySelectorAll("p")).filter(
      (p) => p.textContent === detail
    );

  const sendChatMessage = async () => {
    const composer = await screen.findByLabelText("Message Samantha");
    fireEvent.change(composer, { target: { value: "请调用 recall 工具检索记忆" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  };

  afterEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("shows the full recall body verbatim in the expanded activity list", async () => {
    const recallDetail = [
      "1. 第一条记忆正文首行\n第一条记忆正文第二行",
      `2. ${"长正文段落".repeat(60)}`,
      "3. 第三条记忆正文末尾标记",
    ].join("\n\n");
    stubFetchWithChatEvents([logEvent(recallResult(recallDetail))]);

    const { container } = render(<ChatPage />);
    await sendChatMessage();

    fireEvent.click(await screen.findByRole("button", { name: /^Activity/ }));

    await waitFor(() => {
      expect(detailParagraphs(container, recallDetail)).toHaveLength(1);
    });
    const target = detailParagraphs(container, recallDetail)[0];
    expect(target.className).toContain("whitespace-pre-wrap");
    expect(target.className).toContain("break-words");
    expect(target.className).not.toContain("line-clamp-2");
  });

  it("keeps the recall body unclamped in the collapsed activity preview", async () => {
    const recallDetail = [
      "1. 第一条记忆正文首行\n第一条记忆正文第二行",
      `2. ${"长正文段落".repeat(60)}`,
      "3. 第三条记忆正文末尾标记",
    ].join("\n\n");
    stubFetchWithChatEvents([logEvent(recallResult(recallDetail))]);

    const { container } = render(<ChatPage />);
    await sendChatMessage();

    await screen.findByRole("button", { name: /^Activity/ });

    await waitFor(() => {
      expect(detailParagraphs(container, recallDetail)).toHaveLength(1);
    });
    const target = detailParagraphs(container, recallDetail)[0];
    expect(target.className).toContain("whitespace-pre-wrap");
    expect(target.className).toContain("break-words");
    expect(target.className).not.toContain("line-clamp-2");
  });

  it("renders each recall result separately without mixing bodies", async () => {
    const firstDetail = "1. 本次A";
    const secondDetail = "1. 本次B";
    stubFetchWithChatEvents([
      logEvent({ kind: "tool_call", title: "recall", detail: '{"query":"话题A"}' }),
      logEvent(recallResult(firstDetail)),
      logEvent({ kind: "tool_call", title: "recall", detail: '{"query":"话题B"}' }),
      logEvent(recallResult(secondDetail)),
    ]);

    const { container } = render(<ChatPage />);
    await sendChatMessage();

    fireEvent.click(await screen.findByRole("button", { name: /^Activity/ }));

    await waitFor(() => {
      expect(detailParagraphs(container, firstDetail)).toHaveLength(1);
    });
    expect(detailParagraphs(container, secondDetail)).toHaveLength(1);
    expect(
      Array.from(container.querySelectorAll("p")).filter((p) =>
        p.textContent?.includes("本次A")
      )
    ).toHaveLength(1);
    expect(
      Array.from(container.querySelectorAll("p")).filter((p) =>
        p.textContent?.includes("本次B")
      )
    ).toHaveLength(1);
  });

  it("keeps the two-line preview for non-recall tools in compact mode", async () => {
    const recallDetail = "1. 召回正文";
    const searchDetail = "非召回工具的摘要输出";
    stubFetchWithChatEvents([
      logEvent(recallResult(recallDetail)),
      logEvent({ kind: "tool_result", title: "search_web", detail: searchDetail }),
    ]);

    const { container } = render(<ChatPage />);
    await sendChatMessage();

    await screen.findByRole("button", { name: /^Activity/ });

    await waitFor(() => {
      expect(detailParagraphs(container, searchDetail)).toHaveLength(1);
    });
    const target = detailParagraphs(container, searchDetail)[0];
    expect(target.className).toContain("line-clamp-2");
    expect(target.className).not.toContain("whitespace-pre-wrap");
  });

  it("keeps late text-stream activity in its source session after opening a new conversation", async () => {
    const nextSessionId = "22222222-2222-4222-8222-222222222222";
    let sourceSessionId = "";
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const encoder = new TextEncoder();
    stubFetchWithChatEvents([], (sessionId) => {
      sourceSessionId = sessionId;
      return new Response(new ReadableStream({
        start(streamController) {
          controller = streamController;
          controller.enqueue(encoder.encode(logEvent(recallResult("来源会话 recall 正文"))));
          controller.enqueue(encoder.encode(`event: done\ndata: ${JSON.stringify({ assistant: "", session_id: sessionId })}\n\n`));
        },
      }), { headers: { "Content-Type": "text/event-stream" } });
    });
    const baseFetch = fetch;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/api/chat/session")) return Response.json({ session_id: nextSessionId });
      return baseFetch(input, init);
    }));

    const { container } = render(<ChatPage />);
    await sendChatMessage();
    await waitFor(() => expect(detailParagraphs(container, "来源会话 recall 正文")).toHaveLength(1));
    const newConversation = screen.getByRole("button", { name: "Start new conversation" });
    await waitFor(() => expect(newConversation).toBeEnabled());
    fireEvent.click(newConversation);
    await screen.findByText("New conversation started.");
    await act(async () => {
      controller.enqueue(encoder.encode(logEvent(recallResult("旧流迟到 recall 正文"))));
      controller.enqueue(encoder.encode(`event: done\ndata: ${JSON.stringify({ assistant: "", session_id: sourceSessionId })}\n\n`));
      controller.close();
    });

    await waitFor(() => expect(sessionStorage.getItem("hexis-chat-session-id")).toBe(nextSessionId));
    expect(detailParagraphs(container, "来源会话 recall 正文")).toHaveLength(0);
    expect(detailParagraphs(container, "旧流迟到 recall 正文")).toHaveLength(0);
    const savedEvents = JSON.parse(sessionStorage.getItem("hexis-chat-activity-events") || "[]");
    expect(savedEvents.find((event: { detail: string }) => event.detail === "来源会话 recall 正文")?.sessionId).toBe(sourceSessionId);
  });
});

describe("ChatPage persisted tool activity", () => {
  const sessionId = "11111111-1111-4111-8111-111111111111";

  afterEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("replays persisted request and result once, consumes accepted results, and keeps long recall text readable", async () => {
    const recallText = "持久化 voice recall 正文".repeat(80);
    const calls: URL[] = [];
    const persisted = {
      session_id: sessionId,
      cursor: { "voice-tools": 3 },
      has_more: false,
      events: [
        {
          event_id: "persisted-start",
          event_type: "TOOL_START",
          stream_id: "voice-tools",
          session_id: sessionId,
          seq: 1,
          recorded_at: "2026-10-09T00:00:00.000Z",
          call_id: "voice-call-1",
          activity: { id: "persisted-start", kind: "tool_call", title: "voice_recall", detail: '{"query":"Hexis"}' },
        },
        {
          event_id: "persisted-result",
          event_type: "TOOL_RESULT",
          stream_id: "voice-tools",
          session_id: sessionId,
          seq: 2,
          recorded_at: "2026-10-09T00:00:01.000Z",
          call_id: "voice-call-1",
          activity: { id: "persisted-result", kind: "tool_result", title: "voice_recall", detail: recallText },
        },
        {
          event_id: "accepted-result",
          event_type: "TOOL_RESULT",
          stream_id: "voice-tools",
          session_id: sessionId,
          seq: 3,
          recorded_at: "2026-10-09T00:00:02.000Z",
          call_id: "remember-call",
          activity: null,
        },
      ],
    };
    sessionStorage.setItem("hexis-chat-session-id", sessionId);
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/status") return Response.json({ configured: true, agent_name: "Samantha" });
      if (url.pathname === "/api/outbox") return Response.json({ unread: 0, messages: [] });
      if (url.pathname.startsWith("/api/chat/session/")) return Response.json({ session_id: sessionId, messages: [] });
      if (url.pathname === "/api/chat/activity") {
        calls.push(url);
        return Response.json(persisted);
      }
      return Response.json({});
    }) as unknown as typeof fetch);

    const first = render(<ChatPage />);
    const recallParagraph = await screen.findByText(recallText);
    expect(recallParagraph.className).toContain("whitespace-pre-wrap");
    expect(recallParagraph.className).toContain("break-words");
    expect(recallParagraph.className).not.toContain("line-clamp-2");
    expect(screen.getByText("2 recent events")).toBeInTheDocument();
    expect(screen.queryByText("accepted-result")).toBeNull();
    expect(calls[0].searchParams.get("session_id")).toBe(sessionId);

    first.unmount();
    render(<ChatPage />);
    await screen.findByText(recallText);
    expect(screen.getByText("2 recent events")).toBeInTheDocument();
  });

  it("shows the neutral unknown receipt label without inferring failure from success=false", async () => {
    sessionStorage.setItem("hexis-chat-session-id", sessionId);
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/status") return Response.json({ configured: true, agent_name: "Samantha" });
      if (url.pathname === "/api/outbox") return Response.json({ unread: 0, messages: [] });
      if (url.pathname === "/api/chat/activity") return Response.json({
        session_id: sessionId,
        cursor: { "voice-tools": 1 },
        events: [{
          event_id: "unknown-result",
          event_type: "TOOL_RESULT",
          stream_id: "voice-tools",
          session_id: sessionId,
          seq: 1,
          recorded_at: "2026-10-09T00:00:02.000Z",
          call_id: "remember-call",
          activity: {
            id: "unknown-result",
            kind: "tool_result",
            title: "remember",
            detail: "工具调用结果无法确认。",
            status_label: "结果未确认",
            state: "unknown",
          },
        }],
      });
      return Response.json({ session_id: sessionId, messages: [] });
    }) as unknown as typeof fetch);

    render(<ChatPage />);
    expect(await screen.findByText("工具调用结果无法确认。")).toBeInTheDocument();
    expect(screen.getByText("结果未确认")).toBeInTheDocument();
  });
});

describe("ChatPage outbox replies", () => {
  beforeEach(() => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("queues a reply for the next heartbeat without using the chat composer", async () => {
    const replies: Record<string, unknown>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({
            configured: true,
            agent_name: "Samantha",
            mood: "Ready",
            valence: 0,
          });
        }
        if (url.endsWith("/api/outbox/reply")) {
          replies.push(JSON.parse(String(init?.body || "{}")));
          return Response.json({ queued: true, marked_read: 1 });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({
            unread: 1,
            messages: [
              {
                id: "11111111-1111-4111-8111-111111111111",
                kind: "user",
                intent: "check_in",
                message: "Should I prepare the report?",
                delivered_at: "2026-07-28T12:00:00Z",
                read_at: null,
              },
            ],
            pending_requests: [],
          });
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);

    const composer = await screen.findByLabelText("Message Samantha");
    fireEvent.click(await screen.findByRole("button", { name: /Show inbox/ }));
    expect(await screen.findByText("Should I prepare the report?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reply" }));

    const replyEditor = await screen.findByLabelText("Reply to Samantha");
    fireEvent.change(replyEditor, { target: { value: "Yes, please do." } });
    fireEvent.click(screen.getByRole("button", { name: "Send reply" }));

    await waitFor(() => {
      expect(replies).toEqual([
        {
          message_id: "11111111-1111-4111-8111-111111111111",
          reply: "Yes, please do.",
        },
      ]);
      expect(screen.getByText("Reply queued for Samantha's next heartbeat.")).toBeInTheDocument();
    });
    expect(composer).toHaveValue("");
    expect(screen.getByRole("heading", { name: "Inbox" })).toBeInTheDocument();
  });

  it("preserves a reply draft and retries after the inbox is unavailable", async () => {
    let replyAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({
            configured: true,
            agent_name: "Samantha",
            mood: "Ready",
            valence: 0,
          });
        }
        if (url.endsWith("/api/outbox/reply")) {
          replyAttempts += 1;
          if (replyAttempts === 1) {
            return Response.json(
              {
                detail:
                  "Reply was not queued because Hexis's inbox is unavailable.",
              },
              { status: 503 }
            );
          }
          return Response.json({ queued: true, marked_read: 1 });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({
            unread: 1,
            messages: [
              {
                id: "11111111-1111-4111-8111-111111111111",
                kind: "user",
                intent: "check_in",
                message: "Should I prepare the report?",
                delivered_at: "2026-07-28T12:00:00Z",
                read_at: null,
              },
            ],
            pending_requests: [],
          });
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);

    fireEvent.click(await screen.findByRole("button", { name: /Show inbox/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Reply" }));
    const replyEditor = await screen.findByLabelText("Reply to Samantha");
    fireEvent.change(replyEditor, { target: { value: "Yes, please do." } });
    fireEvent.click(screen.getByRole("button", { name: "Send reply" }));

    expect(
      await screen.findByText(
        "Reply was not queued because Hexis's inbox is unavailable. Check Hexis's RabbitMQ connection, then retry."
      )
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Reply to Samantha")).toHaveValue(
      "Yes, please do."
    );

    fireEvent.click(screen.getByRole("button", { name: "Send reply" }));

    expect(
      await screen.findByText("Reply queued for Samantha's next heartbeat.")
    ).toBeInTheDocument();
    expect(replyAttempts).toBe(2);
  });

  it("shows an inert automation suggestion and activates it only after Accept", async () => {
    const decisions: Record<string, unknown>[] = [];
    let pending = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({ configured: true, agent_name: "Samantha" });
        }
        if (url.endsWith("/api/automations/decide")) {
          decisions.push(JSON.parse(String(init?.body || "{}")));
          pending = false;
          return Response.json({
            ok: true,
            status: "accepted",
            schedule: "daily:08:00",
            scheduled_task_id: "22222222-2222-4222-8222-222222222222",
          });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({
            unread: 0,
            messages: [],
            pending_requests: [],
            pending_automations: pending
              ? [
                  {
                    id: "11111111-1111-4111-8111-111111111111",
                    source: "catalog",
                    title: "Morning briefing",
                    rationale: "Start the day with a deliberate review.",
                    task_spec: { schedule: "daily:08:00" },
                    status: "pending",
                    created_at: "2026-08-27T12:00:00Z",
                  },
                ]
              : [],
          });
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);
    await screen.findByLabelText("Message Samantha");
    fireEvent.click(await screen.findByRole("button", { name: /Show inbox/ }));

    expect(await screen.findByText("Morning briefing")).toBeInTheDocument();
    expect(screen.getByText("Nothing runs unless you accept.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));

    await waitFor(() => {
      expect(decisions).toEqual([
        {
          id: "11111111-1111-4111-8111-111111111111",
          decision: "accept",
        },
      ]);
      expect(
        screen.getByText("Accepted “Morning briefing” (daily:08:00). The scheduled task is active.")
      ).toBeInTheDocument();
    });
  });

  it("surfaces a contradiction as an explicit three-way decision", async () => {
    const decisions: Record<string, unknown>[] = [];
    let pending = true;
    const contradictionId = "44444444-4444-4444-8444-444444444444";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({ configured: true, agent_name: "Samantha" });
        }
        if (url.endsWith("/api/contradictions/decide")) {
          decisions.push(JSON.parse(String(init?.body || "{}")));
          pending = false;
          return Response.json({ ok: true, status: "tension", outcome: "tension" });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({
            unread: 0,
            messages: [],
            pending_requests: [],
            pending_automations: [],
            pending_node_pairings: [],
            pending_contradictions: pending
              ? [
                  {
                    id: contradictionId,
                    code: "ABC12345",
                    status: "pending",
                    tension: "The payment cadence conflicts.",
                    confidence: 0.94,
                    new_memory_id: null,
                    memory_a: {
                      id: "55555555-5555-4555-8555-555555555555",
                      content: "The retainer is monthly.",
                      trust_level: 0.9,
                      created_at: "2026-06-01T12:00:00Z",
                    },
                    memory_b: {
                      id: "66666666-6666-4666-8666-666666666666",
                      content: "The retainer is quarterly.",
                      trust_level: 0.95,
                      created_at: "2026-08-01T12:00:00Z",
                    },
                  },
                ]
              : [],
          });
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);
    await screen.findByLabelText("Message Samantha");
    fireEvent.click(await screen.findByRole("button", { name: /Show inbox/ }));

    expect(await screen.findByText("The payment cadence conflicts.")).toBeInTheDocument();
    expect(screen.getByText(/Neither memory changes until you choose/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Both, by context" }));

    await waitFor(() => {
      expect(decisions).toEqual([{ id: contradictionId, outcome: "tension" }]);
      expect(
        screen.getByText("Case ABC12345: kept both memories as context-dependent.")
      ).toBeInTheDocument();
    });
  });

  it("pairs a signed companion node only after an inbox approval", async () => {
    const decisions: Record<string, unknown>[] = [];
    let pending = true;
    const pairingId = "33333333-3333-4333-8333-333333333333";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({ configured: true, agent_name: "Samantha" });
        }
        if (url.endsWith("/api/nodes")) {
          decisions.push(JSON.parse(String(init?.body || "{}")));
          pending = false;
          return Response.json({ ok: true, status: "approved", name: "Studio Mac" });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({
            unread: 0,
            messages: [],
            pending_requests: [],
            pending_automations: [],
            pending_node_pairings: pending
              ? [
                  {
                    id: pairingId,
                    code: "A1B2C3D4",
                    node_id: "a".repeat(64),
                    name: "Studio Mac",
                    capabilities: ["system.run", "screen.capture"],
                    status: "pending",
                    requested_at: "2026-08-28T12:00:00Z",
                    expires_at: "2026-08-29T12:00:00Z",
                  },
                ]
              : [],
          });
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);
    await screen.findByLabelText("Message Samantha");
    fireEvent.click(await screen.findByRole("button", { name: /Show inbox/ }));

    expect(await screen.findByText("Studio Mac")).toBeInTheDocument();
    expect(screen.getByText(/Every host action still needs approval/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve identity" }));

    await waitFor(() => {
      expect(decisions).toEqual([{ request: pairingId, decision: "approve" }]);
      expect(
        screen.getByText(
          "Approved the signed identity for “Studio Mac”. A waiting node connects automatically."
        )
      ).toBeInTheDocument();
    });
  });

  it("opens and returns from the fixed task session without mixing conversation history", async () => {
    const origin = "11111111-1111-4111-8111-111111111111";
    const taskSession = "22222222-2222-4222-8222-222222222222";
    const secondTaskSession = "44444444-4444-4444-8444-444444444444";
    const taskId = "33333333-3333-4333-8333-333333333333";
    const taskSessionLoads: string[] = [];
    const activitySessions: string[] = [];
    sessionStorage.setItem("hexis-chat-session-id", origin);
    class FakeEventSource {
      static lastUrl = "";
      onopen: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(url: string) {
        FakeEventSource.lastUrl = url;
        queueMicrotask(() => this.onopen?.());
      }
      addEventListener() {}
      close() {}
    }
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/status")) {
          return Response.json({ configured: true, agent_name: "Samantha" });
        }
        if (url.endsWith("/api/outbox")) {
          return Response.json({ unread: 0, messages: [], pending_requests: [] });
        }
        if (url.startsWith("/api/m5/tasks?")) {
          return Response.json({
            origin_session_id: origin,
            event_cursor: { "m5-tasks": 0 },
            tasks: [
              {
                task_id: taskId,
                task_title: "S9专项任务 A",
                task_state: "idle",
                task_session_id: taskSession,
                revision: 2,
                pending: [],
              },
              {
                task_id: "55555555-5555-4555-8555-555555555555",
                task_title: "S9专项任务 B",
                task_state: "idle",
                task_session_id: secondTaskSession,
                revision: 1,
                pending: [],
              },
            ],
          });
        }
        if (url.startsWith("/api/chat/activity?")) {
          const query = new URL(url, "http://localhost").searchParams;
          const sessionId = query.get("session_id") || "";
          activitySessions.push(sessionId);
          const row = (eventId: string, title: string, detail: string) => ({
            event_id: eventId,
            event_type: "TOOL_RESULT",
            stream_id: "background-task",
            session_id: sessionId,
            seq: 1,
            recorded_at: "2026-10-09T00:00:00.000Z",
            call_id: `${eventId}-call`,
            activity: { id: eventId, kind: "tool_result", title, detail },
          });
          const events = sessionId === taskSession
            ? [row("task-a-event", "read_file", "Task A README output")]
            : sessionId === secondTaskSession
              ? [row("task-b-event", "recall", "Task B only output")]
              : [];
          return Response.json({
            session_id: sessionId,
            cursor: events.length ? { "background-task": 1 } : {},
            has_more: false,
            events,
          });
        }
        if (url === `/api/chat/session/${origin}`) {
          return Response.json({
            messages: [{ role: "assistant", content: "来源会话历史", message_id: "origin-message" }],
          });
        }
        if (url === `/api/chat/session/${taskSession}`) {
          taskSessionLoads.push(url);
          return Response.json({
            messages: [{ role: "assistant", content: "任务专属正文", message_id: "task-message" }],
          });
        }
        return Response.json({});
      }) as unknown as typeof fetch
    );

    render(<ChatPage />);
    await screen.findByText("来源会话历史", { selector: "span" });
    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));
    fireEvent.click(await screen.findByRole("button", { name: /进入任务：S9专项任务 A/ }));

    expect(await screen.findByText("任务专属正文", { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByText("来源会话历史")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show activity" }));
    expect(await screen.findByText("Task A README output")).toBeInTheDocument();
    expect(screen.queryByText("Task B only output")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));
    fireEvent.click(await screen.findByRole("button", { name: /进入任务：S9专项任务 B/ }));
    expect(await screen.findByText("Task B only output")).toBeInTheDocument();
    expect(screen.queryByText("Task A README output")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));
    fireEvent.click(screen.getByRole("button", { name: "返回来源会话" }));
    expect(await screen.findByText("来源会话历史", { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByText("任务专属正文")).toBeNull();
    expect(screen.queryByText("Task A README output")).toBeNull();
    expect(screen.queryByText("Task B only output")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "任务列表" }));
    fireEvent.click(await screen.findByRole("button", { name: /进入任务：S9专项任务 A/ }));
    expect(await screen.findByText("任务专属正文", { selector: "span" })).toBeInTheDocument();
    expect(await screen.findByText("Task A README output")).toBeInTheDocument();
    expect(activitySessions).toContain(origin);
    expect(activitySessions).toContain(taskSession);
    expect(activitySessions).toContain(secondTaskSession);
    expect(taskSessionLoads).toEqual([
      `/api/chat/session/${taskSession}`,
      `/api/chat/session/${taskSession}`,
    ]);
  });
});
