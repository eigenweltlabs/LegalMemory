"""What a backup contains, written next to it and readable without the key.

The manifest is the difference between an archive and a backup. It records every
component that was captured, its checksum before and after encryption, and the four
facts a restore has to check before it touches anything: the schema revision the dump
was taken at, the embedding signature the index was built with, the fingerprint of the
connector credential key the encrypted rows belong to, and the fingerprint of the backup
key itself.

It is deliberately *not* encrypted. An operator staring at a NAS during a disaster needs
to be able to see what a backup holds, which appliance it came from and whether they have
the right key, before they can decrypt anything. It carries no secret values — only
names, sizes, digests and fingerprints — and its own digest is in ``SHA256SUMS``, so
tampering with it is detectable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# Bumped when a manifest gains a field a *restore* must understand. A restore refuses a
# manifest from the future rather than guessing at fields it has never heard of.
SCHEMA_VERSION = 1

MANIFEST_NAME = "manifest.json"
CHECKSUM_NAME = "SHA256SUMS"
COMPONENT_PREFIX = "components"


class ManifestError(RuntimeError):
    """A manifest is missing, malformed, or describes a backup this build cannot read."""


@dataclass
class ComponentRecord:
    """One captured store.

    Two digests, because they answer different questions. ``stored_sha256`` is over the
    bytes as they sit at the destination, so it verifies the transfer and is what
    ``SHA256SUMS`` lists — a plain ``sha256sum -c`` at the NAS works. ``plaintext_sha256``
    is over the bytes the component actually is, so it verifies the *decryption* as well,
    and stays meaningful if the backup is later re-encrypted under a new key.
    """

    name: str
    kind: str
    key: str
    plaintext_bytes: int
    plaintext_sha256: str
    stored_bytes: int
    stored_sha256: str
    encrypted: bool = False
    # Whatever the collector wants a human or a restore to know: the database it dumped,
    # the index and document count it snapshotted, the paths a tar walked.
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Manifest:
    backup_id: str
    created_at: str
    schema_version: int = SCHEMA_VERSION
    appliance: dict[str, Any] = field(default_factory=dict)
    encryption: dict[str, Any] | None = None
    components: list[ComponentRecord] = field(default_factory=list)
    # Non-fatal gaps: a store that was configured off, or an optional one that was not
    # reachable. Surfaced on the run and in the UI, because "the backup succeeded" and
    # "the backup contains Langfuse" are different claims and an operator needs both.
    warnings: list[str] = field(default_factory=list)
    config_digest: str = ""

    @property
    def total_plaintext_bytes(self) -> int:
        return sum(component.plaintext_bytes for component in self.components)

    @property
    def total_stored_bytes(self) -> int:
        return sum(component.stored_bytes for component in self.components)

    def component(self, name: str) -> ComponentRecord | None:
        return next((item for item in self.components if item.name == name), None)

    def to_json(self) -> str:
        payload = asdict(self)
        payload["totals"] = {
            "components": len(self.components),
            "plaintext_bytes": self.total_plaintext_bytes,
            "stored_bytes": self.total_stored_bytes,
        }
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)

    def summary(self) -> dict[str, Any]:
        """The shape the API and the run counters both publish."""
        return {
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "encrypted": bool(self.encryption),
            "components": [
                {
                    "name": item.name,
                    "kind": item.kind,
                    "plaintext_bytes": item.plaintext_bytes,
                    "stored_bytes": item.stored_bytes,
                    "detail": item.detail,
                }
                for item in self.components
            ],
            "total_plaintext_bytes": self.total_plaintext_bytes,
            "total_stored_bytes": self.total_stored_bytes,
            "warnings": list(self.warnings),
            "appliance": dict(self.appliance),
        }

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Manifest":
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ManifestError("manifest does not contain an object")
        version = payload.get("schema_version")
        if not isinstance(version, int):
            raise ManifestError("manifest has no schema_version")
        if version > SCHEMA_VERSION:
            raise ManifestError(
                f"backup {payload.get('backup_id', '?')} was written by a newer version of "
                f"this appliance (manifest schema {version}, this build reads "
                f"{SCHEMA_VERSION}). Restore it with a build at least that new — an older "
                "build cannot know what it would be dropping."
            )
        components = []
        for item in payload.get("components", []):
            if not isinstance(item, dict):
                raise ManifestError("manifest component is not an object")
            known = {key: item.get(key) for key in ComponentRecord.__dataclass_fields__}
            missing = [key for key, value in known.items() if value is None and key != "detail"]
            if missing:
                raise ManifestError(
                    f"manifest component {item.get('name', '?')} is missing {', '.join(missing)}"
                )
            known["detail"] = known.get("detail") or {}
            components.append(ComponentRecord(**known))
        return cls(
            backup_id=str(payload.get("backup_id") or ""),
            created_at=str(payload.get("created_at") or ""),
            schema_version=version,
            appliance=payload.get("appliance") or {},
            encryption=payload.get("encryption"),
            components=components,
            warnings=list(payload.get("warnings") or []),
            config_digest=str(payload.get("config_digest") or ""),
        )


def new_backup_id(now: datetime | None = None) -> str:
    """Sortable, timezone-unambiguous, and safe as both a path and an S3 prefix.

    Lexical order is chronological order, which is what makes retention and "the newest
    backup" plain string operations at any destination, including ones that cannot sort.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return f"ki-backup-{moment.strftime('%Y%m%dT%H%M%SZ')}"


def parse_backup_id(backup_id: str) -> datetime | None:
    """The instant a backup id encodes, or None if it is not one of ours.

    Retention sorts on this rather than on the destination's modification times, which an
    rsync to a second NAS rewrites wholesale.
    """
    prefix = "ki-backup-"
    if not backup_id.startswith(prefix):
        return None
    try:
        return datetime.strptime(backup_id[len(prefix) :], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def checksum_file(components: list[ComponentRecord], manifest_sha256: str) -> str:
    """A ``sha256sum -c`` compatible listing of everything in the backup directory."""
    lines = [f"{manifest_sha256}  {MANIFEST_NAME}"]
    lines.extend(f"{item.stored_sha256}  {item.key}" for item in sorted(components, key=_key))
    return "\n".join(lines) + "\n"


def _key(component: ComponentRecord) -> str:
    return component.key
