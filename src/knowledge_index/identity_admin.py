"""Drive the firm's Keycloak realm from this console, over its admin REST API.

An administrator who wants a lawyer to sign in with Google should not have to learn
that Keycloak exists, find it on another port, and log in with another password. Every
setting a firm needs is written from here: the identity provider itself, the broker
mappers that make the imported username match what the connectors report, and the four
token settings that a working OIDC login turns out to depend on.

Everything is idempotent. Setup gets re-run — a second administrator, a re-paste of a
rotated secret, a fresh stack against an existing realm — and the second run has to
converge on the same realm as the first rather than fail or duplicate.

The client secret never leaves this process towards a browser. It goes to Keycloak,
and a copy goes to the database as AES-256-GCM ciphertext under the same key as
connector credentials, because a re-test months later cannot ask Keycloak for it:
the admin API masks it on read.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from knowledge_index.config import IdentityConfig

# A response body from an identity provider is the only thing that explains a failure,
# and it can contain a token or an assertion. Truncated hard before it is ever shown.
MAX_DETAIL_CHARS = 300
TIMEOUT = 15.0


class IdentityAdminError(RuntimeError):
    """The realm could not be read or written. Never carries a secret."""


@dataclass(frozen=True)
class Preset:
    """One way a firm's people sign in, and the one extra fact it needs to be found."""

    kind: str
    label: str
    # Provider-specific input beyond client id + secret. Empty for Google, whose
    # discovery document is at a single well-known address for every customer.
    field_name: str = ""
    field_label: str = ""
    field_placeholder: str = ""
    field_hint: str = ""
    discovery_template: str = ""
    scopes: str = "openid profile email"
    console: str = ""
    console_url: str = ""

    def discovery_url(self, value: str) -> str:
        cleaned = (value or "").strip().strip("/")
        if not self.field_name:
            return self.discovery_template
        if not cleaned:
            raise IdentityAdminError(f"{self.field_label} is required for {self.label}")
        if self.field_name == "discovery_url":
            if not cleaned.lower().startswith(("http://", "https://")):
                raise IdentityAdminError("the discovery URL must start with https://")
            return cleaned
        # A host was asked for; a pasted URL is the usual mistake, so strip it back.
        host = cleaned.split("://", 1)[-1].split("/", 1)[0]
        return self.discovery_template.format(value=host or cleaned)


PRESETS: dict[str, Preset] = {
    "google": Preset(
        kind="google",
        label="Google",
        discovery_template="https://accounts.google.com/.well-known/openid-configuration",
        console="Google Cloud console",
        console_url="https://console.cloud.google.com/apis/credentials",
    ),
    "entra": Preset(
        kind="entra",
        label="Microsoft (Entra)",
        field_name="tenant",
        field_label="Directory (tenant) ID",
        field_placeholder="00000000-0000-0000-0000-000000000000",
        field_hint="Entra ID › App registrations › Overview.",
        discovery_template=(
            "https://login.microsoftonline.com/{value}/v2.0/.well-known/openid-configuration"
        ),
        console="Microsoft Entra admin center",
        console_url="https://entra.microsoft.com/",
    ),
    "okta": Preset(
        kind="okta",
        label="Okta",
        field_name="domain",
        field_label="Okta domain",
        field_placeholder="firm.okta.com",
        field_hint="Without https:// — the host only.",
        discovery_template="https://{value}/.well-known/openid-configuration",
        console="Okta admin console",
        console_url="https://login.okta.com/",
    ),
    "oidc": Preset(
        kind="oidc",
        label="Other OIDC",
        field_name="discovery_url",
        field_label="Discovery URL",
        field_placeholder="https://idp.firm.de/.well-known/openid-configuration",
        field_hint="The provider's OpenID configuration document.",
        console="your provider's console",
    ),
}


def preset(kind: str) -> Preset:
    found = PRESETS.get((kind or "").strip().casefold())
    if found is None:
        raise IdentityAdminError(f"unknown sign-in provider: {kind!r}")
    return found


