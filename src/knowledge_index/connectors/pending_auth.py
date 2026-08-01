"""Connections whose OAuth handshake has not completed yet.

The sibling of :mod:`knowledge_index.connectors.credentials`: that module stores secrets
for a source that exists, this one stores them for a source that does not exist *yet* and
may never. An operator who abandons the browser handshake — closes the tab, cannot find
the tenant admin, is refused by the provider — must leave the appliance exactly as they
found it. Nothing here ever becomes visible in the connections list; it either turns into
a real ``Source`` at the callback or it lapses.

Both guarantees are enforced at lookup rather than by a background job: :func:`claim`
only ever sees live records, so a stale ``state`` cannot be redeemed even if nothing has
swept the table since it lapsed. :func:`sweep` exists so the table does not grow, not so
that expiry works.
"""

from __future__ import annotations

import secrets as stdlib_secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_index.connectors.runtime.secrets import (
    KEY_ENV,
    CredentialCryptoError,
    decrypt_credentials,
    encrypt_credentials,
    key_fingerprint,
)
from knowledge_index.db.models import PendingSourceAuthorization

# Long enough for an operator to switch to the provider, find whoever can consent, and
# sign in; short enough that an authorize URL captured from a browser history or a shared
# screen is worthless by the time anyone acts on it.
TTL = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(UTC)


def create(
    session: Session,
    *,
    state: str,
    kind: str,
    display_name: str,
    project_id: str | None,
    config: dict,
    sync_policy: dict,
    provider: str,
    provider_connection_id: str | None,
    secret_values: dict,
) -> PendingSourceAuthorization:
    """Record an intent to connect, and the secrets needed to finish it.

    ``secret_values`` carries the firm's OAuth client id and secret plus the PKCE
    verifier. It is encrypted before it reaches the database and is never written in the
    clear, not even for the minutes the handshake is in flight.
    """
    sweep(session)
    row = PendingSourceAuthorization(
        state=state,
        kind=kind,
        display_name=display_name,
        project_id=project_id,
        config=config,
        sync_policy=sync_policy,
        provider=provider,
        provider_connection_id=provider_connection_id,
        payload=encrypt_credentials(secret_values),
        key_fingerprint=key_fingerprint(),
        expires_at=_now() + TTL,
    )
    session.add(row)
    session.flush()
    return row


def claim(session: Session, state: str) -> tuple[PendingSourceAuthorization, dict] | None:
    """Return the live handshake matching ``state`` with its decrypted secrets.

    The candidate set is filtered on expiry in SQL, so a lapsed record is invisible here
    whether or not :func:`sweep` has run. The comparison itself stays constant-time: the
    callback is unauthenticated by necessity, and ``state`` is the only thing standing
    between a stranger and a connection being created.
    """
    live = select(PendingSourceAuthorization).where(PendingSourceAuthorization.expires_at > _now())
    for row in session.scalars(live):
        if stdlib_secrets.compare_digest(row.state, state):
            return row, _decrypt(row)
    return None


def discard(session: Session, row: PendingSourceAuthorization) -> None:
    """Drop a handshake without materialising a source. Used on every failure path."""
    session.delete(row)
    session.flush()


def sweep(session: Session) -> int:
    """Delete lapsed handshakes; return how many went. Housekeeping, not enforcement."""
    result = session.execute(
        sql_delete(PendingSourceAuthorization).where(
            PendingSourceAuthorization.expires_at <= _now()
        )
    )
    return int(result.rowcount or 0)


def _decrypt(row: PendingSourceAuthorization) -> dict[str, Any]:
    current = key_fingerprint()
    if row.key_fingerprint and row.key_fingerprint != current:
        # Naming both fingerprints turns "decryption failed" into a diagnosis. Unlike a
        # source credential there is nothing to restore here: start the connection again.
        raise CredentialCryptoError(
            f"the pending authorization for {row.kind!r} was encrypted with key "
            f"{row.key_fingerprint!r}, but {KEY_ENV} is now {current!r}. Start the "
            "connection again under the current key."
        )
    return decrypt_credentials(row.payload)
