import { applianceUrl, identityHeaders } from "@/lib/appliance";
import { callMcpTool } from "@/lib/mcp";

/**
 * The original bytes of one document, streamed to the preview panel.
 *
 * Not a text reconstruction: a lawyer checking a preview against a signed PDF
 * is checking the PDF. The bytes come out of the appliance's blob cache through
 * the capability `download_document` issues — short-lived, identity-bound,
 * re-checked against the ACL snapshot on every read, and recorded in the access
 * ledger. The demo has no other route to a document, which is the property
 * worth demonstrating.
 */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const documentId = url.searchParams.get("document_id");
  if (!documentId) {
    return Response.json({ error: "document_id is required" }, { status: 400 });
  }

  try {
    const result = await callMcpTool("download_document", {
      document_id: documentId,
      version_id: url.searchParams.get("version_id") ?? undefined,
      source_object_id: url.searchParams.get("source_object_id") ?? undefined,
    });

    const meta = (result.structuredContent ?? {}) as {
      download_url?: string;
      filename?: string;
      mime_type?: string;
      size_bytes?: number;
    };
    if (!meta.download_url) {
      return Response.json({ error: "no downloadable original" }, { status: 404 });
    }

    // Only the path is taken from the appliance's answer, and it is re-based
    // onto the configured origin. The appliance builds that URL from the Host
    // header of the call above, so behind a proxy — or a published port that
    // differs from the container's — it names an origin this process cannot
    // reach. Rebuilding it also means a value shaped like a redirect cannot
    // send the demo server fetching some other host: the capability token is
    // the credential, and it is only ever presented to the appliance it was
    // minted by.
    const capabilityPath = new URL(meta.download_url).pathname;
    const blob = await fetch(applianceUrl() + capabilityPath, {
      headers: identityHeaders(),
      cache: "no-store",
    });
    if (!blob.ok || !blob.body) {
      return Response.json({ error: "original is unavailable" }, { status: blob.status || 502 });
    }

    const headers = new Headers({
      "content-type": meta.mime_type ?? "application/octet-stream",
      // inline, not attachment: this is a preview pane, and the browser's own
      // PDF and image viewers are the ones rendering it.
      "content-disposition": `inline; filename="${encodeURIComponent(meta.filename ?? "document")}"`,
      "cache-control": "private, no-store",
      // The bytes are rendered in this app's own frames only. Without this a
      // preview URL is a public embed for anyone who learns a document id.
      "x-content-type-options": "nosniff",
    });
    if (meta.size_bytes) headers.set("content-length", String(meta.size_bytes));
    return new Response(blob.body, { headers });
  } catch (error) {
    return Response.json(
      { error: error instanceof Error ? error.message : "preview failed" },
      { status: 502 },
    );
  }
}
