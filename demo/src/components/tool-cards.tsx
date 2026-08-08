"use client";

import { useState } from "react";
import { makeAssistantToolUI } from "@assistant-ui/react";
import {
  ChevronRight,
  CornerDownRight,
  Loader2,
  Network,
  Scale,
  Search,
} from "lucide-react";

import { useCitations } from "./citation";
import { DocumentGraph, type GraphNode } from "./document-graph";
import { cn } from "@/lib/utils";

/**
 * What the model found, drawn instead of described.
 *
 * The appliance's tools return citation-bearing structures — typed relations
 * with the evidence that justified them, matters with their place in the
 * practice-area ontology, hits with the chunk that matched. Collapsed into a
 * paragraph, all of that becomes "I found some related documents". These cards
 * are the retrieval made visible, which is what the demo is for.
 *
 * The visual grammar is the product page's data panel: a bordered card on
 * paper, a mono eyebrow, hairline rows, and orange used once per card. Nothing
 * animates on arrival — these render mid-stream, and a card that slides in
 * every time a tool returns turns an answer into a slideshow.
 */

// ------------------------------------------------------------------- shared

/**
 * A card, open or folded away.
 *
 * A turn that searches three times and lists the matters once puts four
 * document lists above its answer, and the answer is the thing the reader
 * came for. So the lists fold: the header still says what ran and how much it
 * found — which is the audit trail, and is never hidden — and the rows are one
 * click away for anyone who wants to check the retrieval.
 *
 * The document graph does not fold. It is not a list of what was searched, it
 * is a finding, and it is the one card whose content is the demonstration.
 */
function Card({
  icon: Icon,
  eyebrow,
  title,
  slug,
  collapsible = false,
  children,
}: {
  icon: typeof Network;
  eyebrow: string;
  title?: string;
  slug: string;
  collapsible?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(!collapsible);

  return (
    <section data-card={slug} className="my-2.5 overflow-hidden rounded-xl border bg-background">
      <header
        className={cn(
          "flex items-center gap-2 px-3.5 py-2.5",
          open && "border-b",
          collapsible && "cursor-pointer hover:bg-[var(--lm-paper-2)]",
        )}
        {...(collapsible
          ? {
              role: "button",
              tabIndex: 0,
              "aria-expanded": open,
              onClick: () => setOpen((current) => !current),
              onKeyDown: (event: React.KeyboardEvent) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  setOpen((current) => !current);
                }
              },
            }
          : {})}
      >
        <Icon className="size-3.5 shrink-0 text-[var(--lm-orange)]" aria-hidden />
        <span className="lm-label">{eyebrow}</span>
        {title && (
          <span
            className={cn(
              "min-w-0 truncate text-[11.5px] text-[var(--lm-muted-2)]",
              collapsible ? "ml-auto" : "ml-auto",
            )}
          >
            {title}
          </span>
        )}
        {collapsible && (
          <ChevronRight
            className={cn(
              "size-3.5 shrink-0 text-[var(--lm-muted-3)] transition-transform duration-[var(--lm-dur-fast)]",
              open && "rotate-90",
            )}
            aria-hidden
          />
        )}
      </header>
      {open && children}
    </section>
  );
}

/**
 * A matched chunk, as one line of readable prose.
 *
 * The excerpt is a slice of the Markdown the conversion stage produced, so it
 * arrives carrying table pipes, heading hashes, bold markers and the HTML
 * entities the source document had — rendered raw, `**Holding:**` and `&amp;`
 * are the first thing the eye lands on in a search result.
 */
