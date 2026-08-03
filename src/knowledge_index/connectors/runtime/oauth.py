"""OAuth2 authorization-code flow for connector setup.

Three calls, all provider-agnostic: build an authorize URL, exchange the returned
code for tokens, refresh later.  Per-provider differences (endpoints, scopes, whether
client credentials go in the Basic header or the form body, whether PKCE is required)
are data, not code — see ``providers.yaml``.  Adding a provider is a YAML entry.

Every deployment is bring-your-own-client (BYOC): the firm registers its own app in
Entra ID / Google Cloud and supplies the client id and secret.  There is no
Eigenwelt-hosted OAuth broker, and no cloud auth broker (Composio/Pipedream) — both
would put a third party on the path to a law firm's documents.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

import httpx
import yaml

from knowledge_index.connectors.runtime.errors import SourceAuthError

PROVIDERS_PATH = Path(__file__).with_name("providers.yaml")


@dataclass(frozen=True)
class OAuthProvider:
    """Declarative OAuth2 settings for one upstream provider."""

    short_name: str
    authorize_url: str
    token_url: str
    oauth_type: str = "with_refresh"
    grant_type: str = "authorization_code"
    scope: str | None = None
    content_type: str = "application/x-www-form-urlencoded"
    client_credential_location: str = "body"  # "body" | "header"
    requires_pkce: bool = False
    additional_authorize_params: dict[str, str] = field(default_factory=dict)
    additional_token_params: dict[str, str] = field(default_factory=dict)

    @property
    def supports_refresh(self) -> bool:
        return self.oauth_type in {"with_refresh", "with_rotating_refresh"}


@lru_cache(maxsize=1)
def load_providers(path: str | None = None) -> dict[str, OAuthProvider]:
    """Parse ``providers.yaml`` into provider settings, keyed by short name."""
    target = Path(path) if path else PROVIDERS_PATH
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    entries = raw.get("providers") or {}
    providers: dict[str, OAuthProvider] = {}
    for short_name, values in entries.items():
        if not values:  # a placeholder entry for a provider that is not OAuth
            continue
        providers[short_name] = OAuthProvider(
            short_name=short_name,
            authorize_url=values["authorize_url"],
            token_url=values["token_url"],
            oauth_type=values.get("oauth_type", "with_refresh"),
            grant_type=values.get("grant_type", "authorization_code"),
            scope=values.get("scope"),
            content_type=values.get("content_type", "application/x-www-form-urlencoded"),
            client_credential_location=values.get("client_credential_location", "body"),
            requires_pkce=bool(values.get("requires_pkce", False)),
            additional_authorize_params=dict(values.get("additional_authorize_params") or {}),
            additional_token_params=dict(values.get("additional_token_params") or {}),
        )
    return providers


def get_provider(short_name: str) -> OAuthProvider:
    providers = load_providers()
    try:
        return providers[short_name]
    except KeyError:
        known = ", ".join(sorted(providers)) or "none"
        raise SourceAuthError(
            f"{short_name!r} has no OAuth settings. Known providers: {known}. "
            "Add an entry to connectors/runtime/providers.yaml to support it."
        ) from None


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256."""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


@dataclass
class AuthorizationRequest:
    """What the admin UI needs to start a browser handshake and finish it later."""

    url: str
    state: str
    code_verifier: str | None


def build_authorization_request(
    provider: OAuthProvider,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str | None = None,
) -> AuthorizationRequest:
    """Build the provider authorize URL plus the CSRF state and PKCE verifier."""
    state = secrets.token_urlsafe(32)
    code_verifier: str | None = None
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    effective_scope = scope or provider.scope
    if effective_scope:
        params["scope"] = effective_scope
    if provider.requires_pkce:
        code_verifier, challenge = generate_pkce_pair()
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    params.update(provider.additional_authorize_params)
    separator = "&" if "?" in provider.authorize_url else "?"
    return AuthorizationRequest(
        url=f"{provider.authorize_url}{separator}{urlencode(params)}",
        state=state,
        code_verifier=code_verifier,
    )


def _client_auth(
    provider: OAuthProvider, client_id: str, client_secret: str, payload: dict[str, str]
) -> dict[str, str]:
    """Place client credentials where the provider expects them; return headers."""
    headers = {"Content-Type": provider.content_type}
    if provider.client_credential_location == "header":
        encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    else:
        payload["client_id"] = client_id
        payload["client_secret"] = client_secret
    return headers


async def _post_token(
    provider: OAuthProvider, payload: dict[str, str], headers: dict[str, str], *, what: str
) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(provider.token_url, data=payload, headers=headers)
    if response.status_code >= 400:
        # Provider error bodies name the real cause (bad redirect_uri, revoked grant,
        # wrong client). Surfacing it is the difference between a one-minute fix and an
        # afternoon; these bodies carry no tokens, only error codes.
        raise SourceAuthError(
            f"{provider.short_name} {what} failed ({response.status_code}): "
            f"{response.text[:400]}",
            source_short_name=provider.short_name,
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise SourceAuthError(
            f"{provider.short_name} {what} returned a non-JSON body",
            source_short_name=provider.short_name,
        ) from exc
    if not data.get("access_token"):
        raise SourceAuthError(
            f"{provider.short_name} {what} returned no access_token: {sorted(data)}",
            source_short_name=provider.short_name,
        )
    return data


async def exchange_code(
    provider: OAuthProvider,
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code_verifier: str | None = None,
) -> dict:
    """Exchange an authorization code for the initial credential set."""
    payload: dict[str, str] = {
        "grant_type": provider.grant_type,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier
    payload.update(provider.additional_token_params)
    headers = _client_auth(provider, client_id, client_secret, payload)
    data = await _post_token(provider, payload, headers, what="code exchange")
    if provider.supports_refresh and not data.get("refresh_token"):
        # Without a refresh token the connection dies at the first token expiry, in the
        # middle of a scheduled sync. Say so at setup time instead.
        raise SourceAuthError(
            f"{provider.short_name} issued no refresh token. The connection would stop "
            "working within the hour. Check that the app requests offline access "
            "(Microsoft: 'offline_access' scope; Google: access_type=offline).",
            source_short_name=provider.short_name,
        )
    return _credentials_from_response(data)


async def refresh_token(
    provider: OAuthProvider,
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Redeem a refresh token for a fresh access token."""
    payload: dict[str, str] = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    payload.update(provider.additional_token_params)
    headers = _client_auth(provider, client_id, client_secret, payload)
    data = await _post_token(provider, payload, headers, what="token refresh")
    credentials = _credentials_from_response(data)
    # Non-rotating providers omit refresh_token on refresh; keep the one we used so the
    # stored credential never regresses to None.
    credentials.setdefault("refresh_token", refresh_token)
    if credentials.get("refresh_token") is None:
        credentials["refresh_token"] = refresh_token
    return credentials


def _credentials_from_response(data: dict) -> dict:
    expires_in = data.get("expires_in")
    try:
        expires = int(expires_in) if expires_in is not None else None
    except (TypeError, ValueError):
        expires = None
    return {
        "access_token": str(data["access_token"]),
        "refresh_token": (str(data["refresh_token"]) if data.get("refresh_token") else None),
        "expires_in": expires,
        "token_type": data.get("token_type") or "Bearer",
        "scope": data.get("scope"),
    }
