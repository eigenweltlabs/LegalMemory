"""Sign-in setup: the realm is configured from this appliance, and the answers are real.

Two things are worth pinning here. The first is that "Test sign-in" reports what the
provider actually said — a wrong client secret has to come back as a failure, not as a
green tick, because a green tick that means nothing is worse than no button. The second
is the join key: a lawyer whose login address differs from the address a connector
mirrored sees no documents and no error, and the alias that fixes it has to be applied
early enough to reach that person's source groups.

The provider is a real HTTP server answering on a real socket, so the discovery
validation and the credential probe exercise the same code path as Google does.
"""

from __future__ import annotations

import base64

import http.server
import json
import socketserver
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import knowledge_index.auth as auth_module
from knowledge_index.auth import IdentityResolver
from knowledge_index.config import AppConfig, IdentityConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.connectors.runtime.secrets import decrypt_credentials, encrypt_credentials
from knowledge_index.db.models import (
    Blob,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    IdentityProviderCredential,
    Source,
    SourceGroupMember,
    SourceObject,
    SourceObjectGrant,
)
from knowledge_index.identity_admin import (
    IdentityAdminError,
    fetch_discovery,
    generate_password,
    identity_provider_payload,
    preset,
    probe_client_credentials,
)
from knowledge_index.permissions import AccessService, configure_access
from knowledge_index.web.app import create_app

ADMIN = {"x-ki-principals": "user:local-admin,role:admin"}
LAWYER = {"x-ki-principals": "user:ursula@firm.de"}

GOOD_CLIENT = ("firm-client", "firm-secret")


def test_google_sso_proxy_prefers_email_over_the_opaque_oidc_subject() -> None:
    config = AppConfig().security
    identity = IdentityResolver(config).resolve(
        {
            "x-auth-request-user": "9f6742e0-opaque-keycloak-subject",
            "x-auth-request-email": "Me@Example-Firm.com",
        }
    )
    assert identity.subject == "me@example-firm.com"
    assert "user:me@example-firm.com" in identity.principals


def test_verified_oidc_email_is_a_source_acl_principal(monkeypatch) -> None:
    class _SigningKey:
        key = "test-key"

    class _Jwks:
        def get_signing_key_from_jwt(self, _token):
            return _SigningKey()

    monkeypatch.setattr(auth_module, "_jwks_client", lambda _url: _Jwks())
    monkeypatch.setattr(
        auth_module.jwt,
        "decode",
        lambda *_args, **_kwargs: {
            "sub": "9f6742e0-opaque-keycloak-subject",
            "preferred_username": "me",
            "email": "Me@Example-Firm.com",
            "email_verified": True,
            "groups": [],
        },
    )
    config = AppConfig().security
    config.auth_mode = "oidc"
    identity = IdentityResolver(config).resolve({"authorization": "Bearer test"})
    assert "user:9f6742e0-opaque-keycloak-subject" in identity.principals
    assert "user:me@example-firm.com" in identity.principals
    assert "username:me@example-firm.com" in identity.principals


