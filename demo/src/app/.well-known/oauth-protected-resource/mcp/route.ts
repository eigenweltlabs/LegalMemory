import {
  metadataCorsOptionsRequestHandler,
  protectedResourceHandlerClerk,
} from "@clerk/mcp-tools/next";

import { publicOrigin } from "@/lib/appliance";
import { mcpAuthRequired } from "@/lib/auth";

/**
 * RFC 9728 protected-resource metadata for the MCP endpoint.
 *
 * This is the document an MCP client reads *before* it has a token: it names
 * the resource and points at the authorization server, which is what turns a
 * 401 into a sign-in rather than an error. LegalWork's LegalMemory connector is
 * declared `oauth: true` and discovers exactly this path.
 *
 * Served only when the demo is configured with Clerk. On an open deployment
 * there is no authorization server to name, and advertising one would send
 * clients into a sign-in that cannot complete.
 */
const clerkHandler = protectedResourceHandlerClerk({ scopes_supported: ["profile", "email"] });
const corsHandler = metadataCorsOptionsRequestHandler();

const absent = () => new Response(null, { status: 404 });

/**
 * Two corrections to what Clerk emits.
 *
 * `resource` must identify the resource being accessed — the MCP endpoint —
 * not the origin it happens to sit on. RFC 9728 §2 makes it the identifier a
 * client echoes back as `?resource=` and compares against what it requested, so
 * an origin here is a mismatch against the `/mcp` URL the client actually
 * asked for. The appliance's own metadata gets this right; this now matches it.
 *
 * The value is also derived from the incoming request by Clerk, which inside a
 * container is the bind address — `http://0.0.0.0:3000`. A client that reads
 * that goes looking for a host which does not exist.
 */
async function handler(request: Request) {
  const response = await clerkHandler(request);
  try {
    const metadata = (await response.clone().json()) as Record<string, unknown>;
    metadata.resource = `${publicOrigin(request)}/mcp`;
    return Response.json(metadata, { headers: { "access-control-allow-origin": "*" } });
  } catch {
    return response;
  }
}

export const GET = mcpAuthRequired ? handler : absent;
export const OPTIONS = mcpAuthRequired ? corsHandler : absent;
