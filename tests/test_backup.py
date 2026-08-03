"""Full-appliance backup, exercised against the real Postgres the rest of the suite uses.

No mocked destination, no fake dump. A backup is written to a real directory with real
``pg_dump`` output in it, read back, decrypted, checksummed, and finally ``pg_restore``d
into a second real database where the rows are counted. That round trip is the only test
that answers the question this feature exists to answer, and it is the reason the tests
here are slower than the rest of the suite.

The encryption tests are deliberately adversarial. A truncated backup that restores
without complaint is the worst failure this code can have, so truncation, tampering,
reordering and the wrong key each get their own case.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import struct
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.backup import crypto as backup_crypto
from knowledge_index.backup import restore as backup_restore
from knowledge_index.backup import runs as backup_runs
from knowledge_index.backup import secrets as backup_secrets
from knowledge_index.backup import scheduler as backup_scheduler
from knowledge_index.backup.components import libpq_target
from knowledge_index.backup.crypto import (
    BackupCryptoError,
    EncryptingReader,
    decrypt_stream,
    encrypt_stream,
    key_fingerprint,
    load_key,
)
from knowledge_index.backup.destinations import DestinationError, LocalDestination
from knowledge_index.backup.manifest import (
    Manifest,
    ManifestError,
    new_backup_id,
    parse_backup_id,
)
from knowledge_index.backup.retention import plan_retention
from knowledge_index.config import AppConfig, BackupDestinationConfig, BackupRetentionConfig
from knowledge_index.db.models import PipelineRun, ProcessingState, Source, SourceObject

RESTORE_DATABASE = "ki_backup_restore_test"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _no_backup_outlives_its_environment(monkeypatch: pytest.MonkeyPatch):
    """Drain in-process runs before the fixtures that configured them are torn down.

    ``orchestrator_provider = "local"`` dispatches a reserved run to a thread pool and
    returns, which is the contract a scheduler tick relies on: it asserts a run was
    reserved, not that it finished. The thread then ran after the test ended, by which
    point monkeypatch had put KI_BACKUP_STAGING_DIR back to /data/backup-staging — so a
    backup enqueued by a scheduler test woke up in a later test file and tried to write a
    read-only path, failing tests in tests/test_sync_runs.py that have nothing to do with
    backups. The work is legitimate; letting it outlive its configuration is not.

    It takes ``monkeypatch`` so that pytest builds it after monkeypatch and therefore tears
    it down before: draining once the environment has already been put back would wait for
    exactly the run this exists to prevent, and watch it fail.
    """
    yield
    from knowledge_index.sync.runs import wait_for_local_runs

    try:
        wait_for_local_runs(timeout=300)
    except Exception:  # noqa: BLE001 - a run that failed is the test's business, not this
        pass


@pytest.fixture
def backup_key(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> str:
    """The backup key, set the only way there is: stored, encrypted, in the database."""
    monkeypatch.setenv("KI_CONNECTOR_CREDENTIAL_KEY", base64.urlsafe_b64encode(b"c" * 32).decode())
    key = base64.urlsafe_b64encode(bytes(range(32))).decode()
    backup_secrets.store(backup_secrets.ENCRYPTION_KEY, key, factory)
    return key


@pytest.fixture
def appliance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_key: str) -> AppConfig:
    """A configuration whose every path points somewhere real inside ``tmp_path``.

    The gateway, orchestrator and search components are switched off: this suite runs
    against one Postgres, and a backup is required to report their absence as a warning
    rather than to fail — which is itself one of the things asserted below.
    """
    from tests.conftest import TEST_DATABASE_URL

    artifacts = tmp_path / "data" / "artifacts"
    (artifacts / "blobs" / "ab").mkdir(parents=True)
    (artifacts / "blobs" / "ab" / "document.bin").write_bytes(b"a converted document" * 100)
    (tmp_path / "data" / "browser-sources" / "upload").mkdir(parents=True)
    (tmp_path / "data" / "browser-sources" / "upload" / "brief.docx").write_bytes(b"uploaded")

    monkeypatch.setenv("KI_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("KI_BACKUP_STAGING_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("KI_RESTORE_STAGE_DIR", str(tmp_path / "restore-staging"))
    monkeypatch.setenv("KI_CONNECTOR_CREDENTIAL_KEY", base64.urlsafe_b64encode(b"c" * 32).decode())
    # Not mounted in this environment, and that is the point: they must degrade to
    # warnings rather than failing the run.
    monkeypatch.delenv("KI_BACKUP_KEYCLOAK_PATH", raising=False)
    monkeypatch.delenv("KI_BACKUP_HATCHET_CONFIG_PATH", raising=False)
    monkeypatch.delenv("KI_BACKUP_OPENSEARCH_REPO_PATH", raising=False)

    config = AppConfig()
    config.artifact_dir = artifacts
    config.components.orchestrator_provider = "local"
    config.backup.enabled = True
    config.backup.encrypt = True
    config.backup.destination = BackupDestinationConfig(
        kind="local", path=str(tmp_path / "destination"), prefix="knowledge-index"
    )
    config.backup.sources.gateway_databases = False
    config.backup.sources.orchestrator_database = False
    config.backup.sources.search_index = False
    config.backup.require_settled_pipeline = False
    return config


@pytest.fixture
def restore_engine():
    """A second, empty database to restore into, dropped and recreated per test."""
    from tests.conftest import POSTGRES_ADMIN_URL, TEST_DATABASE_URL

    admin = create_engine(POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{RESTORE_DATABASE}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{RESTORE_DATABASE}"'))
    admin.dispose()
    url = TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{RESTORE_DATABASE}"
    engine = create_engine(url)
    with engine.connect() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.commit()
    yield url, engine
    engine.dispose()


@pytest.fixture
def appliance_restic(appliance: AppConfig, tmp_path: Path) -> AppConfig:
    """The same appliance, pointed at a restic repository instead of a directory."""
    appliance.backup.destination = BackupDestinationConfig(
        kind="restic", path=str(tmp_path / "restic"), prefix="ki"
    )
    # restic encrypts what it stores, so the appliance must not seal components first —
    # ciphertext shares no chunks with anything and deduplication would stop working.
    appliance.backup.encrypt = False
    return appliance


restic_required = pytest.mark.skipif(
    shutil.which("restic") is None, reason="restic is not installed on this machine"
)


# --------------------------------------------------------------- incremental destination


@restic_required
def test_a_restic_backup_round_trips_every_store(
    appliance_restic: AppConfig, factory: sessionmaker[Session]
) -> None:
    _seed(factory)
    backup_id = new_backup_id()
    summary = backup_runs.perform_backup(appliance_restic, backup_id, session_factory=factory)

    names = {item["name"] for item in summary["manifest"]["components"]}
    assert {"postgres/ki", "files/artifact-blobs", "files/uploaded"} <= names
    assert summary["verified"]["ok"] is True
    assert backup_runs.list_backups(appliance_restic, session_factory=factory)[0]["backup_id"] == backup_id


@restic_required
def test_a_second_restic_backup_of_an_unchanged_estate_costs_almost_nothing(
    appliance_restic: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The whole reason this destination exists.

    Storing whole objects means a hundred thousand documents are transferred and kept
    again every night, times whatever retention holds — nineteen full copies under the
    default rules. A deduplicating destination stores the second night as the difference
    from the first, so an estate that did not change costs almost nothing to keep again.
    """
    _seed(factory)
    repository = tmp_path / "restic"

    def repository_bytes() -> int:
        return sum(item.stat().st_size for item in repository.rglob("*") if item.is_file())

    backup_runs.perform_backup(appliance_restic, new_backup_id(), session_factory=factory)
    after_first = repository_bytes()
    backup_runs.perform_backup(appliance_restic, new_backup_id(), session_factory=factory)
    after_second = repository_bytes()

    # Some growth is unavoidable — a new manifest, new snapshot metadata, and a dump whose
    # timestamps moved — but it must be a fraction of storing the estate a second time.
    assert after_second - after_first < after_first * 0.5, (
        f"second backup cost {after_second - after_first} bytes against a first backup of "
        f"{after_first}; deduplication is not working"
    )


@restic_required
def test_a_restic_backup_is_not_sealed_twice_and_says_who_encrypted_it(
    appliance_restic: AppConfig, factory: sessionmaker[Session]
) -> None:
    """Double encryption would be silent, and would cost exactly the feature being paid for.

    A manifest that reads "encryption: null" because restic did the encrypting would also
    tell an operator the opposite of the truth at the worst possible moment.
    """
    _seed(factory)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance_restic, backup_id, session_factory=factory)

    manifest = backup_runs.load_manifest(
        appliance_restic, backup_id, session_factory=factory
    )
    assert manifest.encryption is not None
    assert manifest.encryption["performed_by"] == "destination"
    # Not one component carries the appliance's own seal.
    assert not any(component.encrypted for component in manifest.components)