@pytest.fixture()
def provider():
    """A real OIDC provider on a real port: discovery plus a spec-compliant token endpoint."""
    state: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def _json(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            base = f"http://127.0.0.1:{state['port']}"
            if self.path.endswith("/.well-known/openid-configuration"):
                self._json(
                    200,
                    {
                        "issuer": base,
                        "authorization_endpoint": f"{base}/authorize",
                        "token_endpoint": f"{base}/token",
                        "jwks_uri": f"{base}/jwks",
                    },
                )
            elif self.path == "/incomplete":
                self._json(200, {"issuer": base})
            else:
                self._json(404, {"error": "not_found"})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0))).decode()
            fields = dict(item.split("=", 1) for item in body.split("&") if "=" in item)
            if (fields.get("client_id"), fields.get("client_secret")) != GOOD_CLIENT:
                self._json(401, {"error": "invalid_client", "error_description": "unknown client"})
            else:
                # The credentials were accepted; the probe's fabricated code was not.
                self._json(400, {"error": "invalid_grant", "error_description": "bad code"})

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    state["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{state['port']}"
    server.shutdown()


def _app(factory: sessionmaker[Session], tmp_path: Path) -> tuple[TestClient, ConfigStore]:
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    # No realm on this deployment: the identity endpoints have to degrade to a message
    # rather than a stack trace, because that is what a fresh install looks like.
    config.identity = IdentityConfig(admin_base_url="http://127.0.0.1:1")
    store.save(config)
    return TestClient(create_app(factory, store)), store


# ----------------------------------------------------------------- provider presets


def test_each_preset_builds_the_discovery_url_from_what_the_admin_types() -> None:
    assert preset("google").discovery_url("") == (
        "https://accounts.google.com/.well-known/openid-configuration"
    )
    assert preset("entra").discovery_url("tenant-guid") == (
        "https://login.microsoftonline.com/tenant-guid/v2.0/.well-known/openid-configuration"
    )
    # An Okta domain gets pasted as a URL at least as often as as a host.
    assert preset("okta").discovery_url("https://firm.okta.com/") == (
        "https://firm.okta.com/.well-known/openid-configuration"
    )
    assert preset("oidc").discovery_url("https://idp.firm.de/.well-known/openid-configuration") == (
        "https://idp.firm.de/.well-known/openid-configuration"
    )


def test_a_missing_or_unusable_provider_detail_is_refused_before_anything_is_written() -> None:
    with pytest.raises(IdentityAdminError):
        preset("entra").discovery_url("")
    with pytest.raises(IdentityAdminError):
        preset("oidc").discovery_url("idp.firm.de/openid-configuration")
    with pytest.raises(IdentityAdminError):
        preset("saml")


# ------------------------------------------------------------------ the real probes


def test_discovery_must_carry_the_endpoints_a_broker_needs(provider: str) -> None:
    document = fetch_discovery(f"{provider}/.well-known/openid-configuration")
    assert document["issuer"] == provider
    assert document["token_endpoint"].endswith("/token")

    # A document that parses but names no endpoints would produce a broker that cannot
    # log anybody in, so it is refused with the missing keys named.
    with pytest.raises(IdentityAdminError) as missing:
        fetch_discovery(f"{provider}/incomplete")
    assert "authorization_endpoint" in str(missing.value)

    with pytest.raises(IdentityAdminError):
        fetch_discovery(f"{provider}/nothing-here")


def test_the_credential_probe_reports_what_the_provider_said(provider: str) -> None:
    """The whole point of the test button: no hardcoded success."""
    endpoint = f"{provider}/token"
    redirect = "http://localhost:8083/realms/knowledge-index/broker/oidc/endpoint"

    good = probe_client_credentials(endpoint, *GOOD_CLIENT, redirect)
    assert good.ok, good.detail
    # The pass is an *authentication* success followed by a grant failure, per RFC 6749.
    assert "invalid_grant" in good.detail

    bad = probe_client_credentials(endpoint, GOOD_CLIENT[0], "yesterdays-secret", redirect)
    assert not bad.ok
    assert "unknown client" in bad.detail

    unreachable = probe_client_credentials("http://127.0.0.1:1/token", *GOOD_CLIENT, redirect)
    assert not unreachable.ok


def test_the_broker_payload_is_built_from_the_providers_own_document(provider: str) -> None:
    document = fetch_discovery(f"{provider}/.well-known/openid-configuration")
    payload = identity_provider_payload(
        "okta",
        display_name="Okta",
        client_id="firm-client",
        client_secret="firm-secret",
        discovery=document,
        scopes="openid profile email",
    )
    assert payload["providerId"] == "oidc"
    assert payload["config"]["authorizationUrl"] == document["authorization_endpoint"]
    assert payload["config"]["jwksUrl"] == document["jwks_uri"]
    # The provider has already verified the address; a second confirmation mail from
    # Keycloak would strand every lawyer at an unverified account.
    assert payload["trustEmail"] is True


# --------------------------------------------------------------------- the endpoints


def test_sign_in_setup_is_administrator_only(factory: sessionmaker[Session], tmp_path: Path) -> None:
    client, _ = _app(factory, tmp_path)
    with client:
        for method, path in (
            ("get", "/api/identity/providers"),
            ("get", "/api/identity/people"),
        ):
            assert getattr(client, method)(path, headers=LAWYER).status_code == 403
        assert client.post(
            "/api/identity/providers",
            json={"kind": "google", "client_id": "a", "client_secret": "b"},
            headers=LAWYER,
        ).status_code == 403
        assert client.post(
            "/api/identity/aliases",
            json={"principal": "user:a@firm.de", "alias": "user:b@firm.de"},
            headers=LAWYER,
        ).status_code == 403


def test_the_redirect_uri_is_offered_before_anything_is_configured(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """It has to be registered at the provider first, so it cannot wait on setup."""
    client, _ = _app(factory, tmp_path)
    with client:
        body = client.get("/api/identity/providers", headers=ADMIN).json()
    assert {item["kind"] for item in body["catalog"]} == {"google", "entra", "okta", "oidc"}
    google = next(item for item in body["providers"] if item["kind"] == "google")
    assert google["configured"] is False
    assert google["redirect_uri"].endswith("/realms/knowledge-index/broker/google/endpoint")
    # An unreachable realm is a message, not a 500.
    assert body["realm_error"]


def test_a_provider_the_credentials_do_not_open_is_never_written(
    factory: sessionmaker[Session], tmp_path: Path, provider: str
) -> None:
    client, _ = _app(factory, tmp_path)
    with client:
        response = client.post(
            "/api/identity/providers",
            json={
                "kind": "oidc",
                "client_id": "firm-client",
                "client_secret": "yesterdays-secret",
                "extra": f"{provider}/.well-known/openid-configuration",
            },
            headers=ADMIN,
        )
    assert response.status_code == 400
    assert "unknown client" in response.json()["detail"]
    with factory() as session:
        assert session.get(IdentityProviderCredential, "oidc") is None


def test_a_client_secret_is_never_stored_or_returned_in_the_clear(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key is set here rather than inherited from whatever the shell happens to export.
    # Without it this test passed or failed according to the developer's environment, which
    # is the one thing a test asserting a secret never reaches the clear must not do.
    monkeypatch.setenv(
        "KI_CONNECTOR_CREDENTIAL_KEY", base64.urlsafe_b64encode(b"i" * 32).decode()
    )
    with factory() as session:
        session.add(
            IdentityProviderCredential(
                alias="okta",
                kind="okta",
                client_id="firm-client",
                payload=encrypt_credentials({"client_secret": "firm-secret"}),
                key_fingerprint="deadbeef",
                discovery_url="https://firm.okta.com/.well-known/openid-configuration",
            )
        )
        session.commit()
        row = session.get(IdentityProviderCredential, "okta")
        assert "firm-secret" not in row.payload
        assert decrypt_credentials(row.payload)["client_secret"] == "firm-secret"

    client, _ = _app(factory, tmp_path)
    with client:
        body = client.get("/api/identity/providers", headers=ADMIN).text
    assert "firm-secret" not in body


# ------------------------------------------------------ the join key and its repair


def _mismatched_estate(session: Session) -> None:
    """One document, shared with an Entra group whose only member is u.schmidt@firm.de."""
    source = Source(id="src-1", kind="sharepoint_online", display_name="SharePoint Online")
    session.add(source)
    session.add(
        SourceGroupMember(
            source_id="src-1",
            group_id="entra:group-guid",
            member_id="u.schmidt@firm.de",
            member_type="user",
        )
    )
    obj = SourceObject(id="obj-1", source_id="src-1", external_id="e1", path="/a.docx", name="a.docx")
    blob = Blob(content_hash="c" * 64, size_bytes=8)
    document = Document(id="doc-1", title="Engagement letter")
    session.add_all([obj, blob, document])
    session.flush()
    session.add(
        SourceObjectGrant(
            source_object_id="obj-1", principal="group:entra:group-guid", principal_kind="group"
        )
    )
    version = DocumentVersion(id="ver-1", document_id="doc-1", content_hash=blob.content_hash, ordinal=1)
    session.add(version)
    session.flush()
    session.add(DocumentVersionSource(version_id="ver-1", source_object_id="obj-1"))
    session.commit()


def test_an_address_the_source_does_not_know_is_reported_as_unmatched(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory() as session:
        _mismatched_estate(session)
    client, _ = _app(factory, tmp_path)
    with client:
        body = client.get("/api/identity/people", headers=ADMIN).json()
    # No realm is reachable in this test, so there is no roster — but the source side of
    # the comparison is derived from mirrored rows and must still be there, because it
    # is what the repair form offers as the target.
    assert body["sources_reporting_identities"] == ["SharePoint Online"]
    assert body["source_identities"] == ["u.schmidt@firm.de"]


def test_linking_the_two_addresses_actually_opens_the_documents(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The silent failure, and its repair, end to end.

    ``ursula@firm.de`` signs in; the source knows her as ``u.schmidt@firm.de`` and
    shares the document with a group she is in under that name. Before the alias she
    sees nothing and no error is raised anywhere.
    """
    with factory() as session:
        _mismatched_estate(session)
    client, store = _app(factory, tmp_path)
    try:
        with client:
            with factory() as session:
                before = AccessService(session).visible_document_ids({"user:ursula@firm.de"})
            assert before == []

            written = client.post(
                "/api/identity/aliases",
                json={"principal": "user:ursula@firm.de", "alias": "user:u.schmidt@firm.de"},
                headers=ADMIN,
            )
            assert written.status_code == 200
            assert store.get().security.principal_aliases == {
                "user:ursula@firm.de": "user:u.schmidt@firm.de"
            }

            with factory() as session:
                after = AccessService(session).visible_document_ids({"user:ursula@firm.de"})
            assert after == ["doc-1"]

            client.delete(
                "/api/identity/aliases?principal=user:ursula@firm.de", headers=ADMIN
            )
            with factory() as session:
                assert AccessService(session).visible_document_ids({"user:ursula@firm.de"}) == []
    finally:
        # configure_access installs process-wide defaults; leaving this test's alias in
        # place would silently widen access for every test that runs after it.
        configure_access(source_acl_mode="sufficient", principal_aliases={})


def test_an_alias_cannot_point_at_itself(factory: sessionmaker[Session], tmp_path: Path) -> None:
    client, _ = _app(factory, tmp_path)
    with client:
        response = client.post(
            "/api/identity/aliases",
            json={"principal": "user:a@firm.de", "alias": "user:A@firm.de"},
            headers=ADMIN,
        )
    assert response.status_code == 400


# --------------------------------------------------- local accounts, no directory
#
# Not every firm has Google, Entra or Okta. The realm below is a real HTTP server
# speaking the subset of Keycloak's admin API this console drives, so the endpoints
# under test make the same calls they make against a real Keycloak.


@pytest.fixture()
def realm():
    """A stand-in Keycloak: master-realm token grant plus the user admin endpoints."""
    users: dict[str, dict] = {}
    settings: dict = {"realm": "knowledge-index", "passwordPolicy": ""}
    credentials: dict[str, dict] = {}
    state: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def _json(self, status: int, body) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> dict:
            raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
            return json.loads(raw or b"{}")

        def _path(self) -> tuple[str, dict]:
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(self.path)
            prefix = "/admin/realms/knowledge-index"
            return parsed.path.removeprefix(prefix), {
                key: value[0] for key, value in parse_qs(parsed.query).items()
            }

        def do_POST(self):
            if self.path.endswith("/protocol/openid-connect/token"):
                self.rfile.read(int(self.headers.get("content-length", 0) or 0))
                return self._json(200, {"access_token": "realm-admin-token", "expires_in": 60})
            path, _ = self._path()
            if path == "/users":
                body = self._body()
                if any(row["username"] == body["username"] for row in users.values()):
                    return self._json(409, {"errorMessage": "User exists"})
                user_id = f"kc-{len(users) + 1}"
                users[user_id] = {**body, "id": user_id}
                self.send_response(201)
                self.send_header("location", f"/users/{user_id}")
                self.send_header("content-length", "0")
                self.end_headers()
                return None
            return self._json(404, {})

        def do_GET(self):
            path, query = self._path()
            if path == "":
                return self._json(200, settings)
            if path == "/users":
                rows = list(users.values())
                if query.get("username"):
                    rows = [row for row in rows if row["username"] == query["username"]]
                return self._json(200, rows)
            if path.endswith("/federated-identity"):
                return self._json(200, [])
            if path.endswith("/credentials"):
                user_id = path.split("/")[2]
                return self._json(200, [credentials[user_id]] if user_id in credentials else [])
            if path.startswith("/users/"):
                user_id = path.split("/")[2]
                return self._json(200, users[user_id]) if user_id in users else self._json(404, {})
            return self._json(404, {})

        def do_PUT(self):
            path, _ = self._path()
            body = self._body()
            if path == "":
                settings.update(body)
                return self._json(204, {})
            if path.endswith("/reset-password"):
                user_id = path.split("/")[2]
                credentials[user_id] = body
                state.setdefault("passwords", []).append(body["value"])
                return self._json(204, {})
            if path.startswith("/users/"):
                user_id = path.split("/")[2]
                users[user_id] = {**users[user_id], **body}
                return self._json(204, {})
            return self._json(404, {})

        def do_DELETE(self):
            path, _ = self._path()
            if path.startswith("/users/"):
                users.pop(path.split("/")[2], None)
                return self._json(204, {})
            return self._json(404, {})

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.update(
        url=f"http://127.0.0.1:{server.server_address[1]}",
        users=users,
        settings=settings,
        credentials=credentials,
    )
    yield state
    server.shutdown()


def _app_with_realm(
    factory: sessionmaker[Session], tmp_path: Path, realm: dict, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, ConfigStore]:
    monkeypatch.setenv("KI_KEYCLOAK_ADMIN_USERNAME", "admin@example.com")
    monkeypatch.setenv("KI_KEYCLOAK_ADMIN_PASSWORD", "realm-admin-password")
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    config.identity = IdentityConfig(admin_base_url=realm["url"], public_base_url=realm["url"])
    store.save(config)
    return TestClient(create_app(factory, store)), store


def test_a_person_created_here_gets_their_email_as_the_realm_username(
    factory: sessionmaker[Session], tmp_path: Path, realm: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join key, written the only way that can work.

    Access is decided by matching the address a login asserts against the address a
    connector mirrored. An account created as ``ursula`` authenticates perfectly and
    matches nothing, so the username is not offered as a field at all — it is the email.
    """
    client, _ = _app_with_realm(factory, tmp_path, realm, monkeypatch)
    with client:
        response = client.post(
            "/api/identity/people",
            json={"email": "  Ursula@Firm.DE ", "first_name": "Ursula", "last_name": "Schmidt"},
            headers=ADMIN,
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["username"] == "ursula@firm.de"

    written = next(iter(realm["users"].values()))
    assert written["username"] == written["email"] == "ursula@firm.de"
    assert written["enabled"] is True
    # The administrator asserted the address; there is no mail server on an appliance,
    # so a confirmation mail would strand the person at an account they cannot open.
    assert written["emailVerified"] is True
    # Whoever created the account has read the password off their screen, so it must
    # not survive the first sign-in.
    assert written["requiredActions"] == ["UPDATE_PASSWORD"]
    assert realm["credentials"][written["id"]]["temporary"] is True


def test_creation_says_whether_any_source_reported_that_address(
    factory: sessionmaker[Session], tmp_path: Path, realm: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mismatch is stated before the account exists, not discovered afterwards.

    An account no connector reported works, raises nothing, and shows nothing. That is
    a support call unless it is said while the administrator is still at the form.
    """
    with factory() as session:
        _mismatched_estate(session)
    client, _ = _app_with_realm(factory, tmp_path, realm, monkeypatch)
    with client:
        known = client.post(
            "/api/identity/people", json={"email": "u.schmidt@firm.de"}, headers=ADMIN
        ).json()
        unknown = client.post(
            "/api/identity/people", json={"email": "new.trainee@firm.de"}, headers=ADMIN
        ).json()
        listed = client.get("/api/identity/people", headers=ADMIN).json()

    assert known["matched_sources"] == ["SharePoint Online"]
    assert known["unmatched_sources"] == []
    assert unknown["matched_sources"] == []
    assert unknown["unmatched_sources"] == ["SharePoint Online"]
    # The same index the form reads while the address is still being typed, so the
    # warning at creation time and the table below it cannot disagree.
    assert listed["source_identity_sources"]["u.schmidt@firm.de"] == ["SharePoint Online"]


def test_an_address_that_cannot_be_a_join_key_is_refused(
    factory: sessionmaker[Session], tmp_path: Path, realm: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _app_with_realm(factory, tmp_path, realm, monkeypatch)
    with client:
        assert client.post("/api/identity/people", json={"email": "ursula"}, headers=ADMIN).status_code == 400
        assert client.post("/api/identity/people", json={"email": "a@b"}, headers=ADMIN).status_code == 400
        created = client.post("/api/identity/people", json={"email": "ursula@firm.de"}, headers=ADMIN)
        assert created.status_code == 201
        again = client.post("/api/identity/people", json={"email": "URSULA@firm.de"}, headers=ADMIN)
    # Same person, different capitalisation: one account, not two.
    assert again.status_code == 409
    assert len(realm["users"]) == 1


def test_the_temporary_password_is_shown_once_and_kept_nowhere(
    factory: sessionmaker[Session], tmp_path: Path, realm: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It goes to Keycloak and to the screen. Not to the database, not to the audit log."""
    client, _ = _app_with_realm(factory, tmp_path, realm, monkeypatch)
    with client:
        created = client.post(
            "/api/identity/people", json={"email": "ursula@firm.de"}, headers=ADMIN
        ).json()
        password = created["temporary_password"]
        listing = client.get("/api/identity/people", headers=ADMIN).text
        reset = client.post(f"/api/identity/people/{created['id']}/password", headers=ADMIN)
        audit = client.get("/api/audit", headers=ADMIN).text

    assert len(password) >= 20
    assert password not in listing
    assert password not in audit
    # A reset issues a new one; the old password is not recoverable from anywhere.
    fresh = reset.json()["temporary_password"]
    assert fresh != password
    assert fresh not in audit
    assert realm["credentials"][created["id"]]["value"] == fresh
    assert realm["credentials"][created["id"]]["temporary"] is True


def test_an_administrator_cannot_lock_themselves_out(
    factory: sessionmaker[Session], tmp_path: Path, realm: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An appliance with no directory has no other way back in."""
    client, _ = _app_with_realm(factory, tmp_path, realm, monkeypatch)
    partner = {"x-ki-principals": "user:partner@firm.de,role:admin"}
    with client:
        mine = client.post("/api/identity/people", json={"email": "partner@firm.de"}, headers=partner).json()
        other = client.post("/api/identity/people", json={"email": "trainee@firm.de"}, headers=partner).json()

        assert client.post(
            f"/api/identity/people/{mine['id']}/enabled", json={"enabled": False}, headers=partner
        ).status_code == 400
        assert client.delete(f"/api/identity/people/{mine['id']}", headers=partner).status_code == 400

        # Somebody else's account is a different matter, and disabling is reversible.
        off = client.post(
            f"/api/identity/people/{other['id']}/enabled", json={"enabled": False}, headers=partner
        )
        assert off.status_code == 200 and realm["users"][other["id"]]["enabled"] is False
        client.post(
            f"/api/identity/people/{other['id']}/enabled", json={"enabled": True}, headers=partner
        )
        assert realm["users"][other["id"]]["enabled"] is True

        assert client.delete(f"/api/identity/people/{other['id']}", headers=partner).status_code == 200

    assert other["id"] not in realm["users"]
    assert mine["id"] in realm["users"]


def test_the_realm_gets_a_minimum_password_policy_before_the_first_local_password(
    factory: sessionmaker[Session], tmp_path: Path, realm: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where there is no provider, nothing else is deciding how strong a password is.

    Without this the appliance issues a strong temporary password and then accepts "1"
    as its replacement. A policy the firm already chose is never overwritten.
    """
    client, _ = _app_with_realm(factory, tmp_path, realm, monkeypatch)
    with client:
        client.post("/api/identity/people", json={"email": "ursula@firm.de"}, headers=ADMIN)
        assert "length(12)" in realm["settings"]["passwordPolicy"]

        realm["settings"]["passwordPolicy"] = "length(20) and specialChars(1)"
        client.post("/api/identity/people", json={"email": "otto@firm.de"}, headers=ADMIN)
    assert realm["settings"]["passwordPolicy"] == "length(20) and specialChars(1)"


def test_managing_people_is_administrator_only(
    factory: sessionmaker[Session], tmp_path: Path, realm: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _app_with_realm(factory, tmp_path, realm, monkeypatch)
    with client:
        assert client.post(
            "/api/identity/people", json={"email": "ursula@firm.de"}, headers=LAWYER
        ).status_code == 403
        assert client.post("/api/identity/people/kc-1/password", headers=LAWYER).status_code == 403
        assert client.post(
            "/api/identity/people/kc-1/enabled", json={"enabled": False}, headers=LAWYER
        ).status_code == 403
        assert client.delete("/api/identity/people/kc-1", headers=LAWYER).status_code == 403
    assert realm["users"] == {}


def test_the_issued_password_is_strong_and_free_of_look_alikes() -> None:
    """It is read off one screen and typed into another, often over a phone."""
    issued = {generate_password() for _ in range(50)}
    assert len(issued) == 50
    for password in issued:
        assert len(password.replace("-", "")) == 20
        assert not set(password) & set("0O1lI")
        assert any(item.islower() for item in password)
        assert any(item.isupper() for item in password)
        assert any(item.isdigit() for item in password)
