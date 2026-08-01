"""The MCP endpoint as an OAuth 2.1 resource server.

Covers the handshake an ordinary MCP client performs — 401 challenge, protected
resource metadata, bearer token — and the two ways it must refuse: a token minted for
somebody else's resource, and a caller who just asserts an identity in a header.

Tokens here are signed with a throwaway key and the JWKS lookup is redirected to it, so
the assertions are about this appliance's validation and scoping rules rather than about
Keycloak. The same flow is exercised against a live Keycloak in the operator guide.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index import auth as auth_module
from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.db.models import (
    AuditEvent,
    Blob,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Matter,
    Project,
    Source,
    SourceGroupMember,
    SourceObject,
    SourceObjectGrant,
)
from knowledge_index.web.app import create_app

ISSUER = "https://idp.kanzlei.example/realms/knowledge-index"
RESOURCE = "https://ki.kanzlei.example/mcp"
METADATA_URL = "https://ki.kanzlei.example/.well-known/oauth-protected-resource/mcp"
LITIGATOR = "lit.user@kanzlei.example"
CORPORATE = "corp.user@kanzlei.example"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the test key wherever the resolver would fetch the IdP's JWKS."""

    class _Stub:
        def get_signing_key_from_jwt(self, token: str):  # noqa: ARG002 - one key, no kid
            return type("Key", (), {"key": _KEY.public_key()})()

    monkeypatch.setattr(auth_module, "_jwks_client", lambda jwks_url: _Stub())


def mint(
    username: str,
    *,
    audience: str = RESOURCE,
    issuer: str = ISSUER,
    subject: str = "0a402e22-826a-4b22-8465-2a32805ad582",
    expires_in: int = 300,
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "preferred_username": username,
            "email": username,
            "iat": now,
            "exp": now + timedelta(seconds=expires_in),
        },
        _KEY,
        algorithm="RS256",
    )


def oauth_app(
    factory: sessionmaker[Session], tmp_path: Path, *, allow_trusted_header: bool = False
) -> TestClient:
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    config.security.oidc_issuer = ISSUER
    config.security.mcp_resource = RESOURCE
    config.security.mcp_allow_trusted_header = allow_trusted_header
    # Deliberately left on: the MCP endpoint must not inherit the global mode.
    config.security.auth_mode = "trusted_header"
    store.save(config)
    return TestClient(create_app(factory, store))


def tools_list(client: TestClient, headers: dict[str, str]):
    return client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"accept": "application/json, text/event-stream", **headers},
    )


def call_tool(client: TestClient, token: str, name: str, arguments: dict | None = None):
    response = client.post(
        "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        headers={
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {token}",
        },
    )
    assert response.status_code == 200, response.text
    structured = response.json()["result"]["structuredContent"]
    # FastMCP wraps a non-object return in {"result": …} and passes objects through.
    return structured["result"] if set(structured) == {"result"} else structured