@restic_required
def test_a_restic_backup_stages_and_restores_its_uncompressed_archives(
    appliance_restic: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Archives are written uncompressed for this destination, and must still restore.

    Compression is what hides an unchanged store from a chunker, so it is turned off
    rather than turned off and undone — which means the restore path has to read a plain
    tar as readily as a gzipped one.
    """
    _seed(factory)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance_restic, backup_id, session_factory=factory)

    staged = backup_restore.stage_backup(appliance_restic, backup_id, tmp_path / "staged", session_factory=factory)
    uploads = next(item for item in staged if item.name == "files/uploaded")
    assert uploads.path.suffix == ".tar", "archives must be uncompressed for a deduplicating store"
    with tarfile.open(uploads.path, "r:*") as tar:
        member = tar.extractfile("upload/brief.docx")
        assert member is not None and member.read() == b"uploaded"

    # And it extracts through the same guard a gzipped archive goes through.
    backup_restore.apply_files(appliance_restic, uploads)


@restic_required
def test_retention_reclaims_space_in_a_restic_repository(
    appliance_restic: AppConfig, factory: sessionmaker[Session]
) -> None:
    """Forgetting a backup has to actually remove it, or retention is a listing filter."""
    _seed(factory)
    old = new_backup_id(datetime(2020, 1, 1, 2, 0, 0, tzinfo=UTC))
    keep = new_backup_id()
    backup_runs.perform_backup(appliance_restic, old, session_factory=factory)
    backup_runs.perform_backup(appliance_restic, keep, session_factory=factory)

    appliance_restic.backup.retention = BackupRetentionConfig(
        daily=1, weekly=0, monthly=0, yearly=0, min_keep=1, prune_enabled=True
    )
    outcome = backup_runs.prune_backups(
        appliance_restic, keep_id=keep, session_factory=factory
    )

    assert old in outcome["pruned"]
    assert [item["backup_id"] for item in backup_runs.list_backups(appliance_restic, session_factory=factory)] == [keep]


def test_an_unencrypted_restic_destination_may_still_capture_secrets() -> None:
    """restic encrypts for itself, so 'encrypt' being off is not the same as unprotected.

    The guard exists to stop KI_CONNECTOR_CREDENTIAL_KEY reaching a NAS in the clear. A
    restic repository is not the clear, and forcing the appliance's own encryption on top
    would destroy the deduplication that is the only reason to choose it.
    """
    from knowledge_index.config import BackupConfig

    config = BackupConfig(
        encrypt=False, destination=BackupDestinationConfig(kind="restic", path="/backups")
    )
    assert config.sources.environment_secrets is True
    assert config.encryption_is_guaranteed is True

    # The same combination without restic is still refused.
    with pytest.raises(ValueError, match="requires backup.encrypt"):
        BackupConfig(encrypt=False, destination=BackupDestinationConfig(kind="local"))


# ------------------------------------------------------------------ database credentials


@pytest.mark.parametrize(
    ("url", "expected_uri", "expected_password"),
    [
        (
            "postgresql+pg8000://ki:ki-dev-only@localhost:5439/ki_test",
            "postgresql://ki@localhost:5439/ki_test",
            "ki-dev-only",
        ),
        # A password libpq would misparse inside a URI, and which SQLAlchemy percent-encodes
        # on the way in: it has to reach PGPASSWORD decoded.
        (
            "postgresql+pg8000://hatchet:p%40ss%2Fword@hatchet-postgres:5432/hatchet",
            "postgresql://hatchet@hatchet-postgres:5432/hatchet",
            "p@ss/word",
        ),
        ("postgresql+pg8000://localhost:5432/nopass", "postgresql://localhost:5432/nopass", None),
    ],
)
def test_the_password_reaches_pg_dump_out_of_band_and_never_in_the_uri(
    url: str, expected_uri: str, expected_password: str | None
) -> None:
    """The connection string handed to pg_dump must carry no password at all.

    Two SQLAlchemy behaviours make the obvious implementation silently wrong, and both
    have to stay covered. ``URL.set(password=None)`` is a no-op — ``set`` ignores None
    arguments — and ``str`` on a URL renders the password as ``***``. Together they hand
    pg_dump a literal ``***`` to authenticate with, which libpq prefers over PGPASSWORD, so
    every backup of a password-protected database fails. It is also why the password must
    not be in the URI in the first place: an argument is visible in ``ps``.
    """
    uri, password = libpq_target(url)
    assert uri == expected_uri
    assert password == expected_password
    assert "***" not in uri
    if expected_password:
        assert expected_password not in uri


# ------------------------------------------------------------------------- manifest ids


def test_a_backup_id_sorts_chronologically_and_round_trips_to_its_instant() -> None:
    early = new_backup_id(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    late = new_backup_id(datetime(2026, 11, 2, 3, 4, 5, tzinfo=UTC))
    assert early == "ki-backup-20260102T030405Z"
    # Lexical order is chronological order — retention and "the newest backup" are string
    # operations at destinations that cannot sort by time.
    assert early < late
    assert parse_backup_id(early) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert parse_backup_id("something-an-operator-left-here") is None


def test_a_manifest_from_a_newer_appliance_is_refused_rather_than_partially_read() -> None:
    payload = json.loads(Manifest(backup_id="b", created_at="now").to_json())
    payload["schema_version"] = 99
    with pytest.raises(ManifestError, match="newer version"):
        Manifest.from_json(json.dumps(payload))


# --------------------------------------------------------------------------- encryption


CONTEXT = {"backup_id": "ki-backup-20260728T020000Z", "component": "postgres/ki"}


def test_an_encrypted_stream_round_trips_through_both_writers(backup_key: str) -> None:
    key = load_key(base64.urlsafe_b64encode(bytes(range(32))).decode())
    plaintext = os.urandom(300_000)
    pushed = io.BytesIO()
    encrypt_stream(io.BytesIO(plaintext), pushed, key, chunk_bytes=64_000, context=CONTEXT)
    pulled = EncryptingReader(io.BytesIO(plaintext), key, chunk_bytes=64_000, context=CONTEXT).read()
    for sealed in (pushed.getvalue(), pulled):
        opened = io.BytesIO()
        assert decrypt_stream(io.BytesIO(sealed), opened, key, expect_context=CONTEXT) == len(plaintext)
        assert opened.getvalue() == plaintext


def test_an_empty_component_is_still_a_sealed_stream(backup_key: str) -> None:
    key = load_key(base64.urlsafe_b64encode(bytes(range(32))).decode())
    sealed = EncryptingReader(io.BytesIO(b""), key, context=CONTEXT).read()
    opened = io.BytesIO()
    assert decrypt_stream(io.BytesIO(sealed), opened, key, expect_context=CONTEXT) == 0
    assert opened.getvalue() == b""


def test_a_truncated_component_fails_to_decrypt_instead_of_decrypting_to_less(
    backup_key: str,
) -> None:
    """The failure this framing exists to prevent.

    A backup cut short by a full disk or a killed transfer must not open into a shorter,
    plausible-looking dump that restores without complaint.
    """
    key = load_key(base64.urlsafe_b64encode(bytes(range(32))).decode())
    sealed = EncryptingReader(io.BytesIO(os.urandom(200_000)), key, chunk_bytes=64_000, context=CONTEXT).read()
    for cut in (len(sealed) // 2, len(sealed) - 1):
        with pytest.raises(BackupCryptoError, match="truncated|mid-frame"):
            decrypt_stream(io.BytesIO(sealed[:cut]), io.BytesIO(), key, expect_context=CONTEXT)


def test_a_tampered_component_fails_to_decrypt(backup_key: str) -> None:
    key = load_key(base64.urlsafe_b64encode(bytes(range(32))).decode())
    sealed = bytearray(EncryptingReader(io.BytesIO(b"x" * 50_000), key, context=CONTEXT).read())
    sealed[-20] ^= 0xFF
    with pytest.raises(BackupCryptoError, match="failed authentication"):
        decrypt_stream(io.BytesIO(bytes(sealed)), io.BytesIO(), key, expect_context=CONTEXT)


def test_the_wrong_key_is_named_rather_than_failing_as_corruption(backup_key: str) -> None:
    """An operator holding the wrong key must be told that, not told the backup is broken."""
    key = load_key(base64.urlsafe_b64encode(bytes(range(32))).decode())
    other = base64.urlsafe_b64decode(base64.urlsafe_b64encode(b"z" * 32))
    sealed = EncryptingReader(io.BytesIO(b"payload"), key, context=CONTEXT).read()
    with pytest.raises(BackupCryptoError, match="encrypted under key"):
        decrypt_stream(io.BytesIO(sealed), io.BytesIO(), other, expect_context=CONTEXT)
    assert key_fingerprint(key) != key_fingerprint(other)


def test_appending_to_a_backup_is_detected(backup_key: str) -> None:
    key = load_key(base64.urlsafe_b64encode(bytes(range(32))).decode())
    sealed = EncryptingReader(io.BytesIO(b"first"), key, context=CONTEXT).read()
    with pytest.raises(BackupCryptoError, match="past its final chunk"):
        decrypt_stream(io.BytesIO(sealed + b"trailing"), io.BytesIO(), key, expect_context=CONTEXT)


# ------------------------------------------------------------------------- destinations


def test_a_stream_carries_enough_randomness_to_keep_its_nonces_unique(backup_key: str) -> None:
    """The prefix is what bounds how many streams may share one key.

    Every stream picks a random prefix and counts within it, so two streams that draw the
    same prefix reuse every nonce under the same key — which in GCM surrenders the
    plaintexts to anyone holding both, and the authentication key with them. At roughly a
    dozen components a night, four bytes puts that at a few percent over an appliance's
    life; eight puts it out of reach.
    """
    key = load_key(base64.urlsafe_b64encode(bytes(range(32))).decode())
    prefixes = set()
    for _ in range(200):
        header = backup_crypto.header_for(key, context=CONTEXT)
        prefix = base64.b64decode(header["nonce_prefix"])
        assert len(prefix) >= 8
        prefixes.add(prefix)
    assert len(prefixes) == 200


def test_a_component_cannot_be_moved_into_another_backup(backup_key: str) -> None:
    """The manifest is not signed, so the component itself has to say what it is.

    Anyone who can write to the destination can rewrite manifest.json and SHA256SUMS. What
    they cannot do without the key is make one component's tags claim to be another's, and
    that is what has to stop last month's dump being slipped into tonight's backup.
    """
    key = load_key(base64.urlsafe_b64encode(bytes(range(32))).decode())
    sealed = io.BytesIO()
    encrypt_stream(
        io.BytesIO(b"last month's matters" * 500),
        sealed,
        key,
        context={"backup_id": "ki-backup-20260101T020000Z", "component": "postgres/ki"},
    )

    # Same key, same component name, different backup: the substitution a rewritten
    # manifest would otherwise make invisible.
    with pytest.raises(BackupCryptoError, match="not the one it is stored as"):
        decrypt_stream(
            io.BytesIO(sealed.getvalue()),
            io.BytesIO(),
            key,
            expect_context={
                "backup_id": "ki-backup-20260728T020000Z",
                "component": "postgres/ki",
            },
        )
    # And the same backup, but put in another component's place.
    with pytest.raises(BackupCryptoError, match="not the one it is stored as"):
        decrypt_stream(
            io.BytesIO(sealed.getvalue()),
            io.BytesIO(),
            key,
            expect_context={
                "backup_id": "ki-backup-20260101T020000Z",
                "component": "postgres/litellm",
            },
        )
    # Asked for what it actually is, it opens.
    out = io.BytesIO()
    decrypt_stream(
        io.BytesIO(sealed.getvalue()),
        out,
        key,
        expect_context={"backup_id": "ki-backup-20260101T020000Z", "component": "postgres/ki"},
    )
    assert out.getvalue() == b"last month's matters" * 500


def test_a_component_that_does_not_say_what_it_is_gets_refused(backup_key: str) -> None:
    """An unnamed component is any component, which is the substitution being prevented.

    Nothing reads a stream this appliance did not write, so there is no older format whose
    backups accepting one would preserve — and accepting one would reopen exactly the hole
    the context closes.
    """
    key = load_key(base64.urlsafe_b64encode(bytes(range(32))).decode())
    payload = b"a component with no name" * 500

    # Hand-build a stream in the shape this module writes, minus the context.
    encoded_prefix = base64.b64encode(os.urandom(8)).decode()
    header = {
        "algorithm": "AES-256-GCM",
        "chunk_bytes": 4096,
        "nonce_prefix": encoded_prefix,
        "key_fingerprint": key_fingerprint(key),
    }
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    binding = hashlib.sha256(header_bytes).digest()
    prefix = base64.b64decode(encoded_prefix)
    aesgcm = AESGCM(key)
    body = bytearray(b"KIBAK1\n" + len(header_bytes).to_bytes(4, "big") + header_bytes)
    chunks = [payload[index : index + 4096] for index in range(0, len(payload), 4096)]
    for counter, chunk in enumerate(chunks):
        final = counter == len(chunks) - 1
        nonce = prefix + counter.to_bytes(4, "big")
        aad = binding + struct.pack(">Q?", counter, final)
        sealed = aesgcm.encrypt(nonce, chunk, aad)
        body += len(sealed).to_bytes(4, "big") + sealed

    with pytest.raises(BackupCryptoError, match="does not say which component"):
        decrypt_stream(
            io.BytesIO(bytes(body)),
            io.BytesIO(),
            key,
            expect_context={"backup_id": "ki-backup-20260728T020000Z", "component": "postgres/ki"},
        )


def test_a_nonce_prefix_of_the_wrong_width_is_refused(backup_key: str) -> None:
    """The prefix width is what bounds nonce reuse, so a narrower one is not this format."""
    key = load_key(base64.urlsafe_b64encode(bytes(range(32))).decode())
    header = {
        "algorithm": "AES-256-GCM",
        "chunk_bytes": 4096,
        "nonce_prefix": base64.b64encode(os.urandom(4)).decode(),
        "key_fingerprint": key_fingerprint(key),
        "context": {"backup_id": "b", "component": "c"},
    }
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    body = b"KIBAK1\n" + len(header_bytes).to_bytes(4, "big") + header_bytes

    with pytest.raises(BackupCryptoError, match="nonce prefix"):
        decrypt_stream(
            io.BytesIO(body), io.BytesIO(), key, expect_context={"backup_id": "b", "component": "c"}
        )


def test_a_component_key_cannot_escape_the_backup_directory(tmp_path: Path) -> None:
    """Manifests are read back off a share other people can write to."""
    destination = LocalDestination(BackupDestinationConfig(kind="local", path=str(tmp_path)))
    with pytest.raises(DestinationError, match="escapes"):
        destination.write("ki-backup-1", "../../escaped", io.BytesIO(b"x"))


def test_an_interrupted_write_leaves_no_partial_component(tmp_path: Path) -> None:
    destination = LocalDestination(BackupDestinationConfig(kind="local", path=str(tmp_path)))

    class Failing:
        def read(self, size: int = -1) -> bytes:
            raise OSError("the mount went away")

    with pytest.raises(DestinationError):
        destination.write("ki-backup-1", "components/x.dump", Failing())
    # No half-file that a later listing would count as a component.
    assert not list((tmp_path / "ki-backup-1").glob("components/*"))


# --------------------------------------------------------------------------- retention


def _ids(*stamps: str) -> list[str]:
    return [f"ki-backup-{stamp}" for stamp in stamps]


def test_retention_keeps_the_newest_of_each_period_and_prunes_the_rest() -> None:
    backups = _ids(
        "20260728T020000Z",  # today
        "20260727T020000Z",  # yesterday
        "20260726T020000Z",
        "20260726T010000Z",  # same day, older -> not the daily for that day
        "20260601T020000Z",  # a previous month
        "20250601T020000Z",  # a previous year
    )
    decisions = {item.backup_id: item for item in plan_retention(backups, BackupRetentionConfig())}
    assert decisions["ki-backup-20260728T020000Z"].keep
    assert decisions["ki-backup-20260601T020000Z"].keep  # monthly
    assert decisions["ki-backup-20250601T020000Z"].keep  # yearly
    # Two backups on one day: the older one is only kept if some other rule wants it.
    older = decisions["ki-backup-20260726T010000Z"]
    assert "daily" not in older.reasons


def test_retention_never_deletes_something_it_does_not_recognize() -> None:
    decisions = plan_retention(
        ["ki-backup-20260728T020000Z", "quarterly-archive-do-not-delete"],
        BackupRetentionConfig(daily=0, weekly=0, monthly=0, yearly=0, min_keep=1),
    )
    stranger = next(item for item in decisions if item.backup_id.startswith("quarterly"))
    assert stranger.keep and stranger.reasons == ("unrecognized",)


def test_retention_can_never_empty_a_destination() -> None:
    """Rules that delete everything are a delete feature wearing a backup feature's hat."""
    nothing_kept = BackupRetentionConfig(daily=0, weekly=0, monthly=0, yearly=0, min_keep=1)
    decisions = plan_retention(_ids("20260728T020000Z", "20260101T020000Z"), nothing_kept)
    assert sum(1 for item in decisions if item.keep) == 1
    assert decisions[0].backup_id == "ki-backup-20260728T020000Z"


# ------------------------------------------------------------------------ configuration


def test_capturing_deployment_secrets_requires_encryption() -> None:
    """Refused at validation, so it cannot be saved from the admin UI at all."""
    config = AppConfig()
    with pytest.raises(ValueError, match="environment_secrets requires backup.encrypt"):
        config.backup.__class__(
            encrypt=False, sources={"environment_secrets": True}
        )


def test_the_backup_key_is_never_captured_into_the_backup_it_protects(
    appliance: AppConfig, tmp_path: Path
) -> None:
    from knowledge_index.backup.components import capture_environment_secrets

    staged = capture_environment_secrets(appliance, tmp_path)
    payload = json.loads(staged.path.read_text())
    assert "KI_BACKUP_ENCRYPTION_KEY" not in payload["environment"]
    # And the key a restore genuinely cannot do without is present.
    assert "KI_CONNECTOR_CREDENTIAL_KEY" in payload["environment"]
    # The manifest is not encrypted, so the detail block carries names only.
    assert "environment" not in staged.detail


# ------------------------------------------------------------------------- the round trip


def _seed(factory: sessionmaker[Session], count: int = 3) -> None:
    with factory() as session:
        for index in range(count):
            session.add(
                Source(
                    kind="local_fs",
                    display_name=f"Matter archive {index}",
                    config={"root": f"/estate/{index}"},
                    sync_policy={"mode": "manual"},
                )
            )
        session.commit()


def _unsettled(factory: sessionmaker[Session]) -> None:
    """One document stuck mid-pipeline, with the source object its row has to point at."""
    with factory() as session:
        source = Source(kind="local_fs", display_name="Estate", config={}, sync_policy={})
        session.add(source)
        session.flush()
        obj = SourceObject(
            source_id=source.id, external_id="obj-1", path="/estate/a.docx", name="a.docx"
        )
        session.add(obj)
        session.flush()
        session.add(ProcessingState(source_object_id=obj.id, stage="convert", status="running"))
        session.commit()


def test_a_backup_captures_every_configured_store_and_verifies_itself(
    appliance: AppConfig, factory: sessionmaker[Session]
) -> None:
    _seed(factory)
    backup_id = new_backup_id()
    summary = backup_runs.perform_backup(appliance, backup_id, session_factory=factory)

    names = {item["name"] for item in summary["manifest"]["components"]}
    assert "postgres/ki" in names
    assert "files/artifact-blobs" in names
    assert "files/uploaded" in names
    assert "secrets/environment" in names
    assert summary["encrypted"] is True
    # verify_after_write is on by default, and a backup that fails it raises rather than
    # being reported as a success.
    assert summary["verified"]["ok"] is True
    assert summary["verified"]["deep"] is True

    # The stores this environment does not have are warnings, not failures.
    assert any("volumes/keycloak" in warning for warning in summary["warnings"])

    # And the staging directory is left empty: components are deleted as they transfer.
    staging = Path(os.environ["KI_BACKUP_STAGING_DIR"])
    assert not list(staging.glob("*/*"))


def test_a_backup_is_listed_and_re_verifiable_afterwards(
    appliance: AppConfig, factory: sessionmaker[Session]
) -> None:
    _seed(factory)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)

    listed = backup_runs.list_backups(appliance, session_factory=factory)
    assert [item["backup_id"] for item in listed] == [backup_id]
    assert listed[0]["complete"] is True
    assert backup_runs.verify_backup(appliance, backup_id, session_factory=factory)["ok"] is True