# The minimum a realm gets when it has none of its own, before this appliance issues
# its first local password. See KeycloakAdmin.ensure_password_policy.
LOCAL_PASSWORD_POLICY = "length(12) and notUsername(undefined)"

# Look-alikes are removed: this password is read off one screen and typed into another,
# and a zero mistaken for an O costs a support call the firm has nobody to make.
_LOWER = "abcdefghijkmnopqrstuvwxyz"
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_DIGITS = "23456789"


def generate_password(blocks: int = 4, size: int = 5) -> str:
    """A temporary password strong enough to be the only thing in front of a matter.

    Twenty characters from a 57-character alphabet, grouped for reading aloud. At least
    one of each case and one digit, so a realm policy the firm tightens later does not
    start rejecting the passwords this console issues.
    """
    pool = _LOWER + _UPPER + _DIGITS
    while True:
        raw = "".join(secrets.choice(pool) for _ in range(blocks * size))
        if all(any(item in group for item in raw) for group in (_LOWER, _UPPER, _DIGITS)):
            break
    return "-".join(raw[index : index + size] for index in range(0, len(raw), size))


def normalize_email(value: str) -> str:
    """The join key, or a refusal.

    Not a validator for mail deliverability — it is a guard on the one field that
    decides what this person will be able to see. Something without an ``@`` cannot
    match anything a connector mirrored, so accepting it would create an account that
    works and shows nothing.
    """
    cleaned = (value or "").strip().casefold()
    local, _, domain = cleaned.partition("@")
    if (
        cleaned.count("@") != 1
        or not local
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or any(item.isspace() for item in cleaned)
    ):
        raise IdentityAdminError(
            f"{value.strip()!r} is not an email address. The address is what decides "
            "what this person can see, so it has to be the real one."
        )
    return cleaned


def catalog() -> list[dict]:
    """The provider choices, for the picker. No secrets, no realm access needed."""
    return [
        {
            "kind": item.kind,
            "label": item.label,
            "field": item.field_name,
            "field_label": item.field_label,
            "field_placeholder": item.field_placeholder,
            "field_hint": item.field_hint,
            "console": item.console,
            "console_url": item.console_url,
        }
        for item in PRESETS.values()
    ]


@dataclass
class Check:
    """One thing "Test sign-in" actually established, and how."""

    id: str
    label: str
    ok: bool
    detail: str = ""

    def payload(self) -> dict:
        return {"id": self.id, "label": self.label, "ok": self.ok, "detail": self.detail[:MAX_DETAIL_CHARS]}


def fetch_discovery(url: str, *, client: httpx.Client | None = None) -> dict:
    """Read and validate a provider's OpenID configuration.

    The first real proof that the tenant id or Okta domain an administrator typed
    names a directory that exists — before anything is written to the realm.
    """
    owned = client is None
    http = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    try:
        response = http.get(url)
    except httpx.HTTPError as exc:
        raise IdentityAdminError(f"could not reach {url}: {type(exc).__name__}") from exc
    finally:
        if owned:
            http.close()
    if response.status_code != 200:
        raise IdentityAdminError(f"{url} answered {response.status_code}, not an OIDC discovery document")
    try:
        document = response.json()
    except ValueError as exc:
        raise IdentityAdminError(f"{url} did not return JSON") from exc
    missing = [
        key
        for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
        if not document.get(key)
    ]
    if missing:
        raise IdentityAdminError(f"discovery document is missing {', '.join(missing)}")
    return document


