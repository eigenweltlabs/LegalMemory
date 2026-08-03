"use client";

import { createContext, useContext } from "react";
import { visit } from "unist-util-visit";
import type { Root, Text } from "mdast";
import type { Plugin } from "unified";

import { cn } from "@/lib/utils";

/**
 * Turning a document id in an answer into a document on screen.
 *
 * The model is instructed to write every reference as `[[doc:<id>|<title>]]`.
 * That is a deliberately inert syntax: it survives streaming a token at a time,
 * it cannot be confused with markdown the model meant literally, and a half-
 * written one renders as plain text rather than as a broken link.
 *
 * The remark plugin below rewrites those into link nodes with a `#doc-<id>`
 * fragment, which the anchor renderer picks up. Doing it in the AST rather than
 * with a string replace means a citation inside a code span or a table cell is
 * handled once, correctly, by machinery that already knows where those
 * boundaries are.
 */

const CITATION = /\[\[doc:([^\]|\s]+)\|([^\]]+)\]\]/g;

export interface CitedDocument {
  documentId: string;
  title: string;
}

/**
 * The documents an answer cited, in the order it cited them.
 *
 * Deduplicated by id, because a claim supported twice is one source, and the
 * card under the answer is a source list rather than a tally. The first title
 * wins: if the model shortens a name on a later mention, the first one is the
 * one it introduced the document under.
 */
export function citedDocuments(text: string): CitedDocument[] {
  const seen = new Map<string, CitedDocument>();
  CITATION.lastIndex = 0;
  for (let match = CITATION.exec(text); match; match = CITATION.exec(text)) {
    const documentId = match[1];
    if (!seen.has(documentId)) {
      seen.set(documentId, { documentId, title: match[2].trim() });
    }
  }
  return [...seen.values()];
}

/**
 * Drop a bibliography the model wrote anyway.
 *
 * The prompt tells it not to: the interface already collects every citation
 * into a Sources card under the answer, so a hand-written list sits directly
 * above a better copy of itself. It writes one regularly regardless — the habit
 * is deep in these models — and an instruction that is followed most of the time
 * is not a layout you can ship.
 *
 * So the display drops it. Only a *trailing* heading-plus-list is removed, and
 * only when the heading is one of the handful of things such a section is ever
 * called; a "Sources" heading in the middle of an answer is the model making a
 * point about sources and is left alone.
 */
const SOURCE_HEADING =
  /^\s*(sources?|references?|citations?|documents?(\s+(referenced|cited|reviewed))?)\s*:?\s*$/i;

function plainText(node: unknown): string {
  if (!node || typeof node !== "object") return "";
  const record = node as { value?: string; children?: unknown[] };
  if (typeof record.value === "string") return record.value;
  return (record.children ?? []).map(plainText).join("");
}

export const remarkStripSourceList: Plugin<[], Root> = () => (tree) => {
  const children = tree.children;
  // Walk back over trailing blank-ish nodes to find the last list.
  let index = children.length - 1;
  while (index > 0 && plainText(children[index]).trim() === "") index -= 1;

  const list = children[index];
  const heading = children[index - 1];
  if (!list || !heading) return;
  if (list.type !== "list") return;
  if (heading.type !== "heading" && heading.type !== "paragraph") return;
  if (!SOURCE_HEADING.test(plainText(heading))) return;

  children.splice(index - 1, 2);
};

export const CitationContext = createContext<{ onReveal: (documentId: string) => void }>({
  onReveal: () => {},
});

export const useCitations = () => useContext(CitationContext);

export const remarkDocumentCitations: Plugin<[], Root> = () => (tree) => {
  visit(tree, "text", (node: Text, index, parent) => {
    if (!parent || index === undefined || !node.value.includes("[[doc:")) return;

    const parts: Array<Text | { type: "link"; url: string; children: Text[] }> = [];
    let cursor = 0;
    CITATION.lastIndex = 0;

    for (let match = CITATION.exec(node.value); match; match = CITATION.exec(node.value)) {
      if (match.index > cursor) {
        parts.push({ type: "text", value: node.value.slice(cursor, match.index) });
      }
      parts.push({
        type: "link",
        // A fragment, not a custom `doc:` scheme. react-markdown runs every
        // href through a URL sanitiser that drops protocols it does not know,
        // which rewrote `doc:<id>` to the empty string — and an anchor with an
        // empty href opens the current page in a new tab, which is exactly the
        // blank tab this produced. Fragments are always allowed through.
        url: `#doc-${match[1]}`,
        children: [{ type: "text", value: match[2].trim() }],
      });
      cursor = match.index + match[0].length;
    }
    if (!parts.length) return;
    if (cursor < node.value.length) {
      parts.push({ type: "text", value: node.value.slice(cursor) });
    }

    parent.children.splice(index, 1, ...(parts as typeof parent.children));
    // Skip the nodes just inserted; revisiting them would rescan text that has
    // already been converted.
    return index + parts.length;
  });
};

/**
 * A citation, rendered inline.
 *
 * Not a footnote marker and not a card: the claim and its source read as one
 * sentence, the way a memo cites. Clicking reveals the document in the tree and
 * opens it in the preview — the whole point of the id being in the answer.
 */
export function CitationLink({
  documentId,
  children,
  className,
}: {
  documentId: string;
  children: React.ReactNode;
  className?: string;
}) {
  const { onReveal } = useCitations();
  return (
    <button
      type="button"
      onClick={() => onReveal(documentId)}
      title="Open in the document tree"
      className={cn(
        "mx-[1px] inline items-baseline rounded-[4px] px-[3px] text-left",
        "font-medium text-[var(--lm-orange)] underline decoration-[rgba(233,87,0,0.35)] underline-offset-2",
        "transition-colors duration-[var(--lm-dur-fast)]",
        "hover:bg-[rgba(233,87,0,0.1)] hover:decoration-[var(--lm-orange)]",
        "focus-visible:ring-2 focus-visible:ring-[var(--ring)] focus-visible:outline-none",
        className,
      )}
    >
      {children}
    </button>
  );
}
