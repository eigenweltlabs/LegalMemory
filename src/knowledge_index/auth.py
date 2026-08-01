"""OIDC and trusted-auth-proxy identity resolution for on-prem deployments."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping

import jwt

from knowledge_index.config import SecurityConfig
from knowledge_index.permissions import canonical_principals


@dataclass(frozen=True)
class Identity:
    subject: str
    username: str
    principals: frozenset[str]
    groups: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return "role:admin" in self.principals


class IdentityResolver:
    def __init__(self, config: SecurityConfig) -> None:
        self.config = config

    def resolve(self, headers: Mapping[str, str]) -> Identity:
        normalized_headers = {key.casefold(): value for key, value in headers.items()}
        if self.config.auth_mode == "oidc":
            return self._resolve_oidc(normalized_headers, self.config.oidc_audience)
        return self._resolve_trusted_header(normalized_headers)

    def resolve_bearer(self, headers: Mapping[str, str], *, audience: str) -> Identity:
        """Validate a bearer token whatever the global ``auth_mode`` says.

        ``trusted_header`` is a statement about a reverse proxy standing in front of the
        *admin UI*; an MCP client connects straight to the port with no proxy in the
        path. Letting the global mode decide here would mean any process that reaches
        the port names itself in a header and becomes any lawyer in the firm.

        ``audience`` is passed in rather than read off the config because the MCP
        endpoint binds tokens to its own resource identifier (RFC 8707), which is a
        different and stricter value than the API's ``oidc_audience``.
        """
        return self._resolve_oidc(
            {key.casefold(): value for key, value in headers.items()}, audience
        )

    def _resolve_trusted_header(self, headers: Mapping[str, str]) -> Identity:
        expected_secret = self.config.trusted_header_secret
        if expected_secret and headers.get("x-ki-proxy-secret") != expected_secret:
            raise PermissionError("trusted authentication proxy signature is missing or invalid")
        raw = headers.get(self.config.trusted_header_name.casefold(), "")
        if not raw:
            # Source ACLs identify people by email. OAuth2 Proxy exposes that claim
            # separately from its opaque OIDC user id, so prefer the verified address
            # when the request came through the sign-in proxy.
            user = (
                headers.get("x-auth-request-email")
                or headers.get("x-forwarded-email")
                or headers.get("x-auth-request-preferred-username")
                or headers.get("x-forwarded-preferred-username")
                or headers.get("x-auth-request-user")
                or headers.get("x-forwarded-user")
            )
            groups = headers.get("x-auth-request-groups") or headers.get("x-forwarded-groups", "")
            forwarded = ([f"user:{user}"] if user else []) + [
                # Keycloak group-membership claims may arrive as full paths ("/admins").
                f"group:{item.strip().strip('/')}"
                for item in groups.split(",")
                if item.strip().strip("/")
            ]
            raw = ",".join(forwarded)
        principals = canonical_principals(raw.split(","))
        if not principals:
            raise PermissionError("authenticated principal context is required")
        principals.add("role:authenticated")
        admin_groups = {f"group:{item.casefold().strip('/')}" for item in self.config.admin_groups}
        if principals & admin_groups:
            principals.add("role:admin")
        subject = next(
            (item.removeprefix("user:") for item in principals if item.startswith("user:")),
            "proxy-user",
        )
        groups = tuple(
            sorted(item.removeprefix("group:") for item in principals if item.startswith("group:"))
        )
        return Identity(
            subject=subject, username=subject, principals=frozenset(principals), groups=groups
        )

    def _resolve_oidc(self, headers: Mapping[str, str], audience: str) -> Identity:
        authorization = headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            raise PermissionError("OIDC bearer token is required")
        token = authorization.split(" ", 1)[1].strip()
        if not audience:
            # Without an audience PyJWT accepts a token minted for any client in the
            # realm. A misconfiguration must not quietly widen who gets in.
            raise PermissionError("no expected audience is configured; refusing the token")
        try:
            claims = jwt.decode(
                token,
                _jwks_client(self.config.jwks_url).get_signing_key_from_jwt(token).key,
                algorithms=["RS256", "ES256"],
                audience=audience,
                issuer=self.config.oidc_issuer.rstrip("/"),
            )
        except jwt.PyJWTError as exc:
            raise PermissionError("OIDC token validation failed") from exc
        subject = str(claims.get(self.config.subject_claim, "")).strip()
        if not subject:
            raise PermissionError("OIDC token has no subject")
        username = str(claims.get(self.config.username_claim, subject)).strip() or subject
        raw_groups = claims.get(self.config.groups_claim, [])
        if isinstance(raw_groups, str):
            raw_groups = [raw_groups]
        groups = tuple(
            sorted(str(item).strip().strip("/") for item in raw_groups if str(item).strip())
        )
        principals = {f"user:{subject.casefold()}", f"username:{username.casefold()}"}
        # Keycloak retains the Google/Entra email as a verified claim while ``sub`` is
        # an immutable realm id. Mirrored source ACLs name the email, so carry both
        # spellings into the authorization scope without replacing the stable subject.
        email = str(claims.get("email", "")).strip().casefold()
        if email and claims.get("email_verified") is True:
            principals.update({f"user:{email}", f"username:{email}"})
        principals.add("role:authenticated")
        principals.update(f"group:{item.casefold()}" for item in groups)
        admin_groups = {item.casefold().strip("/") for item in self.config.admin_groups}
        if {item.casefold() for item in groups} & admin_groups:
            principals.add("role:admin")
        return Identity(
            subject=subject,
            username=username,
            principals=frozenset(principals),
            groups=groups,
        )


@lru_cache(maxsize=16)
def _jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(jwks_url)
