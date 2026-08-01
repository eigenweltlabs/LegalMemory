"""Backup secrets an administrator can set from the admin UI.

These are set in the admin UI and held in the database, encrypted at rest under
``KI_CONNECTOR_CREDENTIAL_KEY`` — the same key, the same primitive and the same envelope
as connector OAuth tokens, which are strictly more sensitive and are stored this way
already.

There is deliberately no environment-variable path. A secret an administrator can set two
ways is a secret nobody can say the current value of, and the environment route means the
person configuring backups on a web page cannot set them at all: it takes editing a
compose file, rebuilding and restarting, and until then the page tells them the key is
missing and refuses to run. ``ki backup-key`` covers the headless case.

**A value is never given back.** Everything above this module can ask *whether* a secret
is set, and for its fingerprint, and can use it — nothing can read it back out through the
API. The one exception is the moment a key is generated, where it is shown once and the
administrator is made to save it, because a backup key that exists only on the machine the
backups protect is not a backup key.
"""

from __future__ import annotations

import base64
import hashlib
import secrets as pysecrets
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

# The secrets this appliance knows how to hold. Named rather than free-form: a secret
# store the UI can put arbitrary keys into is a secret store nobody can audit.
ENCRYPTION_KEY = "encryption_key"
S3_ACCESS_KEY_ID = "s3_access_key_id"
S3_SECRET_ACCESS_KEY = "s3_secret_access_key"

KNOWN: tuple[str, ...] = (ENCRYPTION_KEY, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY)


class BackupSecretError(RuntimeError):
    """A secret could not be stored or read."""


@dataclass(frozen=True)
class SecretStatus:
    name: str
    set: bool
    fingerprint: str | None

    def payload(self) -> dict:
        return {"name": self.name, "set": self.set, "fingerprint": self.fingerprint}


def generate_key() -> str:
    """A fresh 32-byte AES key, base64 for a human to copy.

    Generated here rather than printed as a shell command in the documentation. Asking an
    administrator to run `python -c "import base64,os;..."` to use a backup feature is a
    step at which people either give up or paste in something that is not 32 bytes.
    """
    return base64.urlsafe_b64encode(pysecrets.token_bytes(32)).decode()


def fingerprint(value: str) -> str:
    """Short, non-reversible id, so two appliances can be compared without exchanging keys."""
    return hashlib.blake2b(value.strip().encode("utf-8"), digest_size=8).hexdigest()


def resolve(name: str, session_factory: sessionmaker[Session] | None = None) -> str | None:
    """The value in force for one secret, or None if it has not been set."""
    _require_known(name)
    if session_factory is None:
        return None
    return _read_stored(name, session_factory)


def status(name: str, session_factory: sessionmaker[Session] | None = None) -> SecretStatus:
    """Whether a secret is set and its fingerprint — never its value."""
    _require_known(name)
    stored = _read_stored(name, session_factory) if session_factory else None
    if stored:
        return SecretStatus(name, True, fingerprint(stored))
    return SecretStatus(name, False, None)


def store(name: str, value: str, session_factory: sessionmaker[Session]) -> SecretStatus:
    """Save one secret, encrypted, replacing whatever was there."""
    _require_known(name)
    cleaned = (value or "").strip()
    if not cleaned:
        raise BackupSecretError("a secret cannot be empty")
    if name == ENCRYPTION_KEY:
        _require_valid_key(cleaned)
    from knowledge_index.connectors.runtime.secrets import encrypt_credentials
    from knowledge_index.db.models import BackupSecret

    payload = encrypt_credentials({"value": cleaned})
    with session_factory() as session:
        record = session.get(BackupSecret, name)
        if record is None:
            session.add(
                BackupSecret(name=name, payload=payload, fingerprint=fingerprint(cleaned))
            )
        else:
            record.payload = payload
            record.fingerprint = fingerprint(cleaned)
        session.commit()
    return status(name, session_factory)


def forget(name: str, session_factory: sessionmaker[Session]) -> SecretStatus:
    _require_known(name)
    from knowledge_index.db.models import BackupSecret

    with session_factory() as session:
        record = session.get(BackupSecret, name)
        if record is not None:
            session.delete(record)
            session.commit()
    return status(name, session_factory)


def _require_valid_key(value: str) -> None:
    """Reject a backup key that is not a key, at the moment it is typed.

    Otherwise the mistake surfaces at 2am as a failed backup, or worse, at restore time.
    """
    try:
        raw = base64.urlsafe_b64decode(value)
    except Exception as exc:
        raise BackupSecretError(
            "that is not a valid backup key: it has to be base64. Use Generate to make one."
        ) from exc
    if len(raw) != 32:
        raise BackupSecretError(
            f"a backup key has to decode to 32 bytes for AES-256; that one is {len(raw)}. "
            "Use Generate to make one."
        )


def _read_stored(name: str, session_factory: sessionmaker[Session]) -> str | None:
    from knowledge_index.connectors.runtime.secrets import (
        CredentialCryptoError,
        decrypt_credentials,
    )
    from knowledge_index.db.models import BackupSecret

    try:
        with session_factory() as session:
            record = session.get(BackupSecret, name)
            if record is None:
                return None
            return str(decrypt_credentials(record.payload).get("value") or "") or None
    except CredentialCryptoError:
        # The connector key changed or is missing. Reported by the backup page as "not
        # set" rather than raised: an unreadable secret and an absent one need the same
        # thing from the administrator, which is to type it again.
        return None
    except Exception:  # noqa: BLE001 - an appliance mid-migration has no table yet
        return None


def _require_known(name: str) -> None:
    if name not in KNOWN:
        raise BackupSecretError(f"unknown backup secret: {name!r}")
