import {
  authServerMetadataHandlerClerk,
  metadataCorsOptionsRequestHandler,
} from "@clerk/mcp-tools/next";

import { publicOrigin } from "@/lib/appliance";
import { mcpAuthRequired } from "@/lib/auth";

/**
 * The authorization server's metadata, with a registration endpoint added.
 *
 * Clerk's own document carries `authorization_endpoint`, `token_endpoint` and
 * PKCE support but no `registration_endpoint`, so a client that follows the
 * spec correctly reaches the end of discovery with no way to obtain a client
 * id. Advertising the endpoint this app implements (`/oauth/register`, RFC
 * 7591) closes that gap without either side special-casing the other.
 *
 * Only that one field is added — authorize and token stay Clerk's, so sign-in
 * still happens against the firm's own identity provider and no token is minted
 * or seen here.
 */
const clerkHandler = authServerMetadataHandlerClerk();
const corsHandler = metadataCorsOptionsRequestHandler();

const absent = () => new Response(null, { status: 404 });

async function handler(request: Request) {
  // Clerk's handler takes no request — it answers from instance config alone.
  const response = await clerkHandler();
  try {
    const metadata = (await response.clone().json()) as Record<string, unknown>;
    metadata.registration_endpoint = `${publicOrigin(request)}/oauth/register`;
    return Response.json(metadata, { headers: { "access-control-allow-origin": "*" } });
  } catch {
    return response;
  }
}

export const GET = mcpAuthRequired ? handler : absent;
export const OPTIONS = mcpAuthRequired ? corsHandler : absent;
