"""Streaming AES-256-GCM for backup components.

A backup of this appliance is the firm's entire document estate plus the keys to fetch
more of it, sitting on a NAS or in a bucket that the appliance's own access control does
not reach. So it is encrypted before it leaves, with the same primitive and the same
key-handling convention as connector credentials (``connectors/runtime/secrets.py``) —
AES-256-GCM under a 32-byte key the operator supplies out of band.

Why a framed stream rather than one ``AESGCM.encrypt`` call: components are database
dumps and tar archives, tens or hundreds of gigabytes. A single-shot call needs the whole
plaintext and the whole ciphertext in memory at once, and GCM is in any case not safe to
use on a message that large. So the plaintext is cut into chunks, each sealed
independently, and the framing carries the three things that make a sequence of sealed
chunks as trustworthy as one sealed message:

* every chunk's nonce is ``prefix || counter``, so no nonce repeats under one key;
* every chunk is authenticated against its own index, so chunks cannot be reordered;
* every chunk is authenticated against *which component of which backup* it belongs to,
  so a component cannot be moved between backups or into another component's place;
* the last chunk is authenticated as being last, so a stream cut short — by a full disk,
  a killed transfer, or someone shortening the file — fails to decrypt instead of
  decrypting to a shorter, plausible-looking backup.

That last property is the one that matters most here. A truncated dump that restores
without complaint is the worst outcome this module can have.

The nonce prefix is random per stream and the counter runs within it, so the size of the
prefix is what bounds how many streams may safely share a key. Eight bytes: at roughly a
dozen components a night, four would put a birthday collision — and with it full GCM nonce
reuse, which surrenders both plaintexts and the authentication key — at a few percent over
an appliance's life.

Nothing here reads a stream this module did not write. There is no older format to be
compatible with, so a header whose prefix is the wrong width, or which does not say which
component of which backup it holds, is refused rather than accommodated. Accommodating it
would mean accepting an unnamed component as any component, which is the substitution this
format exists to prevent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"KIBAK1\n"
ALGORITHM = "AES-256-GCM"
KEY_BYTES = 32
# GCM's nonce is 12 bytes, split between a per-stream random prefix and a chunk counter.
# The split is recorded in each stream's header via the length of the prefix, so widening
# the prefix here does not strand streams written under the old one.
NONCE_BYTES = 12
NONCE_PREFIX_BYTES = 8
# 4 MiB of plaintext per chunk: large enough that the 16-byte tag and 4-byte length are
# noise, small enough that encryption never needs a meaningful amount of memory and a
# resumed read does not have to re-buffer much.
CHUNK_BYTES = 4 * 1024 * 1024
_TAG_BYTES = 16
_LENGTH = struct.Struct(">I")


class BackupCryptoError(RuntimeError):
    """The backup key is missing or malformed, or a stream failed to decrypt."""


def load_key(value: str | None) -> bytes:
    """Validate the backup key an administrator set, and return its bytes.

    Takes the value rather than the name of a variable to read it from. The key is set in
    the admin UI and held encrypted in the database; there is no environment variable
    whose name a caller could pass, and there was never a deployment that supplied one.
    """
    raw = (value or "").strip()
    if not raw:
        raise BackupCryptoError(
            "No backup key is set, so backups cannot be encrypted. Open Backup \u2192 "
            "Security and press Generate — the appliance makes one, shows it to you once, "
            "and stores it. Keep the copy it shows you somewhere off this machine: a key "
            "that only exists here cannot open the backups after the day this machine is "
            "gone."
        )
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise BackupCryptoError(f"the backup key is not valid base64: {exc}") from exc
    if len(key) != KEY_BYTES:
        raise BackupCryptoError(
            f"a backup key must decode to {KEY_BYTES} bytes for {ALGORITHM}, got {len(key)}"
        )
    return key


def key_fingerprint(key: bytes) -> str:
    """Short, non-reversible key id, recorded in every manifest.

    So that "I have a key and a backup" can be turned into "I have *the* key for this
    backup" before a restore starts, rather than after it fails to decrypt.
    """
    return hashlib.blake2b(key, digest_size=8).hexdigest()


def header_for(key: bytes, *, chunk_bytes: int = CHUNK_BYTES, context: dict) -> dict:
    """The public parameters of one encrypted stream.

    ``context`` names what is being sealed — which component of which backup — and is
    required. It is not a secret and it is not needed in order to decrypt; it is here so
    that it lands inside the value every chunk is authenticated against, which is what
    stops a component being moved between backups or into another component's place by
    somebody who can write to the destination but cannot forge a tag.
    """
    if not context:
        raise BackupCryptoError("a sealed component must say which component of which backup it is")
    return {
        "algorithm": ALGORITHM,
        "chunk_bytes": int(chunk_bytes),
        "nonce_prefix": base64.b64encode(os.urandom(NONCE_PREFIX_BYTES)).decode(),
        "key_fingerprint": key_fingerprint(key),
        "context": {str(name): str(value) for name, value in sorted(context.items())},
    }


def encrypt_stream(
    source: BinaryIO,
    target: BinaryIO,
    key: bytes,
    *,
    chunk_bytes: int = CHUNK_BYTES,
    context: dict,
) -> int:
    """Seal ``source`` into ``target``; return the number of bytes written.

    An empty input still produces one (empty, final) chunk, so a zero-length component is
    distinguishable from a stream that was cut before it began.
    """
    aesgcm = AESGCM(key)
    header = header_for(key, chunk_bytes=chunk_bytes, context=context)
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    binding = hashlib.sha256(header_bytes).digest()
    nonce_prefix = base64.b64decode(header["nonce_prefix"])

    written = target.write(MAGIC) + target.write(_LENGTH.pack(len(header_bytes)))
    written += target.write(header_bytes)

    counter = 0
    pending = source.read(chunk_bytes)
    while True:
        # Read ahead so the final chunk can be *labelled* final while it is being sealed.
        # Discovering the end afterwards would mean a trailer, and a trailer is exactly
        # the thing a truncation removes.
        following = source.read(chunk_bytes)
        final = not following
        sealed = aesgcm.encrypt(
            _nonce(nonce_prefix, counter), pending, _aad(binding, counter, final)
        )
        written += target.write(_LENGTH.pack(len(sealed))) + target.write(sealed)
        if final:
            break
        pending = following
        counter += 1
    return written


class EncryptingReader:
    """A readable file object that seals ``source`` on demand.

    The destinations pull — ``shutil.copyfileobj`` for a mount, ``upload_fileobj`` for
    S3 — so encryption has to be able to answer ``read(n)`` rather than being handed a
    sink to write into. Framing it this way is what lets a component go from pg_dump's
    output straight to the destination without a ciphertext copy on disk: for a firm whose
    blob store is a few hundred gigabytes, the second copy is the difference between a
    backup that runs and one that fills the staging disk.

    Produces byte-for-byte what :func:`encrypt_stream` produces for the same input, key
    and nonce prefix; the two are covered by the same round-trip tests.
    """

    def __init__(
        self,
        source: BinaryIO,
        key: bytes,
        *,
        chunk_bytes: int = CHUNK_BYTES,
        context: dict,
    ) -> None:
        self._source = source
        self._aesgcm = AESGCM(key)
        self._chunk_bytes = chunk_bytes
        header = header_for(key, chunk_bytes=chunk_bytes, context=context)
        header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._binding = hashlib.sha256(header_bytes).digest()
        self._nonce_prefix = base64.b64decode(header["nonce_prefix"])
        self._buffer = bytearray(MAGIC + _LENGTH.pack(len(header_bytes)) + header_bytes)
        self._counter = 0
        self._pending: bytes | None = None
        self._started = False
        self._finished = False

    def read(self, size: int = -1) -> bytes:
        while not self._finished and (size < 0 or len(self._buffer) < size):
            self._seal_one()
        if size < 0 or size >= len(self._buffer):
            out = bytes(self._buffer)
            self._buffer.clear()
            return out
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    def _seal_one(self) -> None:
        if not self._started:
            self._pending = self._source.read(self._chunk_bytes)
            self._started = True
        following = self._source.read(self._chunk_bytes)
        final = not following
        sealed = self._aesgcm.encrypt(
            _nonce(self._nonce_prefix, self._counter),
            self._pending or b"",
            _aad(self._binding, self._counter, final),
        )
        self._buffer += _LENGTH.pack(len(sealed)) + sealed
        if final:
            self._finished = True
            return
        self._pending = following
        self._counter += 1


def decrypt_stream(
    source: BinaryIO, target: BinaryIO, key: bytes, *, expect_context: dict
) -> int:
    """Open a sealed stream into ``target``; return the plaintext byte count.

    Raises ``BackupCryptoError`` on a wrong key, a tampered chunk, a reordered chunk, a
    stream that ends before its final chunk, or a component that is not the one the caller
    asked for.
    """
    if source.read(len(MAGIC)) != MAGIC:
        raise BackupCryptoError(
            "this file is not an encrypted knowledge-index backup component (bad header). "
            "If the backup was written unencrypted, restore it without a key."
        )
    header_bytes = _read_framed(source, "header")
    try:
        header = json.loads(header_bytes)
    except ValueError as exc:
        raise BackupCryptoError(f"backup header is not valid JSON: {exc}") from exc
    if header.get("algorithm") != ALGORITHM:
        raise BackupCryptoError(f"unsupported backup algorithm: {header.get('algorithm')!r}")
    expected = header.get("key_fingerprint")
    actual = key_fingerprint(key)
    if expected and expected != actual:
        raise BackupCryptoError(
            f"this component was encrypted under key {expected}, but the key supplied is "
            f"{actual}. Restoring needs the key the backup was written with; there is no "
            "recovery path from the wrong one."
        )
    _check_context(header, expect_context)
    binding = hashlib.sha256(header_bytes).digest()
    try:
        nonce_prefix = base64.b64decode(header.get("nonce_prefix", ""))
    except Exception as exc:
        raise BackupCryptoError(f"backup header has a malformed nonce prefix: {exc}") from exc
    if len(nonce_prefix) != NONCE_PREFIX_BYTES:
        raise BackupCryptoError(
            f"backup header has a {len(nonce_prefix)}-byte nonce prefix; this format uses "
            f"{NONCE_PREFIX_BYTES}. The component was not written by this appliance."
        )

    aesgcm = AESGCM(key)
    plaintext_bytes = 0
    counter = 0
    while True:
        sealed = _read_framed(source, f"chunk {counter}", allow_eof=True)
        if sealed is None:
            raise BackupCryptoError(
                f"encrypted stream ended after {counter} chunk(s) without a final chunk — "
                "the component is truncated. Do not restore from it."
            )
        if len(sealed) < _TAG_BYTES:
            raise BackupCryptoError(f"chunk {counter} is too short to be authenticated")
        nonce = _nonce(nonce_prefix, counter)
        # A chunk is either the last one or not, and the tag proves which. Trying the
        # final-flag first costs one failed open on every non-final chunk, so try the
        # common case first and fall back.
        plaintext, final = _open_chunk(aesgcm, nonce, sealed, binding, counter)
        target.write(plaintext)
        plaintext_bytes += len(plaintext)
        if final:
            break
        counter += 1
    trailing = source.read(1)
    if trailing:
        raise BackupCryptoError(
            "encrypted stream continues past its final chunk — the file has been appended "
            "to or two backups have been concatenated"
        )
    return plaintext_bytes


def _open_chunk(
    aesgcm: AESGCM, nonce: bytes, sealed: bytes, binding: bytes, counter: int
) -> tuple[bytes, bool]:
    for final in (False, True):
        try:
            return aesgcm.decrypt(nonce, sealed, _aad(binding, counter, final)), final
        except InvalidTag:
            continue
    raise BackupCryptoError(
        f"chunk {counter} failed authentication — wrong key, a corrupted transfer, or the "
        "component was modified after it was written"
    )


def _check_context(header: dict, expected: dict) -> None:
    """Refuse a component that is not the one the caller asked for.

    The context lives in the header, and the header is what every chunk's tag is bound to,
    so it cannot be edited without the key. Someone who can write to the destination can
    replace a whole component and rewrite manifest.json and SHA256SUMS to match — neither
    is signed — but cannot make another component's bytes claim to be this one.

    A stream with no context is refused rather than accepted. Accepting it would mean
    treating an unnamed component as any component, which is exactly the substitution the
    context prevents, and there is no older format here whose backups that would strand.
    """
    if not expected:
        raise BackupCryptoError("a component must be opened as something in particular")
    actual = header.get("context")
    if not actual:
        raise BackupCryptoError(
            "this component does not say which component of which backup it is, so it "
            "cannot be told apart from any other. Do not restore from it."
        )
    mismatched = {
        name: (value, actual.get(name))
        for name, value in expected.items()
        if str(actual.get(name)) != str(value)
    }
    if mismatched:
        detail = "; ".join(
            f"{name}: this file says {found!r}, the manifest asked for {wanted!r}"
            for name, (wanted, found) in sorted(mismatched.items())
        )
        raise BackupCryptoError(
            f"this component is not the one it is stored as ({detail}). A component has "
            "been moved between backups or replaced; do not restore from it."
        )


def _aad(binding: bytes, counter: int, final: bool) -> bytes:
    """Bind each chunk to this stream, to its position, and to whether it ends the stream."""
    return binding + struct.pack(">Q?", counter, final)


def _nonce(prefix: bytes, counter: int) -> bytes:
    """``prefix || counter``, filling the twelve bytes GCM takes."""
    width = NONCE_BYTES - len(prefix)
    try:
        return prefix + counter.to_bytes(width, "big")
    except OverflowError as exc:
        raise BackupCryptoError(
            f"this component needs more than {256**width} chunks, which the {width}-byte "
            "counter in its nonce cannot address. Lower backup.max_component_gb, or raise "
            "the chunk size."
        ) from exc


def _read_framed(source: BinaryIO, what: str, *, allow_eof: bool = False) -> bytes | None:
    raw_length = _read_exactly(source, _LENGTH.size)
    if raw_length is None:
        if allow_eof:
            return None
        raise BackupCryptoError(f"encrypted stream ended before its {what} length")
    (length,) = _LENGTH.unpack(raw_length)
    payload = _read_exactly(source, length)
    if payload is None:
        raise BackupCryptoError(f"encrypted stream ended inside its {what}")
    return payload


def _read_exactly(source: BinaryIO, count: int) -> bytes | None:
    """Read exactly ``count`` bytes; None at a clean end of stream, raise on a partial one.

    ``read(n)`` on a pipe or a slow mount is allowed to return fewer bytes than asked for
    without being at EOF, so this loops rather than treating a short read as the end.
    Running out *mid-frame* is not a clean end — it is a truncated file, and it has to be
    an error here rather than a confusing struct failure one frame later.
    """
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        piece = source.read(remaining)
        if not piece:
            if not chunks:
                return None
            raise BackupCryptoError(
                f"encrypted stream ended mid-frame: expected {count} bytes, got "
                f"{count - remaining}. The component is truncated."
            )
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)