def test_unauthenticated_mcp_returns_the_challenge_that_starts_a_login(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client = oauth_app(factory, tmp_path)
    with client:
        response = tools_list(client, {})
    assert response.status_code == 401
    # A client that cannot find resource_metadata here has no way to discover where to
    # sign in, and reports a connection failure instead of opening a login window.
    assert response.headers["www-authenticate"] == (f'Bearer resource_metadata="{METADATA_URL}"')


def test_protected_resource_metadata_matches_rfc_9728(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client = oauth_app(factory, tmp_path)
    with client:
        # RFC 9728 §3.1 puts the document under the resource's own path.
        document = client.get("/.well-known/oauth-protected-resource/mcp")
        bare = client.get("/.well-known/oauth-protected-resource")
        alias = client.get("/.well-known/oauth-authorization-server", follow_redirects=False)

    assert document.status_code == 200
    body = document.json()
    assert body["resource"] == RESOURCE
    assert body["authorization_servers"] == [ISSUER]
    assert body["bearer_methods_supported"] == ["header"]
    # The scope that carries the audience mapper has to be advertised, or a client that
    # self-registers never asks for it and its tokens are refused here.
    assert "knowledge-index-mcp" in body["scopes_supported"]
    assert bare.status_code == 200 and bare.json() == body
    # Metadata is read before the client has a token, so it cannot be behind the gate.
    assert "www-authenticate" not in document.headers
    assert alias.status_code == 307
    assert alias.headers["location"].startswith(ISSUER)


def test_trusted_header_cannot_authenticate_mcp_when_secure(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client = oauth_app(factory, tmp_path)
    with client:
        response = tools_list(client, {"x-ki-principals": "user:intruder,role:admin"})
    # The global auth_mode is trusted_header and the header names an admin; MCP still
    # refuses, because anyone who reaches the port could send exactly this.
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


def test_trusted_header_works_only_when_the_deployment_asks_for_it(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client = oauth_app(factory, tmp_path, allow_trusted_header=True)
    with client:
        response = tools_list(client, {"x-ki-principals": "user:developer"})
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        (mint(LITIGATOR, audience="some-other-application"), "audience names another resource"),
        (
            mint(LITIGATOR, audience="https://ki.kanzlei.example/mcp/"),
            "trailing slash is a different resource",
        ),
        (
            mint(LITIGATOR, issuer="https://attacker.example/realms/x"),
            "issuer is not the firm's IdP",
        ),
        (mint(LITIGATOR, expires_in=-30), "expired"),
        ("not-a-token", "unparseable"),
    ],
)
def test_tokens_that_do_not_name_this_appliance_are_refused(
    factory: sessionmaker[Session], tmp_path: Path, token: str, reason: str
) -> None:
    client = oauth_app(factory, tmp_path)
    with client:
        response = tools_list(client, {"authorization": f"Bearer {token}"})
    assert response.status_code == 401, reason
    assert 'error="invalid_token"' in response.headers["www-authenticate"]


def test_a_refused_token_reaches_the_audit_ledger_but_a_first_contact_does_not(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Someone presenting bad credentials is a security event; discovery is not.

    Auditing the tokenless probe too would fill the ledger with the normal opening move
    of every handshake and bury the entry a partner would actually want to see.
    """

    client = oauth_app(factory, tmp_path)
    with client:
        tools_list(client, {})
        tools_list(client, {"authorization": f"Bearer {mint(LITIGATOR, audience='elsewhere')}"})

    with factory() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "mcp.authenticate")
        ).all()
    assert [(event.outcome, event.actor_principals) for event in events] == [("denied", [])]


def test_a_rejected_token_never_falls_back_to_the_development_header(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Even with the escape hatch on, a presented token is the answer — good or bad."""

    client = oauth_app(factory, tmp_path, allow_trusted_header=True)
    with client:
        response = tools_list(
            client,
            {
                "authorization": f"Bearer {mint(LITIGATOR, audience='elsewhere')}",
                "x-ki-principals": "user:intruder,role:admin",
            },
        )
    assert response.status_code == 401


def _seed_mirrored_estate(session: Session) -> None:
    """Two SharePoint groups, one document each, and the memberships that name them.

    Mirrors production: the source shares documents with groups and reports members by
    email, while the token authenticates by subject. Only the username claim bridges the
    two, which is exactly what this test has to keep honest.
    """

    project = Project(id="p-1", key="P-1", name="Estate", status="active")
    source = Source(
        id="s-1",
        project_id=project.id,
        kind="sharepoint_online",
        display_name="Firm SharePoint",
        provider="native",
        config={},
    )
    session.add_all([project, source])
    session.flush()
    matter = Matter(id="m-1", project_id=project.id, reference_numbers=["M-1"], title="Matter")
    session.add(matter)
    session.flush()

    for index, (owner, group) in enumerate(
        [(LITIGATOR, "entra:litigation-guid"), (CORPORATE, "entra:corporate-guid")]
    ):
        session.add(Blob(content_hash=f"hash-{index}", size_bytes=16))
        session.flush()
        source_object = SourceObject(
            id=f"so-{index}",
            source_id=source.id,
            external_id=f"ext-{index}",
            path=f"M-1/file-{index}.docx",
            name=f"file-{index}.docx",
            container="M-1",
            content_hash=f"hash-{index}",
        )
        session.add(source_object)
        session.flush()
        document = Document(
            id=f"d-{index}",
            project_id=project.id,
            matter_id=matter.id,
            title=f"Document for {owner}",
            doc_type="contract",
        )
        session.add(document)
        session.flush()
        version = DocumentVersion(
            id=f"v-{index}",
            document_id=document.id,
            content_hash=f"hash-{index}",
            ordinal=1,
            status="final",
        )
        session.add(version)
        session.flush()
        document.latest_final_version_id = version.id
        session.add_all(
            [
                DocumentVersionSource(version_id=version.id, source_object_id=source_object.id),
                SourceObjectGrant(
                    source_object_id=source_object.id,
                    principal=f"group:{group}",
                    principal_kind="group",
                    effect="allow",
                ),
                SourceGroupMember(
                    source_id=source.id,
                    group_id=group,
                    member_id=owner,
                    member_type="user",
                ),
            ]
        )
    session.commit()


def test_a_valid_token_sees_exactly_what_the_source_shared_with_that_person(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory() as session:
        _seed_mirrored_estate(session)

    client = oauth_app(factory, tmp_path)
    with client:
        # preview_search_scope is the compiled ACL itself, so it asserts the permission
        # decision rather than whatever a search backend happened to return.
        scopes = {
            who: call_tool(client, mint(who), "preview_search_scope")
            for who in (LITIGATOR, CORPORATE, "temp.contractor@kanzlei.example")
        }
        # And the same identity has to hold when a document is fetched by id.
        reachable = {
            who: call_tool(client, mint(who), "get_document", {"document_id": "d-0"})
            for who in (LITIGATOR, CORPORATE)
        }

    assert set(scopes[LITIGATOR]["document_ids"]) == {"d-0"}
    assert set(scopes[CORPORATE]["document_ids"]) == {"d-1"}
    # A perfectly valid token for somebody the source never shared anything with.
    assert scopes["temp.contractor@kanzlei.example"]["document_ids"] == []
    assert reachable[LITIGATOR]["citations"][0]["document"]["id"] == "d-0"
    # The corporate lawyer holds a valid token and asks for the litigation document by
    # its exact id; the answer is that no such document exists for her.
    assert reachable[CORPORATE] is None
