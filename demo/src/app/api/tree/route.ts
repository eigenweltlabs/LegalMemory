import { ApplianceError, treeChildren, treeLocate, treeRoots, treeSearch } from "@/lib/appliance";

/**
 * The browser's only view of the estate.
 *
 * One route rather than four files, because all four are the same thing: a
 * parameter check, a call to the appliance under this session's identity, and
 * the answer passed through. Nothing is filtered here — the appliance has
 * already decided what this caller may see, and a second opinion in the demo
 * would only be a place for the two to disagree.
 */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const op = url.searchParams.get("op") ?? "children";

  try {
    switch (op) {
      case "roots":
        return Response.json(await treeRoots());

      case "children": {
        const sourceId = url.searchParams.get("source_id");
        if (!sourceId) return badRequest("source_id is required");
        return Response.json(
          await treeChildren({
            source_id: sourceId,
            path: url.searchParams.get("path") ?? undefined,
            offset: numeric(url.searchParams.get("offset"), 0),
            limit: numeric(url.searchParams.get("limit"), 200),
          }),
        );
      }

      case "locate": {
        const documentId = url.searchParams.get("document_id");
        if (!documentId) return badRequest("document_id is required");
        return Response.json(await treeLocate(documentId));
      }

      case "search": {
        const query = url.searchParams.get("query") ?? "";
        if (!query.trim()) return Response.json({ files: [] });
        return Response.json(await treeSearch(query, numeric(url.searchParams.get("limit"), 50)));
      }

      default:
        return badRequest(`unknown op: ${op}`);
    }
  } catch (error) {
    if (error instanceof ApplianceError) {
      // 404 from the appliance means "not visible to you", which is the same
      // answer as "does not exist" and is deliberately not distinguished here.
      return Response.json({ error: error.message }, { status: error.status });
    }
    return Response.json(
      { error: error instanceof Error ? error.message : "appliance unreachable" },
      { status: 502 },
    );
  }
}

const badRequest = (message: string) => Response.json({ error: message }, { status: 400 });

function numeric(value: string | null, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}
