import "server-only";

/**
 * Server-side access to the LegalMemory appliance.
 *
 * Everything here runs on the Next server and nowhere else. The browser gets a
 * folder listing and a preview stream from this app's own routes; it never
 * learns the appliance URL, never holds the identity header, and cannot reach
 * /mcp. That is not ceremony — the trusted-header identity this demo signs in
 * with is a bearer credential in every way that matters, and a credential in a
 * client bundle is a credential the demo has published.
 */

export const applianceUrl = () =>
  (process.env.LEGALMEMORY_API_URL ?? "http://127.0.0.1:8010").replace(/\/+$/, "");

export const mcpUrl = () =>
  (process.env.LEGALMEMORY_MCP_URL || `${applianceUrl()}/mcp`).replace(/\/+$/, "") + "/";

/**
 * The caller the appliance sees. Every listing, every tool call and every
 * preview is resolved against it, so the demo shows one lawyer's estate rather
 * than the firm's.
 */
export const principals = () => process.env.LEGALMEMORY_PRINCIPALS ?? "role:admin";

export const identityHeaders = (): Record<string, string> => ({
  "x-ki-principals": principals(),
});

/**
 * The origin a client outside this container actually reached us on.
 *
 * `new URL(request.url).origin` in a route handler is built from the server's
 * bind address, which in a container is `http://0.0.0.0:3000` — a URL nobody
 * can fetch. That matters here more than anywhere else in the app: OAuth
 * discovery documents name the resource and the metadata location, and a client
 * told to fetch `0.0.0.0` simply fails.
 *
 * `DEMO_PUBLIC_URL` wins when set, because behind a proxy that rewrites Host
 * there is no header that reliably carries the public name. Otherwise the
 * forwarded headers, then Host, and only then the bind address.
 */
export function publicOrigin(request: Request): string {
  const configured = process.env.DEMO_PUBLIC_URL?.trim();
  if (configured) return configured.replace(/\/+$/, "");

  const headers = request.headers;
  const host = (headers.get("x-forwarded-host") ?? headers.get("host") ?? "")
    .split(",")[0]
    .trim();
  if (!host) return new URL(request.url).origin;

  const forwardedProto = headers.get("x-forwarded-proto")?.split(",")[0]?.trim();
  // A bare host with no forwarded protocol is either local (http) or a
  // deployment that terminates TLS in front of us (https).
  const proto =
    forwardedProto ?? (/^(localhost|127\.0\.0\.1|\[::1\])(:|$)/.test(host) ? "http" : "https");
  return `${proto}://${host}`;
}

export class ApplianceError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApplianceError";
  }
}

export async function applianceGet<T>(
  path: string,
  params: Record<string, string | number | undefined | null> = {},
): Promise<T> {
  const url = new URL(applianceUrl() + path);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  const response = await fetch(url, {
    headers: { ...identityHeaders(), accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new ApplianceError(
      response.status,
      `${path} failed: ${response.status} ${body.slice(0, 300)}`,
    );
  }
  return (await response.json()) as T;
}

// ------------------------------------------------------------------ contracts
// Mirrors of what src/knowledge_index/web/tree.py returns. Kept narrow on
// purpose: the tree renders names, sizes and types, and a field it does not
// render is a field the appliance is free to change.

export interface TreeRoot {
  source_id: string;
  display_name: string;
  kind: string;
  project_id: string | null;
  status: string;
  files: number;
}

export interface TreeFolder {
  name: string;
  path: string;
  files: number;
}

export interface TreeFile {
  source_object_id: string;
  source_id: string;
  name: string;
  path: string;
  mime_type: string | null;
  size_bytes: number | null;
  mtime: string | null;
  document_id: string;
  title: string | null;
  doc_type: string | null;
  language: string | null;
  matter_id: string | null;
  project_id: string | null;
  version_id: string;
  version_status: string | null;
  version_ordinal: number | null;
  content_hash: string | null;
  matter: { id: string; title: string | null; practice_area: string | null; status: string } | null;
}

export interface TreePage {
  source_id: string;
  path: string;
  folders: TreeFolder[];
  files: TreeFile[];
  pagination: {
    total: number;
    offset: number;
    limit: number;
    returned: number;
    has_more: boolean;
  };
}

export interface TreeLocation {
  source_id: string;
  path: string;
  ancestors: string[];
  index: number;
  file: TreeFile | null;
}

export const treeRoots = () => applianceGet<{ roots: TreeRoot[] }>("/api/tree/roots");

export const treeChildren = (params: {
  source_id: string;
  path?: string;
  offset?: number;
  limit?: number;
}) => applianceGet<TreePage>("/api/tree/children", params);

export const treeLocate = (documentId: string) =>
  applianceGet<TreeLocation>("/api/tree/locate", { document_id: documentId });

export const treeSearch = (query: string, limit = 50) =>
  applianceGet<{ files: TreeFile[] }>("/api/tree/search", { query, limit });