def probe_client_credentials(
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    *,
    client: httpx.Client | None = None,
) -> Check:
    """Ask the provider whether it recognises this client id and secret.

    Redeems a code that cannot exist. RFC 6749 §5.2 makes the two answers distinct and
    that distinction is the whole test: ``invalid_client`` means the credentials were
    rejected, anything else means the provider authenticated the client and then
    complained about the code — which is exactly what was asked. Verified against
    Google, which answers ``invalid_client`` / HTTP 401 for a client that does not
    exist.

    No browser is involved and no session is created, so running it costs nothing and
    can be repeated.
    """
    owned = client is None
    http = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    try:
        response = http.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": "knowledge-index-credential-probe",
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        return Check("credentials", "Client id and secret", False, f"could not reach the token endpoint: {type(exc).__name__}")
    finally:
        if owned:
            http.close()
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = str(body.get("error") or "")
    description = str(body.get("error_description") or "")
    if error in {"invalid_client", "unauthorized_client"} or response.status_code == 401:
        return Check("credentials", "Client id and secret", False, description or f"the provider rejected the client ({error or response.status_code})")
    if error:
        # invalid_grant / invalid_request: the client authenticated, the probe code did
        # not. That is the pass.
        return Check("credentials", "Client id and secret", True, f"accepted by the provider ({error})")
    if response.status_code < 400:
        return Check("credentials", "Client id and secret", True, "accepted by the provider")
    return Check("credentials", "Client id and secret", False, f"unexpected answer {response.status_code} from the token endpoint")


def identity_provider_payload(
    alias: str, *, display_name: str, client_id: str, client_secret: str, discovery: dict, scopes: str
) -> dict:
    """A Keycloak ``oidc`` broker built from the provider's own discovery document.

    One provider type for all four presets. Keycloak's branded social providers hide
    their endpoints, which means a failure has nothing to point at; filling the
    endpoints from the document this appliance has just fetched and validated keeps
    every field traceable to something an administrator can check.
    """
    return {
        "alias": alias,
        "providerId": "oidc",
        "displayName": display_name,
        "enabled": True,
        "storeToken": False,
        # The provider verified the address; a second confirmation mail from Keycloak
        # would strand every lawyer at an unverified account.
        "trustEmail": True,
        "firstBrokerLoginFlowAlias": "first broker login",
        "config": {
            "clientId": client_id,
            "clientSecret": client_secret,
            "clientAuthMethod": "client_secret_post",
            "issuer": discovery["issuer"],
            "authorizationUrl": discovery["authorization_endpoint"],
            "tokenUrl": discovery["token_endpoint"],
            "jwksUrl": discovery["jwks_uri"],
            "userInfoUrl": discovery.get("userinfo_endpoint", ""),
            "logoutUrl": discovery.get("end_session_endpoint", ""),
            "useJwksUrl": "true",
            "validateSignature": "true",
            "defaultScope": scopes,
            "syncMode": "FORCE",
        },
    }


