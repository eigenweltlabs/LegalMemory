import { publicOrigin } from "@/lib/appliance";
import { mcpAuthRequired } from "@/lib/auth";

/**
 * RFC 7591 dynamic client registration.
 *
 * MCP's authorization spec expects a client to be able to register itself: it
 * arrives knowing nothing, reads the protected-resource document, follows it to
 * the authorization server, and posts its own redirect URI to get a client id.
 * That is what makes "add this URL" the whole of a user's setup, for any client,
 * without either side knowing about the other in advance.
 *
 * Clerk's own metadata advertises no registration endpoint, so this implements
 * one and backs it with Clerk's OAuth Applications API. The alternative — a
 * hand-maintained allowlist of every client's callback — is not an open
 * protocol; it is a list that is wrong the moment somebody uses a client nobody
 * thought of, which is the situation the protocol exists to avoid.
 *
 * Registration is deliberately unauthenticated, as RFC 7591 §3 permits and MCP
 * assumes. The clients it mints are public and PKCE-only, hold no secret, and
 * can do nothing until a human completes a sign-in against the firm's own
 * identity provider — so an anonymous registration grants no access by itself.
 */

export const dynamic = "force-dynamic";

const CLERK_API = "https://api.clerk.com/v1/oauth_applications";

interface RegistrationRequest {
  client_name?: string;
  redirect_uris?: string[];
  grant_types?: string[];
  response_types?: string[];
  token_endpoint_auth_method?: string;
  scope?: string;
}

const cors = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "POST, OPTIONS",
  "access-control-allow-headers": "content-type, authorization",
};

/** A Clerk-acceptable application name: words only, no URLs, always non-empty. */
function clientName(requested: string | undefined): string {
  const cleaned = (requested ?? "")
    .replace(/[a-z][a-z0-9+.-]*:\/\/\S*/gi, " ")
    .replace(/[^\p{L}\p{N} _-]/gu, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 48);
  const suffix = Math.random().toString(36).slice(2, 8);
  return `${cleaned || "MCP client"} (${suffix})`;
}

const fail = (error: string, description: string, status = 400) =>
  Response.json({ error, error_description: description }, { status, headers: cors });

export async function POST(request: Request) {
  if (!mcpAuthRequired) {
    // An open deployment has no authorization server, so there is nothing to
    // register against and saying so is better than minting a useless client.
    return fail("invalid_request", "This deployment does not require authorization.", 404);
  }

  const secret = process.env.CLERK_SECRET_KEY;
  if (!secret) return fail("server_error", "Registration is not configured.", 500);

  let body: RegistrationRequest;
  try {
    body = (await request.json()) as RegistrationRequest;
  } catch {
    return fail("invalid_client_metadata", "Body must be JSON.");
  }

  const redirectUris = body.redirect_uris?.filter(
    (uri) => typeof uri === "string" && uri.length > 0,
  );
  if (!redirectUris?.length) {
    return fail("invalid_redirect_uri", "At least one redirect_uri is required.");
  }

  // Public + PKCE only. A client that registered itself over an unauthenticated
  // endpoint must not be handed a secret, and MCP clients are all public anyway.
  const created = await fetch(CLERK_API, {
    method: "POST",
    headers: { authorization: `Bearer ${secret}`, "content-type": "application/json" },
    body: JSON.stringify({
      // Clerk refuses a name containing anything URL-shaped, and client_name
      // arrives from an untrusted client — so it is stripped to plain words and
      // given a short suffix, since names need not be unique but reading a list
      // of twenty "MCP client" rows is not much of an audit trail either.
      name: clientName(body.client_name),
      redirect_uris: redirectUris,
      scopes: "profile email offline_access",
      public: true,
    }),
  });

  const result = (await created.json()) as {
    client_id?: string;
    redirect_uris?: string[];
    errors?: Array<{ message?: string; long_message?: string }>;
  };

  if (!created.ok || !result.client_id) {
    const detail = result.errors?.[0];
    return fail(
      "invalid_client_metadata",
      detail?.long_message ?? detail?.message ?? "Registration was refused.",
      created.status === 422 ? 400 : 502,
    );
  }

  // RFC 7591 §3.2.1. No `client_secret`, so a client that reads this correctly
  // will use PKCE and send no secret at the token endpoint.
  return Response.json(
    {
      client_id: result.client_id,
      client_id_issued_at: Math.floor(Date.now() / 1000),
      redirect_uris: result.redirect_uris ?? redirectUris,
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
      token_endpoint_auth_method: "none",
      scope: "profile email offline_access",
      client_name: body.client_name ?? "MCP client",
      registration_client_uri: `${publicOrigin(request)}/oauth/register`,
    },
    { status: 201, headers: cors },
  );
}

export const OPTIONS = () => new Response(null, { status: 204, headers: cors });
