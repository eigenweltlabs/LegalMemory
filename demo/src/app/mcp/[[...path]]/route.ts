import { clerkClient } from "@clerk/nextjs/server";

import { identityHeaders, mcpUrl, publicOrigin } from "@/lib/appliance";
import { mcpAuthRequired } from "@/lib/auth";

/**
 * The appliance's MCP endpoint, republished by the demo.
 *
 * An MCP client — Claude, LegalWork, MCP Inspector — points at
 * `https://<demo>/mcp/` and gets the appliance's real tools: the same fifteen
 * the admin console lists, answering out of the same index the chat on this
 * page is reading. It is a transparent JSON-RPC proxy rather than a second MCP
 * server, so there is no tool list here to drift from the appliance's own.
 *
 * The identity swap is the point. Outside, a caller authenticates as themselves
 * — a Clerk OAuth token when this deployment is configured for it. Inside, the
 * demo calls the appliance under the demo's own principal. The appliance never
 * sees the internet, and the trusted header that would let anyone claim any
 * lawyer never leaves this process.
 *
 * The catch-all segment exists because clients disagree about the trailing
 * slash and the MCP spec's own examples use one. `/mcp` and `/mcp/` are the
 * same endpoint here.
 */

// Streaming responses; nothing about this is static.
export const dynamic = "force-dynamic";

const RESOURCE_METADATA = "/.well-known/oauth-protected-resource/mcp";

/**
 * RFC 6750 §3.1 — a bare 401 makes a client report a failure and stop. The
 * `resource_metadata` parameter is what turns it into a sign-in.
 */
function unauthorized(request: Request) {
  const base = publicOrigin(request);
  return new Response(
    JSON.stringify({ error: "unauthorized", error_description: "Sign in to use this MCP server." }),
    {
      status: 401,
      headers: {
        "content-type": "application/json",
        "www-authenticate": `Bearer resource_metadata="${base}${RESOURCE_METADATA}"`,
      },
    },
  );
}

async function authorize(request: Request): Promise<Response | null> {
  // Open unless a deployment explicitly asks otherwise. See `mcpAuthRequired`:
  // this endpoint spends no model tokens, so the reason the pages are gated
  // does not apply to it.
  if (!mcpAuthRequired) return null;
  const client = await clerkClient();
  const { isAuthenticated } = await client.authenticateRequest(request, {
    acceptsToken: "oauth_token",
  });
  return isAuthenticated ? null : unauthorized(request);
}

async function proxy(request: Request, body: string | null) {
  // The appliance writes URLs into its own answers — `download_document` hands
  // back a `download_url` and a `save_command` curl — and it builds them from
  // the Host of the call it is answering. That is this process, on a container
  // address no client can reach, so without these two headers every link it
  // mints through here names a host that does not exist outside the network.
  // Forwarding the origin the caller actually used points them at
  // /api/downloads/..., which this app republishes.
  const origin = new URL(publicOrigin(request));
  const upstream = await fetch(mcpUrl(), {
    method: request.method,
    headers: {
      ...identityHeaders(),
      "x-forwarded-host": origin.host,
      "x-forwarded-proto": origin.protocol.replace(/:$/, ""),
      "content-type": request.headers.get("content-type") ?? "application/json",
      // Streamable HTTP negotiates between JSON and SSE on this header, so the
      // client's preference is carried rather than replaced.
      accept: request.headers.get("accept") ?? "application/json, text/event-stream",
    },
    body,
    cache: "no-store",
  });

  const headers = new Headers();
  for (const key of ["content-type", "cache-control", "mcp-session-id"]) {
    const value = upstream.headers.get(key);
    if (value) headers.set(key, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers });
}

export async function POST(request: Request) {
  const refusal = await authorize(request);
  if (refusal) return refusal;
  try {
    return await proxy(request, await request.text());
  } catch {
    return Response.json(
      { jsonrpc: "2.0", error: { code: -32603, message: "appliance unreachable" }, id: null },
      { status: 502 },
    );
  }
}

/** Streamable HTTP uses GET for the server-to-client stream and DELETE to end a session. */
export async function GET(request: Request) {
  const refusal = await authorize(request);
  if (refusal) return refusal;
  try {
    return await proxy(request, null);
  } catch {
    return new Response(null, { status: 502 });
  }
}

export async function DELETE(request: Request) {
  const refusal = await authorize(request);
  if (refusal) return refusal;
  try {
    return await proxy(request, null);
  } catch {
    return new Response(null, { status: 502 });
  }
}

export async function OPTIONS() {
  return new Response(null, {
    status: 204,
    headers: {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
      "access-control-allow-headers": "authorization, content-type, accept, mcp-session-id",
      "access-control-expose-headers": "mcp-session-id, www-authenticate",
    },
  });
}