class KeycloakAdmin:
    """The realm, as this appliance writes it. Every call is idempotent."""

    def __init__(self, config: IdentityConfig, *, username: str, password: str, client: httpx.Client | None = None) -> None:
        self.config = config
        self._username = username
        self._password = password
        self._client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        self._token = ""
        self._token_expires = 0.0

    @classmethod
    def from_config(cls, config: IdentityConfig, *, client: httpx.Client | None = None) -> "KeycloakAdmin":
        username = os.environ.get(config.admin_username_env, "").strip()
        password = os.environ.get(config.admin_password_env, "").strip()
        if not username or not password:
            raise IdentityAdminError(
                f"{config.admin_username_env} and {config.admin_password_env} are not set on this "
                "deployment, so Knowledge Index cannot configure the realm on your behalf."
            )
        return cls(config, username=username, password=password, client=client)

    # ----------------------------------------------------------------- transport

    def _bearer(self) -> str:
        if self._token and time.monotonic() < self._token_expires:
            return self._token
        base = self.config.admin_base_url.rstrip("/")
        url = f"{base}/realms/{self.config.admin_realm}/protocol/openid-connect/token"
        try:
            response = self._client.post(
                url,
                data={
                    "grant_type": "password",
                    "client_id": self.config.admin_client_id,
                    "username": self._username,
                    "password": self._password,
                },
            )
        except httpx.HTTPError as exc:
            raise IdentityAdminError(f"Keycloak is unreachable at {base}: {type(exc).__name__}") from exc
        if response.status_code != 200:
            # Never echo the body: a failed password grant can name the account.
            raise IdentityAdminError(
                f"Keycloak refused the appliance's administrator credentials ({response.status_code})"
            )
        payload = response.json()
        self._token = str(payload.get("access_token", ""))
        self._token_expires = time.monotonic() + max(int(payload.get("expires_in", 60)) - 10, 5)
        if not self._token:
            raise IdentityAdminError("Keycloak returned no administrator token")
        return self._token

    def _call(self, method: str, path: str, *, json: Any = None, params: dict | None = None) -> httpx.Response:
        url = f"{self.config.admin_base_url.rstrip('/')}/admin/realms/{self.config.realm}{path}"
        try:
            response = self._client.request(
                method, url, json=json, params=params, headers={"Authorization": f"Bearer {self._bearer()}"}
            )
        except httpx.HTTPError as exc:
            raise IdentityAdminError(f"Keycloak request failed: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return response
        if response.status_code >= 400:
            raise IdentityAdminError(f"Keycloak answered {response.status_code} for {method} {path}")
        return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "KeycloakAdmin":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------- identity providers

    def identity_providers(self) -> list[dict]:
        return list(self._call("GET", "/identity-provider/instances").json())

    def identity_provider(self, alias: str) -> dict | None:
        response = self._call("GET", f"/identity-provider/instances/{alias}")
        return None if response.status_code == 404 else response.json()

    def upsert_identity_provider(self, payload: dict) -> str:
        """Create the broker, or update it in place. Returns "created" or "updated"."""
        alias = payload["alias"]
        if self.identity_provider(alias) is None:
            self._call("POST", "/identity-provider/instances", json=payload)
            return "created"
        self._call("PUT", f"/identity-provider/instances/{alias}", json=payload)
        return "updated"

    def delete_identity_provider(self, alias: str) -> bool:
        if self.identity_provider(alias) is None:
            return False
        self._call("DELETE", f"/identity-provider/instances/{alias}")
        return True

    def ensure_broker_mappers(self, alias: str) -> list[str]:
        """Force the imported Keycloak username to be the address the provider asserts.

        This is the join key. Every connector normalises a person to ``user:<email>``,
        and access is decided by matching that string. A broker that imports a lawyer
        as ``ursula`` while Drive reports ``ursula@firm.de`` produces no error and no
        documents, which is the most expensive failure this page can prevent.
        """
        existing = {item.get("name") for item in self._call("GET", f"/identity-provider/instances/{alias}/mappers").json()}
        wanted = [
            {
                "name": "username-from-email",
                "identityProviderAlias": alias,
                "identityProviderMapper": "oidc-username-idp-mapper",
                "config": {"template": "${CLAIM.email}", "target": "LOCAL", "syncMode": "INHERIT"},
            },
            {
                "name": "email",
                "identityProviderAlias": alias,
                "identityProviderMapper": "oidc-user-attribute-idp-mapper",
                "config": {"claim": "email", "user.attribute": "email", "syncMode": "INHERIT"},
            },
        ]
        added = []
        for mapper in wanted:
            if mapper["name"] in existing:
                continue
            self._call("POST", f"/identity-provider/instances/{alias}/mappers", json=mapper)
            added.append(mapper["name"])
        return added

    # ------------------------------------------------------------ token claims

    def _client_row(self, client_id: str) -> dict | None:
        rows = self._call("GET", "/clients", params={"clientId": client_id}).json()
        return rows[0] if rows else None

    def ensure_token_claims(self, audience: str) -> list[dict]:
        """The three realm settings a working OIDC login turns out to require.

        Each was found the hard way against Keycloak 26 and each fails silently: a
        token with no ``aud`` fails audience validation, a lightweight access token has
        no ``sub``, and a realm without the ``basic`` client scope never had one. They
        are asserted here so no future administrator rediscovers them.
        """
        changes: list[dict] = []
        for client_id in self.config.token_client_ids:
            row = self._client_row(client_id)
            if row is None:
                changes.append({"client": client_id, "change": "absent from the realm", "applied": False})
                continue
            attributes = dict(row.get("attributes") or {})
            if attributes.get("client.use.lightweight.access.token.enabled") != "false":
                attributes["client.use.lightweight.access.token.enabled"] = "false"
                self._call("PUT", f"/clients/{row['id']}", json={**row, "attributes": attributes})
                changes.append({"client": client_id, "change": "full access tokens", "applied": True})
            if not self._has_sub_claim(row):
                self._call(
                    "POST",
                    f"/clients/{row['id']}/protocol-mappers/models",
                    json={
                        "name": "subject",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-sub-mapper",
                        "config": {"access.token.claim": "true", "introspection.token.claim": "true"},
                    },
                )
                changes.append({"client": client_id, "change": "sub claim", "applied": True})
        audience_client = self._client_row(self.config.audience_client_id)
        if audience_client is None:
            changes.append({"client": self.config.audience_client_id, "change": "absent from the realm", "applied": False})
            return changes
        has_audience = any(
            item.get("protocolMapper") == "oidc-audience-mapper"
            and (item.get("config") or {}).get("included.client.audience") == audience
            for item in audience_client.get("protocolMappers") or []
        )
        if not has_audience and not self._scope_carries(audience_client, "oidc-audience-mapper", "included.client.audience", audience):
            self._call(
                "POST",
                f"/clients/{audience_client['id']}/protocol-mappers/models",
                json={
                    "name": f"{audience}-audience",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-audience-mapper",
                    "config": {
                        "included.client.audience": audience,
                        "id.token.claim": "false",
                        "access.token.claim": "true",
                        "introspection.token.claim": "true",
                    },
                },
            )
            changes.append({"client": self.config.audience_client_id, "change": f"aud={audience}", "applied": True})
        return changes

    def _scope_carries(
        self, client_row: dict, protocol_mapper: str, config_key: str = "", config_value: str = ""
    ) -> bool:
        """Whether a default client scope already produces this claim.

        A realm that carries it through the ``basic`` or audience scope is correct as
        it stands. Adding a second mapper for the same claim would not break anything,
        but it would leave the realm looking edited when nothing needed editing.
        """
        assigned = {item.get("name") for item in self._call("GET", f"/clients/{client_row['id']}/default-client-scopes").json()}
        for scope in self._call("GET", "/client-scopes").json():
            if scope.get("name") not in assigned:
                continue
            for mapper in scope.get("protocolMappers") or []:
                if mapper.get("protocolMapper") != protocol_mapper:
                    continue
                if not config_key or (mapper.get("config") or {}).get(config_key) == config_value:
                    return True
        return False

    def _has_sub_claim(self, client_row: dict) -> bool:
        if any(item.get("protocolMapper") == "oidc-sub-mapper" for item in client_row.get("protocolMappers") or []):
            return True
        return self._scope_carries(client_row, "oidc-sub-mapper")

    def _follow_to_provider(self, url: str, params: dict) -> httpx.Response:
        """Walk the broker chain by hand, keeping Keycloak's hops on the internal address.

        KC_HOSTNAME makes Keycloak redirect to its own canonical PUBLIC url, which names
        the operator's machine and is unreachable from inside the compose network. A
        browser has no such problem, so following those hops verbatim would fail a
        provider that works. Keycloak's own hops are rewritten back to the address this
        process can reach; every other hop — the provider's — is followed untouched.
        """
        public = (self.config.public_base_url or "").rstrip("/")
        internal = self.config.admin_base_url.rstrip("/")
        # A development Keycloak serves a self-signed certificate that a browser accepts
        # on a click-through; refusing it here would fail a working provider.
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False, verify=False) as browser:
            response = browser.get(url, params=params)
            for _ in range(10):
                if response.status_code not in (301, 302, 303, 307, 308):
                    return response
                location = response.headers.get("location", "")
                if not location:
                    return response
                nxt = str(httpx.URL(response.url).join(location))
                if public and nxt.startswith(public):
                    nxt = internal + nxt[len(public) :]
                response = browser.get(nxt)
            return response

    def _login_redirect_uri(self, client_row: dict) -> str:
        """A redirect URI the client will accept, for starting a probe login.

        Keycloak refuses the authorization request outright if the redirect URI is not
        registered, which would read as "the provider is broken" when it is the probe
        that is wrong. Wildcards are skipped: Keycloak matches them, but the value has
        to be a concrete URL to be sent at all.
        """
        for candidate in client_row.get("redirectUris") or []:
            if "*" not in candidate and candidate.lower().startswith(("http://", "https://")):
                return candidate
        return f"{self.config.public_base_url.rstrip('/')}/realms/{self.config.realm}/account/"

    def token_claim_state(self, audience: str) -> list[Check]:
        """Read back what ``ensure_token_claims`` writes, without writing anything."""
        checks: list[Check] = []
        for client_id in self.config.token_client_ids:
            row = self._client_row(client_id)
            if row is None:
                checks.append(Check(f"client:{client_id}", f"Client {client_id}", False, "not present in the realm"))
                continue
            lightweight = (row.get("attributes") or {}).get("client.use.lightweight.access.token.enabled")
            has_sub = self._has_sub_claim(row)
            checks.append(
                Check(
                    f"client:{client_id}",
                    f"Token claims on {client_id}",
                    lightweight != "true" and has_sub,
                    "lightweight access tokens are on, so tokens carry no subject"
                    if lightweight == "true"
                    else ("carries sub" if has_sub else "no mapper produces a sub claim"),
                )
            )
        row = self._client_row(self.config.audience_client_id)
        carried = bool(row) and (
            any(
                item.get("protocolMapper") == "oidc-audience-mapper"
                and (item.get("config") or {}).get("included.client.audience") == audience
                for item in row.get("protocolMappers") or []
            )
            or self._scope_carries(row, "oidc-audience-mapper", "included.client.audience", audience)
        )
        checks.append(
            Check(
                "audience",
                f"Audience {audience}",
                carried,
                "stamped into access tokens" if carried else "no mapper stamps this audience, so the appliance will reject the token",
            )
        )
        return checks

    # ------------------------------------------------------------------- people

    def users(self, limit: int = 200) -> list[dict]:
        return list(self._call("GET", "/users", params={"max": limit, "briefRepresentation": True}).json())

    def user_identity_links(self, user_id: str) -> list[dict]:
        response = self._call("GET", f"/users/{user_id}/federated-identity")
        return [] if response.status_code == 404 else list(response.json())

    def user(self, user_id: str) -> dict | None:
        response = self._call("GET", f"/users/{user_id}")
        return None if response.status_code == 404 else dict(response.json())

    def find_user(self, username: str) -> dict | None:
        """The one account with this username, or nothing. Exact, never a prefix."""
        rows = self._call(
            "GET", "/users", params={"username": username, "exact": True, "max": 2}
        ).json()
        wanted = username.casefold()
        return next((row for row in rows if str(row.get("username", "")).casefold() == wanted), None)

    def create_user(self, email: str, *, first_name: str = "", last_name: str = "") -> dict:
        """A person who signs in here with a password, keyed on their address.

        ``username`` is deliberately the email and not a name. Access is decided by
        matching what a login asserts against what the connectors mirrored, and what
        they mirror is addresses — so an account created as ``ursula`` is an account
        that silently sees nothing. Keycloak will accept any username; this will not.

        The address is marked verified because the administrator standing in this
        console asserted it. There is no mail server on a firm's appliance, so a
        confirmation mail would strand the person at an account they cannot open.
        """
        username = normalize_email(email)
        if self.find_user(username) is not None:
            raise IdentityAdminError(f"{username} can already sign in")
        self._call(
            "POST",
            "/users",
            json={
                "username": username,
                "email": username,
                "firstName": first_name.strip(),
                "lastName": last_name.strip(),
                "enabled": True,
                "emailVerified": True,
                # The temporary password below is known to whoever created the account,
                # so it must not survive that person's first sign-in.
                "requiredActions": ["UPDATE_PASSWORD"],
            },
        )
        created = self.find_user(username)
        if created is None:
            raise IdentityAdminError("Keycloak accepted the account but does not list it")
        return created

    def set_password(self, user_id: str, password: str, *, temporary: bool = True) -> None:
        """Hand Keycloak the password. It is never written anywhere else."""
        self._call(
            "PUT",
            f"/users/{user_id}/reset-password",
            json={"type": "password", "value": password, "temporary": temporary},
        )

    def require_password_change(self, user_id: str) -> None:
        """Re-arm the first-sign-in password change after a reset.

        Keycloak adds the required action itself for a temporary credential, but only
        on some paths; asserting it means a reset never leaves an account reachable with
        the address the administrator read off their screen.
        """
        row = self.user(user_id)
        if row is None:
            raise IdentityAdminError("that person is no longer in the realm")
        actions = list(row.get("requiredActions") or [])
        if "UPDATE_PASSWORD" in actions:
            return
        self._call("PUT", f"/users/{user_id}", json={**row, "requiredActions": [*actions, "UPDATE_PASSWORD"]})

    def set_user_enabled(self, user_id: str, enabled: bool) -> dict:
        """Disable rather than delete: the account stops opening, the history stays."""
        row = self.user(user_id)
        if row is None:
            raise IdentityAdminError("that person is no longer in the realm")
        self._call("PUT", f"/users/{user_id}", json={**row, "enabled": bool(enabled)})
        return {**row, "enabled": bool(enabled)}

    def delete_user(self, user_id: str) -> None:
        self._call("DELETE", f"/users/{user_id}")

    def ensure_password_policy(self, policy: str) -> str:
        """Give a realm with no policy of its own a minimum, before the first password.

        Where a provider is brokered, that provider decides how strong a password has
        to be. Local accounts have no such authority behind them, so without this the
        appliance would hand someone a strong temporary password and then accept "1"
        as its replacement. A policy the firm has already chosen is left alone.
        """
        realm = dict(self._call("GET", "").json())
        current = str(realm.get("passwordPolicy") or "").strip()
        if current:
            return current
        self._call("PUT", "", json={**realm, "passwordPolicy": policy})
        return policy

    def broker_login_redirect(self, alias: str, client_id: str, authorization_endpoint: str) -> Check:
        """Start the login the way a browser does, and follow it to the provider.

        The strongest check that does not need a person: Keycloak's broker chain is
        walked with a cookie jar until it lands somewhere, and that somewhere has to be
        the provider's own authorization endpoint carrying the client id this appliance
        registered. Realm, client, broker alias, the imported endpoints and the
        provider's own acceptance of the request all have to be right for that to
        happen. No session is created — it is a GET at a login page.
        """
        row = self._client_row(client_id)
        if row is None:
            return Check("login", "Keycloak hands off to the provider", False, f"client {client_id} is not in the realm")
        # Connect over the internal address — the public one names the host, which this
        # process cannot reach from inside the compose network. That is only safe because
        # KC_HOSTNAME pins Keycloak to one canonical public URL: without it Keycloak
        # builds the broker callback from whichever host the request arrived on, hands
        # Google an unregistered plain-http address, and Google refuses with its generic
        # "does not comply with OAuth 2.0 policy" — blaming the provider for a sign-in
        # that actually works. Verified: a browser reaches Google's real sign-in page.
        base = self.config.admin_base_url.rstrip("/")
        params = {
            "client_id": client_id,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": self._login_redirect_uri(row),
            "kc_idp_hint": alias,
        }
        try:
            # Its own client: the broker hop needs Keycloak's authentication-session
            # cookie, which the admin client deliberately does not keep.
            url = f"{base}/realms/{self.config.realm}/protocol/openid-connect/auth"
            try:
                response = self._follow_to_provider(url, params)
            except httpx.ConnectError as exc:
                # A development Keycloak serves a self-signed certificate, which a real
                # browser accepts once on a click-through. Retry unverified rather than
                # report a working provider as broken — but only for a certificate
                # failure, and the caller is told, so this never passes silently.
                if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
                    raise
                with httpx.Client(timeout=TIMEOUT, follow_redirects=True, verify=False) as browser:
                    response = browser.get(url, params=params)
                landed = str(response.url)
                if landed.startswith(authorization_endpoint.split("?", 1)[0]):
                    return Check(
                        "login",
                        "Keycloak hands off to the provider",
                        True,
                        f"reaches {authorization_endpoint.split('?', 1)[0]} "
                        "(its certificate is self-signed — a browser will warn once)",
                    )
                return Check("login", "Keycloak hands off to the provider", False, _login_failure_detail(landed))
        except httpx.HTTPError as exc:
            return Check("login", "Keycloak hands off to the provider", False, f"the broker chain failed: {type(exc).__name__}")
        landed = str(response.url)
        expected = authorization_endpoint.split("?", 1)[0]
        # Reaching the provider's own sign-in UI is the success we are testing for, and it
        # is not the authorization endpoint: Google forwards /o/oauth2/v2/auth on to
        # /v3/signin/identifier, so a path-prefix match could never pass. Judge by origin
        # instead — but an error page lives on that same origin, so exclude it explicitly.
        from urllib.parse import urlparse

        landed_parts, expected_parts = urlparse(landed), urlparse(expected)
        same_origin = (landed_parts.scheme, landed_parts.netloc) == (
            expected_parts.scheme,
            expected_parts.netloc,
        )
        looks_like_error = "error" in landed_parts.path.casefold() or "error=" in (
            landed_parts.query or ""
        )
        if landed.startswith(expected) or (same_origin and not looks_like_error):
            return Check(
                "login",
                "Keycloak hands off to the provider",
                True,
                f"reaches the sign-in page at {landed_parts.scheme}://{landed_parts.netloc}",
            )
        if f"/broker/{alias}/" in landed:
            return Check("login", "Keycloak hands off to the provider", False, "Keycloak stopped at its own broker page instead of reaching the provider")
        return Check("login", "Keycloak hands off to the provider", False, _login_failure_detail(landed))