def test_a_corrupted_component_is_caught_by_verification(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The whole point of reading a backup back rather than trusting the write."""
    _seed(factory)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)

    stored = next(
        (tmp_path / "destination" / "knowledge-index" / backup_id / "components").glob("*.dump")
    )
    raw = bytearray(stored.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    stored.write_bytes(bytes(raw))

    report = backup_runs.verify_backup(appliance, backup_id, session_factory=factory)
    assert report["ok"] is False
    assert any("postgres/ki" in problem for problem in report["problems"])


def test_swapping_a_component_between_backups_is_caught_even_with_a_rewritten_manifest(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The threat this encryption exists for: a destination the appliance does not control.

    manifest.json and SHA256SUMS are not encrypted and not signed, so anyone who can write
    to the share can rewrite them to describe whatever they put there. Slipping an older
    dump into a newer backup would then restore a firm to a state it did not choose, and
    every checksum would agree. What the attacker cannot do is re-seal the component under
    the backup it is being passed off as.
    """
    _seed(factory, count=2)
    old_id = new_backup_id(datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC))
    backup_runs.perform_backup(appliance, old_id, session_factory=factory)
    _seed(factory, count=2)
    new_id = new_backup_id(datetime(2026, 7, 28, 2, 0, 0, tzinfo=UTC))
    backup_runs.perform_backup(appliance, new_id, session_factory=factory)

    root = tmp_path / "destination" / "knowledge-index"
    stolen = root / old_id / "components" / "postgres-ki.dump"
    victim = root / new_id / "components" / "postgres-ki.dump"
    victim.write_bytes(stolen.read_bytes())

    # And the manifest is rewritten to agree with what is now on disk, exactly as someone
    # with write access to the share would do.
    old_manifest = json.loads((root / old_id / "manifest.json").read_text())
    new_path = root / new_id / "manifest.json"
    new_manifest = json.loads(new_path.read_text())
    swapped = next(c for c in old_manifest["components"] if c["name"] == "postgres/ki")
    for index, component in enumerate(new_manifest["components"]):
        if component["name"] == "postgres/ki":
            new_manifest["components"][index] = swapped
    new_path.write_text(json.dumps(new_manifest, indent=2, sort_keys=True))

    report = backup_runs.verify_backup(appliance, new_id, session_factory=factory)
    assert report["ok"] is False
    assert any("postgres/ki" in problem for problem in report["problems"])


