import { callMcpTool } from "@/lib/mcp";

/**
 * The converted text of one document, for formats a browser cannot render.
 *
 * Email, Outlook messages, scans and everything else the pipeline understood but
 * Chrome does not. The appliance already converted each of them once during
 * insertion, so the preview shows what the index actually holds — which is more
 * useful than a download prompt, and is the thing a lawyer would want to check
 * anyway when a search result surprises them.
 *
 * Paginated at the source. `/api/documents/{id}` would also return this text,
 * but it computes related documents and clause extractions to do it; at fifty
 * thousand documents that is a large query to run because somebody clicked an
 * .eml file.
 */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const documentId = url.searchParams.get("document_id");
  if (!documentId) {
    return Response.json({ error: "document_id is required" }, { status: 400 });
  }

  const offset = Number(url.searchParams.get("offset") ?? 0);
  try {
    const result = await callMcpTool("get_document", {
      document_id: documentId,
      version_id: url.searchParams.get("version_id") ?? undefined,
      offset: Number.isFinite(offset) ? Math.max(0, offset) : 0,
      max_chars: 40_000,
    });

    const payload = result.structuredContent as
      | {
          content?: { text?: string };
          content_page?: { has_more?: boolean; next_offset?: number | null; total_chars?: number };
          document?: { title?: string };
        }
      | undefined;

    if (!payload) {
      return Response.json({ error: "document not found" }, { status: 404 });
    }
    return Response.json({
      text: payload.content?.text ?? "",
      page: payload.content_page ?? null,
      title: payload.document?.title ?? null,
    });
  } catch (error) {
    return Response.json(
      { error: error instanceof Error ? error.message : "text unavailable" },
      { status: 502 },
    );
  }
}