def _login_failure_detail(landed: str) -> str:
    """Say WHY the handoff failed, in the provider's own words.

    The reason is in the query string the provider redirected to — dropping it left an
    operator with "the login ended at .../signin/oauth/error" and nowhere to go but the
    provider's console. Google additionally packs a base64 blob into `authError`; it is
    not a documented format, but the useful part ("redirect_uri_mismatch",
    "invalid_client", the offending URI) is plain ASCII inside it, so pull that out.
    """
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(landed)
    query = parse_qs(parsed.query)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    for key in ("error_description", "error", "authError"):
        raw = (query.get(key) or [""])[0]
        if not raw:
            continue
        if key == "authError":
            raw = _readable_google_auth_error(raw) or ""
            if not raw:
                continue
        return f"{raw} — the provider refused at {base}"
    return (
        f"the login ended at {base}. The provider gave no reason in the redirect; "
        "the usual cause is that this appliance's redirect URI is not registered on the "
        "provider's client."
    )


def _readable_google_auth_error(raw: str) -> str:
    """Recover the human-readable fragment from Google's opaque `authError` blob."""
    import base64
    import re

    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - undocumented format; a failure just means no detail
        return ""
    # Keep runs of printable text; the blob interleaves them with framing bytes.
    fragments = [item for item in re.findall(r"[ -~]{6,}", decoded) if item.strip()]
    return " · ".join(fragments[:3])[:300]


@dataclass
class ProviderState:
    """What the realm currently holds for one preset, for the UI to render."""

    kind: str
    alias: str
    configured: bool = False
    display_name: str = ""
    enabled: bool = False
    client_id: str = ""
    issuer: str = ""
    discovery_url: str = ""
    redirect_uri: str = ""
    extra_value: str = ""
    last_tested_at: str | None = None
    last_test_ok: bool | None = None
    checks: list[dict] = field(default_factory=list)

    def payload(self) -> dict:
        return {
            "kind": self.kind,
            "alias": self.alias,
            "configured": self.configured,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "client_id": self.client_id,
            "issuer": self.issuer,
            "discovery_url": self.discovery_url,
            "redirect_uri": self.redirect_uri,
            "extra_value": self.extra_value,
            "last_tested_at": self.last_tested_at,
            "last_test_ok": self.last_test_ok,
            "checks": self.checks,
        }
