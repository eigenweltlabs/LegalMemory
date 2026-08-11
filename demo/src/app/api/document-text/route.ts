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
 * Paginated at the source, by chunk — the same units search ranks, so the page
 * a reader is on and the page a hit reports are the same page. `/api/documents/{id}`
 * would also return this text, but it computes related documents and clause
 * extractions to do it; at fifty thousand documents that is a large query to run
 * because somebody clicked an .eml file.
 */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const documentId = url.searchParams.get("document_id");
  if (!documentId) {
    return Response.json({ error: "document_id is required" }, { status: 400 });
  }

  const requested = Number(url.searchParams.get("page") ?? 1);
  try {
    const result = await callMcpTool("get_document", {
      document_id: documentId,
      version_id: url.searchParams.get("version_id") ?? undefined,
      page: Number.isFinite(requested) ? Math.max(1, requested) : 1,
      chunks_per_page: 40,
    });

    const payload = result.structuredContent as
      | {
          content?: { text?: string };
          page?: { page?: number; pages?: number; has_more?: boolean; next_page?: number | null };
          document?: { title?: string };
        }
      | undefined;

    if (!payload) {
      return Response.json({ error: "document not found" }, { status: 404 });
    }
    return Response.json({
      text: payload.content?.text ?? "",
      page: payload.page ?? null,
      title: payload.document?.title ?? null,
    });
  } catch (error) {
    return Response.json(
      { error: error instanceof Error ? error.message : "text unavailable" },
      { status: 502 },
    );
  }
}