def test_an_incomplete_backup_directory_is_never_offered_as_restorable(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A run that died mid-transfer leaves components and no manifest."""
    _seed(factory)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)
    (tmp_path / "destination" / "knowledge-index" / backup_id / "manifest.json").unlink()

    listed = backup_runs.list_backups(appliance, session_factory=factory)
    assert listed[0]["complete"] is False and listed[0]["problem"]
    with pytest.raises(backup_runs.BackupNotFound):
        backup_runs.load_manifest(appliance, backup_id)


def test_a_backup_restores_into_an_empty_database_with_its_rows_intact(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path, restore_engine
) -> None:
    """The only test that answers the question the whole feature exists to answer."""
    _seed(factory, count=4)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)

    staged = backup_restore.stage_backup(appliance, backup_id, tmp_path / "staged", session_factory=factory)
    dump = next(item for item in staged if item.name == "postgres/ki")
    restore_url, engine = restore_engine
    result = backup_restore.apply_database(appliance, dump, target_url=restore_url)
    assert result["database"] == "ki_test"

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT display_name FROM sources ORDER BY 1")).all()
    assert [row[0] for row in rows] == [f"Matter archive {index}" for index in range(4)]


def test_a_healthy_restore_is_not_reported_as_failed_by_harmless_version_skew(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path, restore_engine
) -> None:
    """pg_restore exits non-zero on a restore that worked, and that must not read as failure.

    A dump written by a newer pg_dump than the server it is loaded into opens by SETting a
    GUC the server does not know, which pg_restore reports as an error and exits 1 for. That
    is the normal case for this appliance. If it counted as a failed restore, every restore
    would report failure and the report would stop meaning anything.
    """
    _seed(factory, count=4)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)
    staged = backup_restore.stage_backup(appliance, backup_id, tmp_path / "staged", session_factory=factory)
    dump = next(item for item in staged if item.name == "postgres/ki")
    restore_url, engine = restore_engine

    result = backup_restore.apply_database(appliance, dump, target_url=restore_url)

    assert result["ok"] is True
    assert not result["serious_errors"]
    # And the rows really are there, which is what makes "ok" the honest answer.
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT count(*) FROM sources")).scalar_one()
    assert rows == 4


def test_a_restore_that_fails_is_reported_as_failed_rather_than_returned_as_data(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path, restore_engine
) -> None:
    """The failure this guards is a restore that half worked and exited 0.

    pg_restore is deliberately not run with --exit-on-error, so a store can come back
    with errors and still return. Those errors have to reach the caller as a verdict, or
    the CLI prints them inside a JSON blob, returns 0, and restore-backup.sh says
    "Restore complete" over the top.
    """
    _seed(factory)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)
    staged = backup_restore.stage_backup(appliance, backup_id, tmp_path / "staged", session_factory=factory)
    dump = next(item for item in staged if item.name == "postgres/ki")
    restore_url, _engine = restore_engine

    # A dump whose table data is intact but whose schema cannot be created: the restore
    # runs, reports errors against most objects, and exits non-zero.
    hostile = tmp_path / "staged" / "hostile.dump"
    hostile.write_bytes(dump.path.read_bytes().replace(b"public", b"nosuchschema", 1))
    broken = backup_restore.StagedFile(
        name="postgres/ki", kind="postgres", path=hostile, bytes=0, detail=dump.detail
    )

    try:
        result = backup_restore.apply_database(appliance, broken, target_url=restore_url)
    except backup_restore.RestoreError:
        # Refusing outright is also an honest answer; what must not happen is a silent ok.
        return
    assert result["ok"] is False
    assert result["serious_errors"]


def test_staged_files_are_the_files_that_went_in(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _seed(factory)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)

    staged = backup_restore.stage_backup(appliance, backup_id, tmp_path / "staged", session_factory=factory)
    uploads = next(item for item in staged if item.name == "files/uploaded")
    with tarfile.open(uploads.path, "r:gz") as tar:
        member = tar.extractfile("upload/brief.docx")
        assert member is not None and member.read() == b"uploaded"


def test_staging_leaves_no_world_readable_copy_of_the_estate(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Staging turns an encrypted backup back into plaintext, and it is asked for routinely.

    The docs tell operators to stage on a live appliance as a drill, which is the right
    advice — but at the default 0644 every drill leaves the firm's database dumps and its
    KI_CONNECTOR_CREDENTIAL_KEY readable by any local account.
    """
    _seed(factory)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)

    target = tmp_path / "staged"
    staged = backup_restore.stage_backup(appliance, backup_id, target, session_factory=factory)

    assert target.stat().st_mode & 0o077 == 0
    for item in staged:
        assert item.path.stat().st_mode & 0o077 == 0, f"{item.name} is readable by others"
    # Including the one that is literally a file of secrets.
    secrets = next(item for item in staged if item.name == "secrets/environment")
    assert "KI_CONNECTOR_CREDENTIAL_KEY" in json.loads(secrets.path.read_text())["environment"]


def test_restaging_reuses_what_is_already_there_but_re_checks_it_first(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A whole-appliance restore stages, replaces volumes, then applies — in two calls.

    Without reuse the second call transfers and decrypts the whole estate again, which on
    a NAS or an S3 endpoint is the longest part of the restore, paid twice on the day it
    matters most. Reuse has to keep the guarantee: what is on disk is still hashed against
    the manifest before it is handed back, so a staged file somebody edited in between is
    fetched again rather than trusted.
    """
    _seed(factory)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)
    target = tmp_path / "staged"
    first = backup_restore.stage_backup(appliance, backup_id, target, session_factory=factory)

    again = backup_restore.stage_backup(appliance, backup_id, target, reuse=True, session_factory=factory)
    assert {item.name for item in again} == {item.name for item in first}
    assert all(item.path.is_file() for item in again)

    # A staged component that no longer matches the manifest is re-fetched, not reused.
    dump = next(item for item in again if item.name == "postgres/ki")
    dump.path.write_bytes(b"not the dump that was staged")
    repaired = backup_restore.stage_backup(appliance, backup_id, target, reuse=True, session_factory=factory)
    restaged = next(item for item in repaired if item.name == "postgres/ki")
    assert restaged.path.read_bytes() != b"not the dump that was staged"
    assert restaged.bytes > 0


def test_staging_refuses_a_backup_whose_bytes_no_longer_match_its_manifest(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _seed(factory)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)
    stored = next(
        (tmp_path / "destination" / "knowledge-index" / backup_id / "components").glob("*.tar.gz")
    )
    raw = bytearray(stored.read_bytes())
    raw[-40] ^= 0xFF
    stored.write_bytes(bytes(raw))

    with pytest.raises((backup_restore.RestoreError, BackupCryptoError)):
        backup_restore.stage_backup(appliance, backup_id, tmp_path / "staged", session_factory=factory)


def test_a_restore_stages_and_verifies_without_touching_the_appliance(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The safe half of a restore, which is the half a firm should be running routinely.

    Staging proves a backup is readable — downloaded, decrypted, every checksum re-checked
    against the manifest — and writes nothing the appliance uses. A firm that has never
    done it does not have backups, it has files.
    """
    from knowledge_index.backup import restore_runs

    _seed(factory, count=3)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)

    enqueued = restore_runs.enqueue_restore(factory, appliance, backup_id=backup_id)
    # Dispatched to a worker, exactly as the API does it, so the test waits the way the UI
    # does rather than reaching past the ledger this feature is built on.
    outcome = backup_runs.wait_for_run(factory, enqueued.run_id, timeout=300)
    assert outcome["status"] == "completed", outcome["error"]

    with factory() as session:
        record = session.get(PipelineRun, enqueued.run_id)
        assert record.workflow == "restore"
        assert record.status == "completed", record.last_error
        assert record.counters["applying"] is False
        assert record.counters["staged"] >= 3
    # And the estate is untouched: the rows that were there are still the rows there are.
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 3


