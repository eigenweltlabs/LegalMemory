"use client";

import { useAuiState } from "@assistant-ui/react";
import { BookMarked } from "lucide-react";

import { citedDocuments, useCitations } from "./citation";

/**
 * The sources under the answer.
 *
 * The search cards above are the retrieval: everything the model looked at,
 * folded away because most of it did not end up mattering. This is the other
 * list — the documents the answer actually rests on — and it is the one a
 * lawyer checks. Keeping them separate is the point: "what was searched" and
 * "what this claim is based on" are different questions, and a single list
 * that conflates them answers neither.
 *
 * It is derived from the inline citations rather than from the tool results,
 * so it cannot drift from the prose. A document the model retrieved but never
 * cited does not appear here, which is correct — it supported nothing.
 */
export function CitationsCard() {
  const { onReveal } = useCitations();

  // Only text parts: a citation inside a tool result is the appliance's own
  // data, not something the model asserted.
  const text = useAuiState((state) =>
    state.message.content
      .filter((part): part is { type: "text"; text: string } => part.type === "text")
      .map((part) => part.text)
      .join("\n"),
  );
  // Rendering mid-stream would grow the card a row at a time under a paragraph
  // that is still being written, and move the text the reader is reading.
  const running = useAuiState((state) => state.message.status?.type === "running");

  if (running) return null;
  const documents = citedDocuments(text);
  if (!documents.length) return null;

  return (
    <section
      data-card="citations"
      className="mt-3 overflow-hidden rounded-xl border bg-background"
    >
      <header className="flex items-center gap-2 border-b px-3.5 py-2.5">
        <BookMarked className="size-3.5 shrink-0 text-[var(--lm-orange)]" aria-hidden />
        <span className="lm-label">Sources</span>
        <span className="ml-auto text-[11.5px] text-[var(--lm-muted-2)]">
          {documents.length} {documents.length === 1 ? "document" : "documents"}
        </span>
      </header>

      <ol className="list-none">
        {documents.map((document, index) => (
          <li key={document.documentId}>
            <button
              type="button"
              onClick={() => onReveal(document.documentId)}
              className="group flex w-full items-baseline gap-3 border-b px-3.5 py-2.5 text-left transition-colors duration-[var(--lm-dur-fast)] last:border-b-0 hover:bg-[rgba(233,87,0,0.05)]"
            >
              {/* The same mono index the product page numbers its rows with. */}
              <span className="lm-mono w-4 shrink-0 text-[10px] text-[var(--lm-muted-3)] tabular-nums">
                {String(index + 1).padStart(2, "0")}
              </span>
              <span className="font-emphasis min-w-0 flex-1 truncate text-[13px] group-hover:text-[var(--lm-orange)]">
                {document.title}
              </span>
              {/* The hint a pointer gets from hovering the row, spelled out
                  where there is no pointer to hover with. */}
              <span className="lm-label compact:opacity-100 shrink-0 opacity-0 transition-opacity duration-[var(--lm-dur-fast)] group-hover:opacity-100">
                Open
              </span>
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}
