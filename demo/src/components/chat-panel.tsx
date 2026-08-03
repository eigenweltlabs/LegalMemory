"use client";

import { useMemo, type PropsWithChildren } from "react";
import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { AssistantChatTransport, useChatRuntime } from "@assistant-ui/react-ai-sdk";

import { Thread } from "@/components/assistant-ui/thread";
import { CitationContext } from "@/components/citation";
import { ToolCards } from "@/components/tool-cards";

/**
 * The chat side.
 *
 * `useChatRuntime` runs the AI SDK's `useChat` underneath, so the transport
 * streams the whole agent turn — every tool call, every tool result, and the
 * text between them — rather than only the final message. That matters more
 * here than it usually does: the demo is about retrieval, and watching the model
 * search the estate and read what it found is most of what there is to see.
 *
 * The loop itself lives on the server, in /api/chat, because the tools are MCP
 * tools on the appliance and the identity that authorizes them must never reach
 * the browser.
 */
/**
 * Tool calls, shown rather than folded away.
 *
 * assistant-ui's default collapses a turn's tool calls behind a "1 tool call"
 * disclosure, which is right for an assistant whose tools are plumbing. Here
 * they are the substance: the search results and the document graph are the
 * demonstration, and hiding them behind a chevron hides the product. The cards
 * in tool-cards.tsx are already compact enough to sit in the transcript.
 */
const OpenToolGroup = ({ children }: PropsWithChildren<{ group: unknown }>) => (
  <div className="flex flex-col">{children}</div>
);

export function ChatPanel({ onReveal }: { onReveal: (documentId: string) => void }) {
  const runtime = useChatRuntime({
    transport: new AssistantChatTransport({ api: "/api/chat" }),
  });

  const citations = useMemo(() => ({ onReveal }), [onReveal]);

  return (
    <CitationContext.Provider value={citations}>
      <AssistantRuntimeProvider runtime={runtime}>
        {/* Registers a renderer per tool name. Rendered here rather than inside
            Thread so the registration outlives any one message. */}
        <ToolCards />
        <div className="flex h-full min-h-0 flex-col bg-background">
          <header className="flex flex-none items-baseline justify-between gap-3 border-b px-5 pt-4 pb-3.5">
            <span className="lm-label">Ask</span>
            <span className="lm-mono text-[10px] text-[var(--lm-muted-3)]">
              Answers cite the documents they came from
            </span>
          </header>
          <div className="min-h-0 flex-1">
            <Thread components={{ ToolGroup: OpenToolGroup }} />
          </div>
        </div>
      </AssistantRuntimeProvider>
    </CitationContext.Provider>
  );
}
