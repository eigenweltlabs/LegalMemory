"""Load and store connector credentials.

The only place ciphertext becomes a usable token. Everything above this module deals
in ``Source`` rows; everything below deals in dicts that contain live secrets.

Rotating-refresh providers (Confluence, Dropbox, most Microsoft scopes) invalidate the
old refresh token the moment a new one is issued, so :func:`save` is called from inside
the token provider's refresh path rather than at the end of a sync. A crash mid-sync
must not lose the rotation, or the connection is permanently dead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from knowledge_index.connectors.runtime.secrets import (
    KEY_ENV,
    CredentialCryptoError,
    decrypt_credentials,
    encrypt_credentials,
    key_fingerprint,
)
from knowledge_index.db.models import SourceCredential


def load(session: Session, source_id: str) -> dict:
    """Return decrypted credentials for a source, or ``{}`` if none are stored."""
    row = session.get(SourceCredential, source_id)
    if row is None:
        return {}
    current = key_fingerprint()
    if row.key_fingerprint and row.key_fingerprint != current:
        # Naming both fingerprints turns "decryption failed" into a diagnosis: the row
        # was written under a different key, so the fix is to restore that key or
        # re-authorize the connection — not to debug the ciphertext.
        raise CredentialCryptoError(
            f"credentials for source {source_id} were encrypted with key "
            f"{row.key_fingerprint!r}, but {KEY_ENV} is now {current!r}. Restore the "
            "original key to keep the connection, or re-authorize the connector to "
            "issue fresh credentials under the new key."
        )
    return decrypt_credentials(row.payload)


def save(
    session: Session,
    source_id: str,
    credentials: dict,
    *,
    provider: str | None = None,
) -> None:
    """Encrypt and upsert credentials for a source."""
    expires_at = _expiry(credentials)
    row = session.get(SourceCredential, source_id)
    payload = encrypt_credentials(credentials)
    fingerprint = key_fingerprint()
    if row is None:
        session.add(
            SourceCredential(
                source_id=source_id,
                payload=payload,
                key_fingerprint=fingerprint,
                provider=provider,
                expires_at=expires_at,
            )
        )
    else:
        row.payload = payload
        row.key_fingerprint = fingerprint
        row.expires_at = expires_at
        if provider:
            row.provider = provider
    session.flush()


def delete(session: Session, source_id: str) -> None:
    row = session.get(SourceCredential, source_id)
    if row is not None:
        session.delete(row)
        session.flush()


def _expiry(credentials: dict) -> datetime | None:
    """Best-effort access-token expiry, for surfacing connection health in the UI.

    This is informational only — the token provider refreshes on its own schedule and
    never consults this value.
    """
    expires_in = credentials.get("expires_in")
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return datetime.now(UTC) + timedelta(seconds=seconds) if seconds > 0 else None
