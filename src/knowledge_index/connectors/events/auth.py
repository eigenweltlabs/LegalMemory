"""Obtain a source OAuth token without constructing or crawling its connector."""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.connectors import credentials as credential_store
from knowledge_index.connectors.registry import get
from knowledge_index.connectors.runtime import oauth as oauth_runtime
from knowledge_index.connectors.runtime.errors import SourceAuthError
from knowledge_index.connectors.runtime.secrets import CredentialCryptoError
from knowledge_index.db.models import Source, SourceCredential

TOKEN_MARGIN = timedelta(minutes=5)


# Connector kinds whose stored credentials belong to a Microsoft Entra application.
# Any of them may hold the client secret the Event Hubs consumer authenticates with —
# a compact deployment registers one Entra app for SharePoint, OneDrive and events.
MICROSOFT_GRAPH_KINDS = ("sharepoint_online", "onedrive")


def application_client_secret(
    session_factory: sessionmaker[Session],
    *,
    client_id: str,
    secret_env: str,
) -> str:
    """Resolve a broker client secret without duplicating connector secrets.

    A deployment may use a dedicated Event Hubs receiver application, in which case its
    secret comes from ``secret_env``. For a compact on-prem installation the same Entra
    application commonly authorizes SharePoint or OneDrive and receives Event Hubs
    messages. Its client secret already lives encrypted in ``source_credentials``; when
    the configured client id matches, reuse that value rather than requiring a second
    plaintext copy in ``.env``. A dedicated environment secret always wins.
    """

    explicit = os.environ.get(secret_env, "").strip()
    if explicit:
        return explicit
    expected = client_id.strip()
    if not expected:
        return ""
    with session_factory() as session:
        source_ids = session.scalars(
            select(Source.id)
            .join(SourceCredential, SourceCredential.source_id == Source.id)
            .where(
                Source.kind.in_(MICROSOFT_GRAPH_KINDS),
                Source.status.in_(["active", "error"]),
            )
            .order_by(Source.created_at)
        ).all()
        for source_id in source_ids:
            try:
                stored = credential_store.load(session, source_id)
            except CredentialCryptoError:
                continue
            if str(stored.get("client_id") or "").strip() != expected:
                continue
            secret = str(stored.get("client_secret") or "").strip()
            if secret:
                return secret
    return ""


def source_access_token(session_factory: sessionmaker[Session], source_id: str) -> str:
    """Return a current token and durably keep any rotated refresh token.

    Subscription renewal is infrequent, so the stored access token is reused until it is
    genuinely near expiry. That also avoids needlessly racing a connector worker on a
    rotating Microsoft refresh token.
    """
    with session_factory() as session:
        _advisory_xact_lock(session, f"source-oauth-refresh:{source_id}")
        source = session.get(Source, source_id)
        if source is None:
            raise SourceAuthError(f"event source {source_id} no longer exists")
        spec = get(source.kind)
        if not spec.oauth_provider:
            raise SourceAuthError(f"{source.kind} has no OAuth provider for event renewal")
        row = session.get(SourceCredential, source_id)
        stored = credential_store.load(session, source_id)
        token = str(stored.get("access_token") or "")
        if (
            token
            and row is not None
            and row.expires_at is not None
            and _aware(row.expires_at) > datetime.now(UTC) + TOKEN_MARGIN
        ):
            return token

        refresh = str(stored.get("refresh_token") or "")
        client_id = str(stored.get("client_id") or "")
        client_secret = str(stored.get("client_secret") or "")
        if not refresh or not client_id or not client_secret:
            raise SourceAuthError(
                f"{source.display_name} cannot renew events: its OAuth refresh token or "
                "client credentials are missing; re-authorize the connection"
            )
        refreshed = asyncio.run(
            oauth_runtime.refresh_token(
                oauth_runtime.get_provider(spec.oauth_provider),
                refresh_token=refresh,
                client_id=client_id,
                client_secret=client_secret,
            )
        )
        merged = {**stored, **refreshed}
        credential_store.save(session, source_id, merged, provider=source.kind)
        session.commit()
        return str(refreshed["access_token"])


def _advisory_xact_lock(session: Session, key: str) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    lock_id = int.from_bytes(
        hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
    session.execute(select(func.pg_advisory_xact_lock(lock_id)))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
