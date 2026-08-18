import { applianceUrl, identityHeaders } from "@/lib/appliance";

/**
 * The appliance's short-lived download link, republished by the demo.
 *
 * `download_document` answers three ways at once: the bytes ride back inside the
 * tool result as a base64 blob, and beside them sit a `download_url` and a
 * `save_command` curl for a client that would rather fetch the file than decode
 * it. The blob is the reliable one and the tool description says so. The link is
 * the older path, and until this route existed it was broken for every client
 * reaching us through `/mcp`: the appliance is not on the internet, so a URL
 * naming it directly resolves to nothing outside the container network, and this
 * app published no `/api/downloads/...` of its own for it to name instead.
 *
 * Both halves of that are fixed here. The MCP proxy forwards the origin the
 * caller actually reached us on, so the appliance mints a link on this host, and
 * this route serves it — the same capability path, re-based onto the appliance
 * the way `/api/preview` already does. A client that follows the link or runs the
 * curl now gets the file, and one that reads the blob is unaffected.
 *
 * Only the token and the filename are taken from the request, and they are
 * re-encoded onto the configured appliance origin rather than used to build a
 * URL. Nothing a caller sends can point this fetch at another host.
 *
 * There is no session check, and that is the design rather than an omission. The
 * capability token IS the credential: the appliance issues it only to a caller
 * that was authorized for that document version, binds it to the principals held
 * at that moment, expires it, re-checks the ACL snapshot on every read, and
 * revokes it when the grant is gone. A second gate here would buy none of that
 * and would break the two things the link exists for — a curl from a terminal
 * and a fetch from an agent, neither of which carries this app's session.
 */

// A capability token as `secrets.token_urlsafe` writes one, and nothing else.
const TOKEN = /^[A-Za-z0-9._~-]{16,128}$/;

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ token: string; filename: string }> },
) {
  const { token, filename } = await params;
  if (!TOKEN.test(token) || !filename) {
    return Response.json({ error: "download link is invalid or expired" }, { status: 404 });
  }

  const upstream = `${applianceUrl()}/api/downloads/${encodeURIComponent(token)}/${encodeURIComponent(filename)}`;
  let blob: Response;
  try {
    blob = await fetch(upstream, { headers: identityHeaders(), cache: "no-store" });
  } catch {
    return Response.json({ error: "appliance unreachable" }, { status: 502 });
  }

  if (!blob.ok || !blob.body) {
    // The appliance distinguishes an invalid or expired link (404) from a
    // document whose blob has gone (410), and a caller acts differently on
    // each — ask again, or stop asking. Passing the status through keeps that
    // difference instead of flattening both to "failed".
    return Response.json(
      { error: blob.status === 410 ? "original document is unavailable" : "download link is invalid or expired" },
      { status: blob.status || 502 },
    );
  }

  const headers = new Headers({ "cache-control": "private, no-store", "x-content-type-options": "nosniff" });
  // The appliance already named the file and its type on the way out; carrying
  // its own headers is what makes `curl --output` and a browser's Save dialog
  // land on the original name rather than on the token.
  for (const key of ["content-type", "content-disposition", "content-length"]) {
    const value = blob.headers.get(key);
    if (value) headers.set(key, value);
  }
  return new Response(blob.body, { headers });
}