function plainExcerpt(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  const text = value
    .replace(/[*_`#>|]+/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return text || undefined;
}

const Running = ({ label }: { label: string }) => (
  <div className="my-2.5 flex items-center gap-2 rounded-xl border bg-background px-3.5 py-2.5">
    <Loader2 className="size-3.5 animate-spin text-[var(--lm-orange)]" />
    <span className="lm-label">{label}</span>
  </div>
);

/** A document, as a row that reveals it in the tree when clicked. */
function DocumentRow({
  documentId,
  title,
  path,
  detail,
  meta,
  indent,
}: {
  documentId: string;
  title: string;
  path?: string | null;
  detail?: string | null;
  meta?: string | null;
  indent?: boolean;
}) {
  const { onReveal } = useCitations();
  return (
    <button
      type="button"
      onClick={() => onReveal(documentId)}
      className={cn(
        "group flex w-full items-baseline gap-2 border-b px-3.5 py-2.5 text-left last:border-b-0",
        "transition-colors duration-[var(--lm-dur-fast)] hover:bg-[rgba(233,87,0,0.05)]",
        indent && "pl-8",
      )}
    >
      {indent && (
        <CornerDownRight
          className="mt-[3px] size-3 shrink-0 -translate-x-4 text-[var(--lm-muted-3)]"
          aria-hidden
        />
      )}
      <span className="min-w-0 flex-1">
        <span className="font-emphasis block truncate text-[13px] text-foreground group-hover:text-[var(--lm-orange)]">
          {title}
        </span>
        {path && (
          <span className="lm-mono mt-0.5 block truncate text-[10px] text-[var(--lm-muted-3)]">
            {path}
          </span>
        )}
        {detail && (
          // No `block` here: `line-clamp-2` works by setting display to
          // -webkit-box, and a display utility alongside it silently wins,
          // which is how a two-line excerpt becomes a ten-line wall of text.
          <span className="mt-0.5 line-clamp-2 text-[11.5px] leading-[1.5] text-[var(--lm-muted-2)]">
            {detail}
          </span>
        )}
      </span>
      {meta && (
        <span className="lm-mono shrink-0 text-[10px] whitespace-nowrap text-[var(--lm-muted-3)]">
          {meta}
        </span>
      )}
    </button>
  );
}


// --------------------------------------------------------------- unwrapping

/**
 * The payload, out of two layers of envelope.
 *
 * The AI SDK's MCP client hands back the whole `CallToolResult` when a tool has
 * no declared output schema — `{content, structuredContent, isError}` — and
 * FastMCP wraps a list return inside that in `{result: [...]}` because JSON
 * Schema has no top-level array form. Neither wrapper is stable enough to
 * assume, so each is peeled only if present, and the text content is the last
 * resort for a server that sends no structured output at all.
 */
function unwrap<T>(value: unknown): T | null {
  if (value == null || typeof value !== "object") return null;
  const envelope = value as Record<string, unknown>;
  if (envelope.isError) return null;

  let payload: unknown = value;
  if (envelope.structuredContent != null) {
    payload = envelope.structuredContent;
  } else if (Array.isArray(envelope.content)) {
    const text = (envelope.content as Array<{ type: string; text?: string }>).find(
      (part) => part.type === "text",
    )?.text;
    if (!text) return null;
    try {
      payload = JSON.parse(text);
    } catch {
      return null;
    }
  }

  if (payload != null && typeof payload === "object" && "result" in payload) {
    payload = (payload as Record<string, unknown>).result;
  }
  return (payload ?? null) as T | null;
}

interface PageBlock {
  offset: number;
  limit: number;
  returned: number;
  has_more: boolean;
  next_offset: number | null;
  total?: number;
}

/**
 * The rows and the page block out of a paginated tool result.
 *
 * Every list-shaped tool returns `{results, page}` rather than a bare list, so
 * a caller can tell a full page from the end of the corpus. The array form is
 * still accepted: this demo talks to whatever appliance it is pointed at, and
 * an older one still answers with a plain list.
 */
function unwrapPage<T>(value: unknown): { rows: T[]; page: PageBlock | null } {
  const payload = unwrap<unknown>(value);
  if (Array.isArray(payload)) return { rows: payload as T[], page: null };
  if (payload != null && typeof payload === "object" && "results" in payload) {
    const envelope = payload as { results?: unknown; page?: PageBlock };
    if (Array.isArray(envelope.results)) {
      return { rows: envelope.results as T[], page: envelope.page ?? null };
    }
  }
  return { rows: [], page: null };
}

/** "12 documents" or "12 of 137 documents" — never a count that implies a total. */
function pagedCount(shown: number, page: PageBlock | null, noun: string): string {
  if (page?.total != null && page.total > shown) return `${shown} of ${page.total} ${noun}`;
  if (page?.has_more) return `first ${shown} ${noun}`;
  return `${shown} ${noun}`;
}

interface Citation {
  document?: { id?: string; title?: string };
}

interface RelatedDocument {
  document_id: string;
  title?: string | null;
  doc_type_label?: string | null;
  version_status?: string | null;
  source_paths?: string[];
}

interface Edge {
  kind: string;
  basis?: string;
  from: { id: string };
  to: { id: string };
  provenance?: { evidence?: string[]; confidence?: number } | null;
}

// ------------------------------------------------------- the document graph

export const DocumentGraphUI = makeAssistantToolUI<
  { document_id: string },
  unknown
>({
  toolName: "find_related_documents",
  render: function DocumentGraphCard({ status, result }) {
    const { onReveal } = useCitations();
    if (status.type === "running" || result == null) {
      return <Running label="Tracing document relations" />;
    }
    const data = unwrap<{
      root_document?: RelatedDocument;
      related_documents?: RelatedDocument[];
      edges?: Edge[];
      page?: PageBlock;
    }>(result);
    const root = data?.root_document;
    const related = data?.related_documents ?? [];
    if (!root) return null;

    const rootId = root.document_id;
    const byId = new Map(related.map((item) => [item.document_id, item]));
    // One edge per neighbour: the strongest description of the connection wins,
    // and a stored relation always beats shared-matter context.
    const best = new Map<string, Edge>();
    for (const edge of data?.edges ?? []) {
      const other = edge.from.id === rootId ? edge.to.id : edge.from.id;
      if (other === rootId || !byId.has(other)) continue;
      const held = best.get(other);
      if (!held || (held.basis !== "stored_relation" && edge.basis === "stored_relation")) {
        best.set(other, edge);
      }
    }

    const graphNodes: GraphNode[] = related.map((item) => {
      const edge = best.get(item.document_id);
      return {
        documentId: item.document_id,
        title: item.title || item.source_paths?.[0]?.split("/").pop() || item.document_id,
        meta: item.doc_type_label ?? item.version_status,
        kind: edge?.kind ?? "same matter",
        stored: edge?.basis === "stored_relation",
        evidence: plainExcerpt(edge?.provenance?.evidence?.[0]),
      };
    });
    // Stored relations first: they are the ones carrying evidence, and with the
    // node count capped they are the ones worth spending the picture on.
    graphNodes.sort((a, b) => Number(b.stored) - Number(a.stored));

    return (
      <Card
        slug="document-graph"
        icon={Network}
        eyebrow="Document graph"
        title={pagedCount(related.length, data?.page ?? null, "related")}
      >
        <DocumentGraph
          rootTitle={root.title || rootId}
          nodes={graphNodes}
          totalRelated={related.length}
          onReveal={onReveal}
        />
      </Card>
    );
  },
});

// ----------------------------------------------------------- search results

export const SearchResultsUI = makeAssistantToolUI<{ query?: string }, unknown>({
  toolName: "search_semantic",
  render: ({ args, status, result }) => {
    if (status.type === "running" || result == null) {
      return <Running label={args?.query ? `Searching "${args.query}"` : "Searching"} />;
    }
    const { rows: raw, page } = unwrapPage<{
      document_id: string;
      title?: string | null;
      excerpt?: string | null;
      doc_type_label?: string | null;
      source_paths?: string[];
      score?: number;
    }>(result);

    // One row per document. Retrieval ranks chunks, so a long document that
    // matches in four places is four hits — correct for the model, and for a
    // reader just the same document listed four times. The first occurrence
    // wins because the results arrive already ranked.
    const hits = raw.filter(
      (hit, index) => raw.findIndex((other) => other.document_id === hit.document_id) === index,
    );

    if (!hits.length) {
      return (
        <Card slug="search" icon={Search} eyebrow="Search" title="no matches">
          <p className="px-3.5 py-3 text-[12.5px] text-[var(--lm-muted-2)]">
            Nothing in this estate matched, for this identity.
          </p>
        </Card>
      );
    }

    return (
      <Card
        slug="search"
        collapsible
        icon={Search}
        eyebrow="Search"
        // "first 8 documents" when more matched: the header is the only place a
        // reader learns this was a page rather than the estate's whole answer.
        title={pagedCount(hits.length, page, "documents")}
      >
        {hits.map((hit) => (
          <DocumentRow
            key={hit.document_id}
            documentId={hit.document_id}
            title={hit.title || hit.document_id}
            // Shown only when it is doing work. A firm mirroring the same brief
            // into two systems has two documents with one title, and two rows
            // that read as a rendering bug until the path says otherwise —
            // while on every other row the path is noise above the excerpt.
            path={
              hits.filter((other) => other.title === hit.title).length > 1
                ? hit.source_paths?.[0]
                : undefined
            }
            // The matched chunk, not a summary: this is the text that made the
            // document rank, and it is what a lawyer checks the hit against.
            detail={plainExcerpt(hit.excerpt)}
            meta={hit.doc_type_label}
          />
        ))}
      </Card>
    );
  },
});

// ------------------------------------------------------------ matter graph

export const MattersUI = makeAssistantToolUI<Record<string, never>, unknown>({
  toolName: "list_matters",
  render: ({ status, result }) => {
    if (status.type === "running" || result == null) {
      return <Running label="Reading the matter list" />;
    }
    const { rows: matters, page } = unwrapPage<{
      id: string;
      title?: string | null;
      reference_numbers?: string[];
      practice_area?: { label?: string; path?: string[] } | null;
      matter_kind?: { label?: string } | null;
      visible_versions?: number;
      citations?: Citation[];
    }>(result);
    if (!matters.length) return null;

    return (
      <Card
        slug="matters"
        collapsible
        icon={Scale}
        eyebrow="Matters"
        title={pagedCount(matters.length, page, "matters")}
      >
        {matters.map((matter) => {
          // The ontology walk, shown as the path it took. A practice area is a
          // node in a tree here, not a free-text label, and the path is what
          // makes a parent-area filter meaningful.
          const path = matter.practice_area?.path ?? [];
          const first = matter.citations?.[0]?.document;
          return (
            <div key={matter.id} className="border-b px-3.5 py-3 last:border-b-0">
              <div className="flex items-baseline gap-2">
                <p className="font-emphasis min-w-0 flex-1 text-[13px]">
                  {matter.title || matter.id}
                </p>
                {matter.reference_numbers?.[0] && (
                  // Capped and truncated: an unassigned matter's reference can
                  // be forty characters of invoice filename, and left to size
                  // itself it takes the row and leaves the title wrapping in a
                  // column two words wide.
                  <span
                    title={matter.reference_numbers[0]}
                    className="lm-mono max-w-[40%] shrink-0 truncate text-[10px] text-[var(--lm-muted-3)]"
                  >
                    {matter.reference_numbers[0]}
                  </span>
                )}
              </div>

              {path.length > 0 && (
                <p className="mt-1.5 flex flex-wrap items-center gap-1 text-[11px] text-[var(--lm-muted-2)]">
                  {path.map((node, index) => (
                    <span key={node} className="flex items-center gap-1">
                      {index > 0 && <span className="text-[var(--lm-muted-3)]">›</span>}
                      <span className={index === path.length - 1 ? "text-[var(--lm-orange)]" : ""}>
                        {node}
                      </span>
                    </span>
                  ))}
                </p>
              )}

              <div className="mt-2 flex items-center gap-3">
                {matter.matter_kind?.label && (
                  <span className="lm-mono text-[9.5px] text-[var(--lm-muted-3)]">
                    {matter.matter_kind.label}
                  </span>
                )}
                {matter.visible_versions != null && (
                  <span className="lm-mono text-[9.5px] text-[var(--lm-muted-3)]">
                    {matter.visible_versions} visible
                  </span>
                )}
                {first?.id && (
                  <RevealLink documentId={first.id} label={first.title ?? "Open a document"} />
                )}
              </div>
            </div>
          );
        })}
      </Card>
    );
  },
});

function RevealLink({ documentId, label }: { documentId: string; label: string }) {
  const { onReveal } = useCitations();
  return (
    <button
      type="button"
      onClick={() => onReveal(documentId)}
      className="lm-mono ml-auto max-w-[50%] truncate text-[9.5px] text-[var(--lm-orange)] hover:underline"
    >
      {label}
    </button>
  );
}

/** Every tool that has a card. Mounted once, inside the runtime provider. */
export function ToolCards() {
  return (
    <>
      <SearchResultsUI />
      <DocumentGraphUI />
      <MattersUI />
    </>
  );
}
