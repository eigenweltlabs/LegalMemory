"""Envelope encryption for connector credentials.

OAuth refresh tokens are long-lived keys to a law firm's entire document estate, so
they never land in ``Source.config`` (documented as non-secret) and never in a log
line.  They live in ``source_credentials.payload`` as AES-256-GCM ciphertext under a
key supplied by the operator out of band.

The key is required in production.  A missing key is a hard error rather than a
silent fallback to plaintext: a deployment that quietly stored refresh tokens in the
clear would be worse than one that refuses to start.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12
KEY_ENV = "KI_CONNECTOR_CREDENTIAL_KEY"


class CredentialCryptoError(RuntimeError):
    """The credential key is missing/malformed, or a payload failed to decrypt."""


def _load_key(explicit: str | None = None) -> bytes:
    raw = explicit if explicit is not None else os.environ.get(KEY_ENV, "")
    if not raw:
        raise CredentialCryptoError(
            f"{KEY_ENV} is not set. Generate one with "
            "`python -c \"import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())\"` "
            "and supply it to the app, worker and watcher containers. Connector "
            "credentials are never stored unencrypted."
        )
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise CredentialCryptoError(f"{KEY_ENV} is not valid base64: {exc}") from exc
    if len(key) != 32:
        raise CredentialCryptoError(
            f"{KEY_ENV} must decode to 32 bytes for AES-256-GCM, got {len(key)}"
        )
    return key


def encrypt_credentials(credentials: dict, *, key: str | None = None) -> str:
    """Encrypt a credential dict into a base64 ``nonce || ciphertext`` blob."""
    aesgcm = AESGCM(_load_key(key))
    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(credentials, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(nonce + aesgcm.encrypt(nonce, plaintext, None)).decode()


def decrypt_credentials(blob: str, *, key: str | None = None) -> dict:
    """Reverse :func:`encrypt_credentials`. Raises on tamper or wrong key."""
    aesgcm = AESGCM(_load_key(key))
    try:
        raw = base64.urlsafe_b64decode(blob)
    except Exception as exc:
        raise CredentialCryptoError(f"credential payload is not valid base64: {exc}") from exc
    if len(raw) <= NONCE_BYTES:
        raise CredentialCryptoError("credential payload is truncated")
    try:
        plaintext = aesgcm.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], None)
    except InvalidTag as exc:
        raise CredentialCryptoError(
            "credential payload failed authentication — wrong "
            f"{KEY_ENV} for this database, or the row was tampered with"
        ) from exc
    loaded = json.loads(plaintext.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise CredentialCryptoError("credential payload did not decode to an object")
    return loaded


def key_fingerprint(*, key: str | None = None) -> str:
    """Short, non-reversible key id so operators can tell two keys apart in logs."""
    import hashlib

    return hashlib.blake2b(_load_key(key), digest_size=4).hexdigest()