def test_a_restore_puts_the_rows_back(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The question the whole feature exists to answer, asked through the run ledger."""
    from knowledge_index.backup import restore_runs

    _seed(factory, count=4)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)

    # Lose the estate the way a firm would.
    with factory() as session:
        session.query(Source).delete()
        session.commit()
        assert session.scalar(select(func.count()).select_from(Source)) == 0

    enqueued = restore_runs.enqueue_restore(
        factory, appliance, backup_id=backup_id, apply_databases=True
    )
    # Restoring the appliance's own database replaces pipeline_runs, so the row tracking
    # this restore is deleted by the restore itself. Waiting for it to reach "completed"
    # would be waiting on a row that cannot survive; what has to be true is that a record
    # of the restore exists in the ledger the appliance now has.
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        with factory() as session:
            record = session.get(PipelineRun, enqueued.run_id)
            if record is not None and record.status in ("completed", "failed"):
                break
        time.sleep(1)

    with factory() as session:
        record = session.get(PipelineRun, enqueued.run_id)
        assert record is not None, "the restore left no record of itself"
        assert record.status == "completed", record.last_error
        assert record.counters["applying"] is True
        assert record.counters.get("ledger_replaced") is True
    with factory() as session:
        names = sorted(session.scalars(select(Source.display_name)).all())
    assert names == [f"Matter archive {index}" for index in range(4)]


def test_a_restore_refuses_a_folder_outside_the_ones_offered(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """An admin-only endpoint is still not a way to read any path on the host."""
    from knowledge_index.backup import restore_runs

    with pytest.raises(restore_runs.RestoreNotAllowed, match="not one of the folders"):
        restore_runs.list_backups_at(appliance, "/etc", factory)


def test_a_restore_is_blocked_when_the_connector_key_no_longer_matches(
    appliance: AppConfig,
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure that ruins a recovery quietly a week after it appears to have worked."""
    _seed(factory)
    backup_id = new_backup_id()
    backup_runs.perform_backup(appliance, backup_id, session_factory=factory)

    monkeypatch.setenv(
        "KI_CONNECTOR_CREDENTIAL_KEY", base64.urlsafe_b64encode(b"d" * 32).decode()
    )
    plan = backup_restore.restore_plan(appliance, backup_id, factory)
    assert plan["ok"] is False
    assert any("connector credential key" in blocker for blocker in plan["blockers"])


def test_retention_prunes_old_backups_but_never_the_one_just_taken(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _seed(factory)
    destination_root = tmp_path / "destination" / "knowledge-index"
    destination_root.mkdir(parents=True, exist_ok=True)
    for stamp in ("20200101T020000Z", "20200102T020000Z"):
        (destination_root / f"ki-backup-{stamp}").mkdir()

    appliance.backup.retention = BackupRetentionConfig(
        daily=1, weekly=0, monthly=0, yearly=0, min_keep=1
    )
    backup_id = new_backup_id()
    summary = backup_runs.perform_backup(appliance, backup_id, session_factory=factory)

    remaining = {path.name for path in destination_root.iterdir()}
    assert backup_id in remaining
    assert summary["pruned"]
    assert not any(name.startswith("ki-backup-2020") for name in remaining)


# ------------------------------------------------------------- the appliance afterwards


def test_the_restore_agent_refuses_a_service_that_is_not_on_its_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service name reaches a Docker socket that is root on the host.

    A caller who could name any container could stop Postgres, or the agent's own
    neighbours, and a compromised app would have found in the restore path a way to take
    the appliance apart. The list lives in the agent's source; the request only chooses
    from it.
    """
    from fastapi.testclient import TestClient

    from knowledge_index.backup import volume_agent

    monkeypatch.setenv("KI_RESTORE_AGENT_SECRET", "shared-secret")
    client = TestClient(volume_agent.create_agent())

    refused = client.post(
        "/restart-service",
        json={"service": "postgres"},
        headers={"Authorization": "Bearer shared-secret"},
    )
    assert refused.status_code == 400
    assert "not a service this agent may restart" in refused.json()["detail"]

    # The same auth as replacing a volume, and it is checked before the list is consulted:
    # an unauthenticated caller learns nothing about what this appliance runs.
    assert client.post("/restart-service", json={"service": "app"}).status_code == 401
    assert (
        client.post(
            "/restart-service", json={"service": "app"}, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    # And what it will restart is declared rather than discovered by trying.
    assert "worker" in client.get("/healthz").json()["restarts"]


def test_only_the_services_whose_store_was_restored_are_restarted() -> None:
    """Restoring a folder of documents must not take the orchestrator down with it.

    The restart exists because ``pg_restore --clean`` leaves connected services using
    cached plans and type identifiers for tables it has dropped. Nothing that was not
    restored has that problem, so a blanket "bounce everything" would turn the narrowest
    recovery a firm can ask for into an outage of the whole appliance.
    """
    from knowledge_index.backup import restore_runs

    assert restore_runs.services_to_restart([{"component": "files/artifact-blobs"}]) == []
    assert restore_runs.services_to_restart([{"component": "postgres/litellm"}]) == ["litellm"]
    assert restore_runs.services_to_restart([{"component": "postgres/ki"}]) == [
        "watcher",
        "app",
        "worker",
    ]
    # A whole-appliance restore: what the appliance's own processes depend on first, and
    # the process that is running the restore last.
    everything = restore_runs.services_to_restart(
        [
            {"component": name}
            for name in ("files/uploaded", "postgres/ki", "postgres/langfuse", "postgres/hatchet")
        ]
    )
    assert everything == ["hatchet", "langfuse", "watcher", "app", "worker"]

    # The two tables are in different files and only ever meet on a real recovery, where a
    # name in one that is missing from the other reads as "not a service this agent may
    # restart" — a warning telling an operator to restart something by hand for no reason
    # other than an edit to one table and not the other.
    from knowledge_index.backup.volume_agent import RESTARTABLE

    derivable = {
        service
        for services in restore_runs._SERVICES_BY_COMPONENT.values()
        for service in services
    }
    assert derivable <= set(RESTARTABLE)


def test_an_unreachable_restore_agent_is_work_to_do_by_hand_not_a_failed_restore(
    appliance: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore that put every row back has succeeded, whatever the agent did next.

    Failing the run would tell an operator their recovery did not work, and the thing they
    would do about that is restore again over a database that was already correct. What
    they need instead is the list of services still talking to the old database and the
    command that fixes them.
    """
    from knowledge_index.backup import restore_runs

    # Nothing listens on the discard port, so this is an agent that is configured, named,
    # and not there — the state a firm is in when the agent container failed to start.
    monkeypatch.setenv("KI_RESTORE_AGENT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("KI_RESTORE_AGENT_SECRET", "shared-secret")

    restarted, warnings = restore_runs._restart_services(appliance, ["hatchet", "litellm"])

    assert restarted == []
    assert len(warnings) == 1
    assert "hatchet, litellm" in warnings[0]
    assert "docker compose restart hatchet litellm" in warnings[0]


def test_the_service_running_the_restore_is_named_rather_than_restarted_by_it(
    appliance: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker cannot bounce itself halfway through the run that is doing the bouncing.

    Stopping it here kills the restore at the point where it still has to write down what
    it did — into a ledger it has just replaced, which is the only evidence the firm has
    that the restore happened at all. Leaving it out silently would be worse again: an
    appliance whose worker still holds dead type identifiers accepts no work and explains
    nothing. So it is named, with the command, and everything else is restarted here.

    The agent's own refusals are tested against the real agent above; what is under test
    here is which services the restore decides to ask about, so the client is stood in for.
    """
    from knowledge_index.backup import restore_runs, volume_agent

    appliance.components.orchestrator_provider = "hatchet"
    asked: list[str] = []

    def record(service: str) -> dict:
        asked.append(service)
        return {"service": service, "ok": True}

    monkeypatch.setattr(volume_agent, "available", lambda: True)
    monkeypatch.setattr(volume_agent, "restart_service", record)

    restarted, warnings = restore_runs._restart_services(
        appliance, ["watcher", "app", "worker"]
    )

    assert asked == ["watcher", "app"]
    assert restarted == ["watcher", "app"]
    assert any("docker compose restart worker" in warning for warning in warnings)


def test_a_restore_that_could_not_restart_everything_says_so_where_the_ledger_shows_it(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """An appliance that looks restored and does no work is the failure being prevented.

    The run table prints one piece of free text per row. A restore that came back without
    its worker has to put its instructions there, on a run whose status is still
    ``completed`` — because the restore did work, and what is left is one command.
    """
    from knowledge_index.backup import restore_runs

    with factory() as session:
        record = PipelineRun(
            provider="local", workflow="restore", status="running", counters={}
        )
        session.add(record)
        session.commit()
        run_id = record.id

    restore_runs._record_outcome(
        factory,
        run_id,
        "ki-backup-20260728T020000Z",
        {
            "applying": True,
            "trigger": "api",
            "components_planned": 1,
            "warnings": ["the dump was taken at schema revision 0f21c3"],
        },
        {
            "backup_id": "ki-backup-20260728T020000Z",
            "staged": [],
            "applied": [{"component": "postgres/ki"}],
            "restarted": ["watcher", "app"],
            "warnings": ["restart worker by hand, last: `docker compose restart worker`."],
        },
        tmp_path,
    )

    with factory() as session:
        restored = session.get(PipelineRun, run_id)
        assert restored.status == "completed"
        assert restored.last_error["class"] == "RestartRequired"
        assert "docker compose restart worker" in restored.last_error["message"]
        assert restored.counters["restarted"] == ["watcher", "app"]
        # And the warnings the plan raised are still beside it rather than replaced by it.
        assert any("schema revision" in warning for warning in restored.counters["warnings"])


def test_a_restore_run_ends_by_asking_for_the_restarts_the_stores_it_applied_need(
    appliance: AppConfig,
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the deriving and the restarting both being right and nothing joining them.

    Every other test here reaches past the run and calls ``services_to_restart`` or
    ``_restart_services`` directly. With the two lines that call them from a restore taken
    out, all of those stayed green while every restore went back to ending exactly where it
    used to — the estate written back and the appliance still talking to a database that is
    no longer there. The wiring is the thing that was missing before, so the wiring is what
    this drives.
    """
    from knowledge_index.backup import restore as restore_module
    from knowledge_index.backup import restore_runs, volume_agent

    monkeypatch.setattr("knowledge_index.sync.runs._submit_local", lambda work: None)
    _seed(factory)
    backup_id = new_backup_id()
    run_id = backup_runs._reserve_run(factory, appliance, backup_id, "schedule", False)
    backup_runs.execute_backup_run(factory, appliance, run_id)

    # pg_restore itself is stood in for. What is under test is which restarts the run asks
    # for, and restoring this dump for real would take with it the ledger row the
    # assertions below read back.
    monkeypatch.setattr(
        restore_module,
        "apply_database",
        lambda config, staged, **kwargs: {"component": staged.name, "ok": True},
    )
    asked: list[str] = []
    monkeypatch.setattr(volume_agent, "available", lambda: True)
    monkeypatch.setattr(volume_agent, "restart_service", lambda service: asked.append(service))

    enqueued = restore_runs.enqueue_restore(
        factory, appliance, backup_id=backup_id, apply_databases=True
    )
    summary = restore_runs.execute_restore_run(factory, appliance, enqueued.run_id)

    # postgres/ki is the only store applied, and on this appliance the restore runs in the
    # app, so the app is the one that is named rather than bounced.
    assert asked == ["watcher", "worker"]
    assert summary["restarted"] == ["watcher", "worker"]
    with factory() as session:
        record = session.get(PipelineRun, enqueued.run_id)
        assert record.status == "completed"
        assert record.last_error["class"] == "RestartRequired"
        assert "docker compose restart app" in record.last_error["message"]


def test_a_restore_is_not_refused_by_the_backup_the_ledger_it_restored_was_taking(
    appliance: AppConfig,
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ghost a restore leaves blocks the next restore too, and that is the worse half.

    An operator who has just restored the wrong backup, or restored one store and now
    wants another, meets "a backup or restore is already in flight" naming a run that came
    out of the archive. The reservation that refuses is this one, so it releases the same
    ghost the backup reservation does.
    """
    from knowledge_index.backup import restore_runs

    monkeypatch.setattr("knowledge_index.sync.runs._submit_local", lambda work: None)
    _seed(factory)
    backup_id = new_backup_id()
    run_id = backup_runs._reserve_run(factory, appliance, backup_id, "schedule", False)
    backup_runs.execute_backup_run(factory, appliance, run_id)

    staged = backup_restore.stage_backup(
        appliance, backup_id, tmp_path / "staged", session_factory=factory
    )
    dump = next(item for item in staged if item.name == "postgres/ki")
    backup_restore.apply_database(appliance, dump)
    with factory() as session:
        assert session.get(PipelineRun, run_id).status == "running", (
            "the dump was expected to hold this backup mid-flight; without that there is "
            "nothing here to release"
        )

    enqueued = restore_runs.enqueue_restore(factory, appliance, backup_id=backup_id)

    assert enqueued.backup_id == backup_id
    with factory() as session:
        assert session.get(PipelineRun, run_id).status == "failed"
        assert session.get(PipelineRun, enqueued.run_id).status == "queued"


# ---------------------------------------------------------------------------- run ledger


def test_a_backup_run_is_reserved_before_any_work_and_refuses_a_second(
    appliance: AppConfig, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list = []
    monkeypatch.setattr("knowledge_index.sync.runs._submit_local", submitted.append)

    first = backup_runs.enqueue_backup(factory, appliance, trigger="cli")
    with factory() as session:
        record = session.get(PipelineRun, first.run_id)
        assert record.workflow == "backup" and record.status == "queued"
        assert record.counters["backup_id"] == first.backup_id
        assert record.counters["trigger"] == "cli"
    with pytest.raises(backup_runs.BackupNotConfigured, match="already in flight"):
        backup_runs.enqueue_backup(factory, appliance)
    assert len(submitted) == 1


def test_a_backup_is_refused_while_documents_are_mid_pipeline(
    appliance: AppConfig, factory: sessionmaker[Session]
) -> None:
    appliance.backup.require_settled_pipeline = True
    _unsettled(factory)

    run_id = backup_runs._reserve_run(factory, appliance, new_backup_id(), "cli", False)
    with pytest.raises(backup_runs.BackupRunFailed, match="mid-pipeline"):
        backup_runs.execute_backup_run(factory, appliance, run_id)
    with factory() as session:
        record = session.get(PipelineRun, run_id)
        assert record.status == "failed"
        assert "mid-pipeline" in record.last_error["message"]


def test_an_end_to_end_run_records_its_outcome_on_the_ledger(
    appliance: AppConfig, factory: sessionmaker[Session]
) -> None:
    _seed(factory)
    run_id = backup_runs._reserve_run(factory, appliance, new_backup_id(), "schedule", False)
    backup_runs.execute_backup_run(factory, appliance, run_id)
    with factory() as session:
        record = session.get(PipelineRun, run_id)
        assert record.status == "completed" and record.progress == 1
        assert record.counters["verified"] is True
        assert record.counters["components_captured"] >= 3
        assert record.finished_at is not None


def test_a_running_backup_names_its_staging_directory_only_while_it_holds_it(
    appliance: AppConfig, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the marker drifting apart from the directory it stands for.

    A reservation reads this counter to decide whether a backup is alive, so a run that
    recorded the directory after the capture had started would be released as a ghost
    while it was capturing, and one that never gave it back would be released while it was
    verifying. Either way the appliance ends up running two backups through one staging
    directory.
    """
    from knowledge_index.backup import components as backup_components

    _seed(factory)
    backup_id = new_backup_id()
    run_id = backup_runs._reserve_run(factory, appliance, backup_id, "cli", False)

    real_collect = backup_components.collect
    during: dict = {}

    def spy(*args, **kwargs):
        with factory() as session:
            named = session.get(PipelineRun, run_id).counters.get("staging_dir")
        during["named"] = named
        during["exists"] = bool(named) and Path(named).is_dir()
        yield from real_collect(*args, **kwargs)

    monkeypatch.setattr(backup_components, "collect", spy)
    backup_runs.execute_backup_run(factory, appliance, run_id)

    assert during["named"] == str(Path(os.environ["KI_BACKUP_STAGING_DIR"]) / backup_id)
    assert during["exists"] is True
    with factory() as session:
        assert session.get(PipelineRun, run_id).counters["staging_dir"] == ""


def test_a_backup_that_captured_itself_does_not_block_the_next_one_after_a_restore(
    appliance: AppConfig,
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure that took a live appliance's backups out until a row was deleted by hand.

    A backup dumps this database while its own run row reads ``running``, so restoring
    that dump puts a backup in flight back on the ledger — one no worker has ever heard
    of, which then refuses every later backup and every later restore.
    """
    submitted: list = []
    monkeypatch.setattr("knowledge_index.sync.runs._submit_local", submitted.append)

    _seed(factory)
    backup_id = new_backup_id()
    run_id = backup_runs._reserve_run(factory, appliance, backup_id, "schedule", False)
    backup_runs.execute_backup_run(factory, appliance, run_id)

    # Restore the ledger over itself, which is what the admin UI does to postgres/ki.
    staged = backup_restore.stage_backup(
        appliance, backup_id, tmp_path / "staged", session_factory=factory
    )
    dump = next(item for item in staged if item.name == "postgres/ki")
    backup_restore.apply_database(appliance, dump)

    with factory() as session:
        ghost = session.get(PipelineRun, run_id)
        assert ghost is not None and ghost.status == "running", (
            "the dump was expected to hold this run mid-flight; without that there is "
            "nothing here to release"
        )

    second = backup_runs.enqueue_backup(factory, appliance, trigger="api")
    assert second.run_id != run_id and len(submitted) == 1
    with factory() as session:
        assert session.get(PipelineRun, second.run_id).status == "queued"
        released = session.get(PipelineRun, run_id)
        assert released.status == "failed"
        assert released.last_error["class"] == "RestoredRun"
        assert released.finished_at is not None


def test_a_backup_still_holding_its_staging_directory_is_not_released(
    appliance: AppConfig, factory: sessionmaker[Session]
) -> None:
    """The half that must not break: a backup in the middle of capturing still refuses.

    Two backups sharing one staging directory and one snapshot repository is the failure
    the single-reservation rule exists to prevent, and it is a worse one than a ledger
    that needs a row cleared by hand.
    """
    from knowledge_index.backup import components as backup_components

    backup_id = new_backup_id()
    staging = backup_components.prepare_staging(backup_id)
    with factory() as session:
        session.add(
            PipelineRun(
                workflow="backup",
                status="running",
                current_step="capturing postgres/ki",
                started_at=datetime.now(UTC),
                counters={"backup_id": backup_id, "staging_dir": str(staging)},
            )
        )
        session.commit()

    with pytest.raises(backup_runs.BackupNotConfigured, match="already in flight"):
        backup_runs.enqueue_backup(factory, appliance)
    with factory() as session:
        assert session.scalars(select(PipelineRun)).one().status == "running"


def test_a_backup_that_has_given_its_staging_directory_back_is_still_in_flight(
    appliance: AppConfig, factory: sessionmaker[Session]
) -> None:
    """A backup past its capture is verifying, not finished, and still refuses a second.

    Verification reads the whole archive back and can take longer than the capture did.
    It holds no staging directory while it does, so a check that only asked whether the
    directory was there would start a second backup against the same destination.
    """
    with factory() as session:
        session.add(
            PipelineRun(
                workflow="backup",
                status="running",
                current_step="verifying",
                started_at=datetime.now(UTC),
                counters={"backup_id": new_backup_id(), "staging_dir": ""},
            )
        )
        session.commit()

    with pytest.raises(backup_runs.BackupNotConfigured, match="already in flight"):
        backup_runs.enqueue_backup(factory, appliance)
    with factory() as session:
        assert session.scalars(select(PipelineRun)).one().status == "running"


def test_a_backup_is_not_released_in_the_gap_between_deleting_its_staging_directory_and_saying_so(
    appliance: AppConfig, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one window a reservation must not be able to land in.

    Deleting the staging directory and recording that it is gone are two writes, and
    between them the run row says precisely what a restored ghost says: active, naming a
    directory that is not on the disk. A reservation arriving then — the nightly schedule,
    or an operator clicking Back up — would fail a run that is about to write its manifest
    and verify, and put a second backup on the same destination and the same snapshot
    repository.
    """
    from knowledge_index.backup import components as backup_components

    monkeypatch.setattr("knowledge_index.sync.runs._submit_local", lambda job: None)
    _seed(factory)
    run_id = backup_runs._reserve_run(factory, appliance, new_backup_id(), "schedule", False)

    real_clear = backup_components.clear_staging
    intruder: dict = {}

    def clear_and_reserve(directory: Path) -> None:
        real_clear(directory)
        # Exactly what a second operator's click does, at the only instant it could be
        # mistaken for the recovery case.
        try:
            backup_runs.enqueue_backup(factory, appliance, trigger="api")
            intruder["refused"] = None
        except backup_runs.BackupNotConfigured as exc:
            intruder["refused"] = str(exc)

    monkeypatch.setattr(backup_components, "clear_staging", clear_and_reserve)
    backup_runs.execute_backup_run(factory, appliance, run_id)

    assert intruder["refused"] is not None and "already in flight" in intruder["refused"]
    with factory() as session:
        assert session.get(PipelineRun, run_id).status == "completed"
        assert session.scalars(select(PipelineRun)).one().id == run_id


def test_preflight_reports_an_unwritable_destination_before_anything_is_dumped(
    appliance: AppConfig, factory: sessionmaker[Session]
) -> None:
    appliance.backup.destination = BackupDestinationConfig(
        kind="local", path="/proc/definitely-not-writable"
    )
    report = backup_runs.preflight(appliance, factory)
    assert report["ok"] is False
    assert any("not writable" in problem or "cannot be created" in problem
               for problem in report["problems"])


def test_preflight_names_a_missing_encryption_key(
    appliance: AppConfig, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing key stops a backup that is switched on, and says what to press."""
    backup_secrets.forget(backup_secrets.ENCRYPTION_KEY, factory)
    report = backup_runs.preflight(appliance, factory)
    assert report["ok"] is False
    assert any("No backup key is set" in problem for problem in report["problems"])
    # And it points at the button rather than at a shell command.
    assert any("Generate" in problem for problem in report["problems"])
    assert report["encryption"]["key_set"] is False


def test_a_fresh_appliance_is_not_told_it_is_broken(
    appliance: AppConfig, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backups start switched off, and nothing about that is a fault.

    An install that opens this page to a wall of red — no key, no destination, stores
    "not ready" — has been told it is broken when it is merely not set up yet, and the
    report that matters later is the one it has learned to ignore. Everything is still
    reported; it is just not called a problem until backups are actually on.
    """
    backup_secrets.forget(backup_secrets.ENCRYPTION_KEY, factory)
    appliance.backup.enabled = False

    report = backup_runs.preflight(appliance, factory)

    assert report["problems"] == []
    assert report["ok"] is True
    # The facts are all still there for the page to show.
    assert report["encryption"]["key_set"] is False
    assert report["warnings"]


def test_a_store_with_nothing_in_it_yet_is_not_a_fault(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Nobody has uploaded a file yet. That is not "not ready", it is empty."""
    shutil.rmtree(tmp_path / "data" / "browser-sources")

    plans = {item.name: item for item in __import__(
        "knowledge_index.backup.components", fromlist=["plan"]
    ).plan(appliance)}

    uploaded = plans["files/uploaded"]
    assert uploaded.ready is True, uploaded.problem
    assert uploaded.detail["empty"] is True

    # And a backup still captures it, so the manifest records that it was covered.
    summary = backup_runs.perform_backup(appliance, new_backup_id(), session_factory=factory)
    assert "files/uploaded" in {item["name"] for item in summary["manifest"]["components"]}


# ----------------------------------------------------------------------------- schedule


def _at(hour: int, minute: int = 0, day: int = 28) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=UTC)


def _attempt(factory: sessionmaker[Session], *, at: datetime) -> None:
    """A finished backup attempt at a given instant, so due-ness has a history to read."""
    with factory() as session:
        session.add(
            PipelineRun(
                provider="local",
                workflow=backup_runs.WORKFLOW,
                status="completed",
                created_at=at,
                finished_at=at,
            )
        )
        session.commit()


def test_the_schedule_fires_once_for_an_occurrence_and_not_again(
    appliance: AppConfig, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list = []
    monkeypatch.setattr("knowledge_index.sync.runs._submit_local", submitted.append)
    appliance.backup.schedule.enabled = True
    appliance.backup.schedule.hour = 2
    # Last night's backup already happened, so the only question is tonight's occurrence.
    _attempt(factory, at=_at(2, 5, day=27))

    assert backup_scheduler.tick(factory, appliance, now=_at(1, 59)).enqueued is None
    report = backup_scheduler.tick(factory, appliance, now=_at(2, 1))
    assert report.enqueued is not None

    with factory() as session:
        session.get(PipelineRun, report.enqueued).status = "completed"
        session.commit()
    # A second tick in the same night must not take a second backup.
    assert backup_scheduler.tick(factory, appliance, now=_at(3, 0)).enqueued is None


def test_a_failed_backup_is_not_retried_every_minute_of_the_night(
    appliance: AppConfig, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Due-ness is measured from the last attempt, not the last success."""
    monkeypatch.setattr("knowledge_index.sync.runs._submit_local", lambda work: None)
    appliance.backup.schedule.enabled = True
    first = backup_scheduler.tick(factory, appliance, now=_at(2, 1))
    with factory() as session:
        session.get(PipelineRun, first.enqueued).status = "failed"
        session.commit()
    assert backup_scheduler.tick(factory, appliance, now=_at(2, 2)).enqueued is None


def test_a_missed_night_is_taken_when_the_appliance_comes_back(
    appliance: AppConfig, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interval would have drifted; a wall-clock schedule read from the ledger does not."""
    monkeypatch.setattr("knowledge_index.sync.runs._submit_local", lambda work: None)
    appliance.backup.schedule.enabled = True
    # Switched off all night, back at nine in the morning: the 02:00 occurrence is owed.
    assert backup_scheduler.tick(factory, appliance, now=_at(9, 0)).enqueued is not None


def test_the_schedule_waits_for_a_busy_pipeline_then_reports_rather_than_going_quiet(
    appliance: AppConfig, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("knowledge_index.sync.runs._submit_local", lambda work: None)
    appliance.backup.schedule.enabled = True
    appliance.backup.schedule.defer_limit_minutes = 60
    _unsettled(factory)

    waiting = backup_scheduler.tick(factory, appliance, now=_at(2, 30))
    assert waiting.deferred is True and waiting.enqueued is None
    # Past the deferral window it enqueues anyway, so the outcome is recorded as a visible
    # failed run instead of another silent night.
    past = backup_scheduler.tick(factory, appliance, now=_at(3, 30))
    assert past.enqueued is not None and past.warnings


def test_the_schedule_does_nothing_while_backups_are_disabled(
    appliance: AppConfig, factory: sessionmaker[Session]
) -> None:
    appliance.backup.enabled = False
    appliance.backup.schedule.enabled = True
    report = backup_scheduler.tick(factory, appliance, now=_at(2, 1))
    assert report.enqueued is None and "not enabled" in report.skipped


def test_the_schedule_stays_at_the_hour_the_firm_chose_across_daylight_saving(
    appliance: AppConfig,
) -> None:
    """"Every night at two" means two o'clock in Berlin, in January and in July.

    A UTC-only schedule drifts an hour twice a year against the firm that set it, which
    walks the backup window into the working day each spring. The occurrence is an
    instant, so the test asserts what an operator would see on a wall clock.
    """
    appliance.backup.schedule.timezone = "Europe/Berlin"
    appliance.backup.schedule.hour = 2
    appliance.backup.schedule.minute = 0
    berlin = ZoneInfo("Europe/Berlin")

    winter = backup_scheduler.latest_occurrence(
        appliance, datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    )
    summer = backup_scheduler.latest_occurrence(
        appliance, datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    )

    assert winter.astimezone(berlin).hour == 2
    assert summer.astimezone(berlin).hour == 2
    # And they are genuinely different instants in UTC — the point of the exercise.
    assert winter.hour != summer.hour


def test_a_backup_is_owed_exactly_once_on_each_daylight_saving_day(
    appliance: AppConfig, factory: sessionmaker[Session]
) -> None:
    """The two days of the year when a local wall time is missing, or happens twice.

    Spring forward skips 02:00 entirely; autumn back passes it twice. Neither may produce
    two backups in a night, and neither may produce none — a missed night is the failure
    this scheduler exists to make impossible.
    """
    appliance.backup.schedule.timezone = "Europe/Berlin"
    appliance.backup.schedule.enabled = True
    appliance.backup.schedule.hour = 2
    appliance.backup.schedule.defer_while_active = False

    for label, day in (("spring forward", datetime(2026, 3, 29)), ("autumn back", datetime(2026, 10, 25))):
        occurrences = set()
        # Walk the whole local day in ten-minute steps, as the loop would.
        for step in range(0, 24 * 6):
            moment = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(minutes=10 * step)
            occurrences.add(backup_scheduler.latest_occurrence(appliance, moment))
        # One occurrence for the day itself, plus the previous day's, which the early
        # hours are still measured against. Never three, which would be a double backup.
        assert len(occurrences) <= 2, f"{label}: {sorted(occurrences)}"
        assert occurrences, label


def test_a_schedule_nothing_is_watching_is_reported_as_a_problem(
    appliance: AppConfig, factory: sessionmaker[Session]
) -> None:
    """A switched-on schedule over a deployment with no scheduler loop is silent by nature.

    The setting says nightly backups; the machine runs none; nothing in the configuration
    can tell the two apart. That is the same failure the whole feature exists to prevent,
    wearing the feature's own clothes, so preflight has to catch it.
    """
    appliance.backup.schedule.enabled = True

    report = backup_runs.preflight(appliance, factory)

    assert report["schedule"]["enabled"] is True
    assert report["schedule"]["watcher_alive"] is False
    assert any("no scheduler has looked at the clock" in problem for problem in report["problems"])

    # One tick is enough to make it healthy, because a tick is exactly the evidence wanted.
    backup_scheduler.tick(factory, appliance)
    healthy = backup_runs.preflight(appliance, factory)
    assert healthy["schedule"]["watcher_alive"] is True
    assert not any("looked at the clock" in problem for problem in healthy["problems"])


def test_a_schedule_that_is_off_is_not_reported_as_a_dead_watcher(
    appliance: AppConfig, factory: sessionmaker[Session]
) -> None:
    """Nothing watching is only a problem when something was supposed to happen."""
    appliance.backup.schedule.enabled = False
    report = backup_runs.preflight(appliance, factory)
    assert not any("looked at the clock" in problem for problem in report["problems"])


def test_an_unknown_timezone_is_refused_rather_than_silently_becoming_utc(
    appliance: AppConfig,
) -> None:
    from knowledge_index.config import BackupScheduleConfig

    with pytest.raises(ValueError, match="not a timezone"):
        BackupScheduleConfig(timezone="Middle/Earth")


def test_latest_occurrence_looks_backwards_never_forwards(appliance: AppConfig) -> None:
    appliance.backup.schedule.hour = 2
    assert backup_scheduler.latest_occurrence(appliance, _at(1, 0)) == _at(2, 0) - timedelta(days=1)
    assert backup_scheduler.latest_occurrence(appliance, _at(2, 0)) == _at(2, 0)


# ---------------------------------------------------------------------------- the API


def _client(appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path):
    from fastapi.testclient import TestClient

    from knowledge_index.config_store import ConfigStore
    from knowledge_index.web.app import create_app

    store = ConfigStore(tmp_path / "config.json")
    store.save(appliance)
    return TestClient(create_app(factory, store)), store


ADMIN = {"x-ki-principals": "user:local-admin,role:admin"}
MEMBER = {"x-ki-principals": "user:associate"}


def test_every_backup_endpoint_is_admin_only(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A backup is the estate in one file; reading the catalogue is an admin act too."""
    client, _store = _client(appliance, factory, tmp_path)
    gets = [
        "/api/backup/preflight",
        "/api/backup/backups",
        "/api/backup/secrets",
        "/api/backup/folders",
        "/api/backup/restorable",
    ]
    posts = {
        "/api/actions/backup": {},
        "/api/actions/backup-verify": {"backup_id": "ki-backup-20260101T000000Z"},
        "/api/actions/backup-restore-plan": {"backup_id": "ki-backup-20260101T000000Z"},
        "/api/actions/backup-prune": {"dry_run": True},
        # A restore writes the estate back over the running appliance, and the folder
        # endpoints list what this machine can see. Neither is a member's to reach.
        "/api/actions/restore": {"backup_id": "ki-backup-20260101T000000Z"},
        "/api/backup/secrets": {"name": "encryption_key", "value": "x"},
        "/api/backup/generate-key": {},
        "/api/backup/folders": {"path": "/backups", "name": "x"},
    }
    for path in gets:
        assert client.get(path, headers=MEMBER).status_code == 403
    for path, body in posts.items():
        assert client.post(path, json=body, headers=MEMBER).status_code == 403


def test_the_api_takes_lists_and_verifies_a_backup(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path, monkeypatch
) -> None:
    _seed(factory)
    client, _store = _client(appliance, factory, tmp_path)

    preflight = client.get("/api/backup/preflight", headers=ADMIN).json()
    assert preflight["destination"]["writable"] is True
    assert preflight["encryption"]["key_fingerprint"]

    # The orchestrator is "local", so the run executes on the thread pool; drive it
    # synchronously instead so the assertion is about the endpoint, not about timing.
    started: list = []
    monkeypatch.setattr("knowledge_index.sync.runs._submit_local", started.append)
    accepted = client.post("/api/actions/backup", json={}, headers=ADMIN)
    assert accepted.status_code == 202
    backup_id = accepted.json()["backup_id"]
    started[0]()

    listed = client.get("/api/backup/backups", headers=ADMIN).json()
    assert [item["backup_id"] for item in listed] == [backup_id]

    verified = client.post(
        "/api/actions/backup-verify", json={"backup_id": backup_id}, headers=ADMIN
    ).json()
    assert verified["ok"] is True and verified["checked"] >= 3

    plan = client.post(
        "/api/actions/backup-restore-plan", json={"backup_id": backup_id}, headers=ADMIN
    ).json()
    assert plan["ok"] is True
    # The two volume components are honest about needing the stack stopped.
    assert all(step["restorable_here"] for step in plan["steps"] if step["kind"] == "postgres")


def test_the_api_refuses_a_backup_while_backups_are_disabled(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    appliance.backup.enabled = False
    client, _store = _client(appliance, factory, tmp_path)
    response = client.post("/api/actions/backup", json={}, headers=ADMIN)
    assert response.status_code == 409 and "not enabled" in response.json()["detail"]


def test_pruning_through_the_api_defaults_to_reporting_not_deleting(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The one backup operation that destroys a firm's off-machine copies."""
    root = tmp_path / "destination" / "knowledge-index"
    root.mkdir(parents=True)
    (root / "ki-backup-20200101T020000Z").mkdir()
    client, _store = _client(appliance, factory, tmp_path)

    reported = client.post("/api/actions/backup-prune", json={}, headers=ADMIN).json()
    assert reported["dry_run"] is True
    assert (root / "ki-backup-20200101T020000Z").is_dir()


def test_the_backup_configuration_round_trips_through_the_config_api(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, store = _client(appliance, factory, tmp_path)
    draft = client.get("/api/config", headers=ADMIN).json()
    draft["backup"]["schedule"]["enabled"] = True
    draft["backup"]["schedule"]["hour"] = 3
    draft["backup"]["retention"]["monthly"] = 12
    assert client.put("/api/config", json=draft, headers=ADMIN).status_code == 200

    saved = store.get().backup
    assert saved.schedule.enabled is True and saved.schedule.hour == 3
    assert saved.retention.monthly == 12


def test_the_config_api_refuses_unencrypted_secrets_rather_than_saving_them(
    appliance: AppConfig, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The admin UI cannot be clicked into writing connector keys onto a NAS in the clear."""
    client, store = _client(appliance, factory, tmp_path)
    draft = client.get("/api/config", headers=ADMIN).json()
    draft["backup"]["encrypt"] = False
    draft["backup"]["sources"]["environment_secrets"] = True
    assert client.put("/api/config", json=draft, headers=ADMIN).status_code == 422
    assert store.get().backup.encrypt is True
