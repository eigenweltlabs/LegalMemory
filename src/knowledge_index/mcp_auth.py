"""The MCP endpoint as an OAuth 2.1 resource server (MCP spec 2025-06-18).

The appliance never issues tokens and never sees a password: the firm's identity
provider is the authorization server, this process only verifies what it signed. Three
things are needed before an ordinary MCP client (Claude Desktop, Claude Code, MCP
Inspector) can sign a lawyer in with no hand-minted token and nothing pasted:

* protected-resource metadata (RFC 9728) naming this resource and its authorization
  server, served without authentication because a client reads it *before* it has a
  token;
* a 401 carrying ``WWW-Authenticate: Bearer resource_metadata="…"`` (RFC 6750 §3.1,
  RFC 9728 §5.1), which is the only signal that starts the login — a bare 401 makes a
  client report a failure and stop;
* an audience check that ties the token to *this* appliance (RFC 8707), so a token the
  same identity provider minted for some other application in the firm is refused here.
"""

from __future__ import annotations

from typing import Mapping
from urllib.parse import urlparse

from mcp.shared.auth import ProtectedResourceMetadata
from pydantic import AnyHttpUrl

from knowledge_index.auth import Identity, IdentityResolver
from knowledge_index.config import AppConfig

# Where the streamable-HTTP transport is mounted on the FastAPI app. The resource
# identifier and the RFC 9728 well-known path are both derived from it, so the mount
# point and what clients are told can never drift apart.
MCP_PATH = "/mcp"


def mcp_resource_url(config: AppConfig) -> str:
    """The RFC 8707 resource identifier this appliance defends.

    Must be the URL the *client* reaches, not the container-internal one, because the
    client echoes it back to the authorization server as ``?resource=`` and compares it
    against the metadata document it fetched.
    """

    configured = config.security.mcp_resource.strip()
    if configured:
        return configured.rstrip("/")
    return f"{config.connectors.public_base_url.rstrip('/')}{MCP_PATH}"


def resource_metadata_url(config: AppConfig) -> str:
    """RFC 9728 §3.1: ``/.well-known/oauth-protected-resource`` is inserted between the
    host and the resource path, so a host can defend several resources."""

    parsed = urlparse(mcp_resource_url(config))
    path = "" if parsed.path == "/" else parsed.path
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{path}"


def protected_resource_metadata(config: AppConfig) -> dict:
    """The RFC 9728 document. Built from the MCP SDK's model so the field names are the
    ones clients parse, not an approximation of them."""

    metadata = ProtectedResourceMetadata(
        resource=AnyHttpUrl(mcp_resource_url(config)),
        authorization_servers=[AnyHttpUrl(config.security.oidc_issuer.rstrip("/"))],
        scopes_supported=list(config.security.mcp_scopes),
        resource_name="Knowledge Index",
    )
    return metadata.model_dump(mode="json", exclude_none=True)


def bearer_challenge(config: AppConfig, *, error: str = "", description: str = "") -> str:
    """The ``WWW-Authenticate`` value that turns a refusal into a sign-in prompt."""

    parts = ["Bearer"]
    if error:
        parts.append(f'error="{error}"')
    if description:
        # Quoted-string, so a quote or backslash from an exception message would end the
        # header early or escape out of it.
        safe = description.replace("\\", " ").replace('"', "'")
        parts.append(f'error_description="{safe}"')
    parts.append(f'resource_metadata="{resource_metadata_url(config)}"')
    return f"{parts[0]} {', '.join(parts[1:])}"


def presented_bearer_token(headers: Mapping[str, str]) -> bool:
    """Whether the caller tried to authenticate at all.

    Distinguishes "has not signed in yet", which is the normal first step of the
    handshake, from "presented something that failed", which is a security event.
    """

    for key, value in headers.items():
        if key.casefold() == "authorization":
            return value.lower().startswith("bearer ")
    return False


def resolve_mcp_identity(headers: Mapping[str, str], config: AppConfig) -> Identity:
    """Authenticate an MCP caller, or refuse.

    A presented token is always the answer — valid or not. Falling back to the
    development header after a token failed validation would turn a rejected token into
    an unauthenticated session with whatever identity the caller typed.
    """

    resolver = IdentityResolver(config.security)
    if presented_bearer_token(headers):
        return resolver.resolve_bearer(headers, audience=mcp_resource_url(config))
    if config.security.mcp_allow_trusted_header:
        return resolver.resolve(headers)
    raise PermissionError("an OAuth bearer token issued for this appliance is required")
