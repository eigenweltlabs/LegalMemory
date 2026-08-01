"""The whole estate, captured by the appliance itself and restored somewhere else.

``tests/test_backup.py`` covers the backup machinery against one Postgres and two
directories, with the gateway databases, the orchestrator database and the search index
switched off and neither container-owned volume mounted. It is a good test of five
components. The other five are exactly the five that shipped broken, one at a time, and
were each found by hand on a running appliance days apart:

1. ``postgres/litellm`` and ``postgres/langfuse`` failed to authenticate, because the URLs
   derived from the primary were built with ``str()`` on a SQLAlchemy URL and SQLAlchemy
   renders a password as ``***``. Only the *derived* databases broke, so it read like a
   LiteLLM credentials problem rather than a backup bug.
2. ``opensearch/snapshot`` failed because OpenSearch runs as uid 1000 and the snapshot
   repository volume it shares with the appliance was root-owned.
3. ``volumes/hatchet-config`` failed because its files are 0600 and the backup runs
   unprivileged.
4. ``volumes/keycloak`` could not be restored from the UI at all: it holds an embedded H2
   database that is open and memory-mapped while Keycloak runs.
5. Restoring the databases left every service that owned one broken, because
   ``pg_restore --clean`` drops and recreates types underneath live connection pools.

Every one of those presented as a backup reporting SUCCESS with six of ten components.
That is the failure this file exists to make impossible, so the assertion that matters is
not "the run did not raise" — it is that all ten components are named in the manifest and
that ``warnings`` is empty.

Two of the five needed no stack at all and are pinned by the fast tests at the top. The
rest can only be told apart from a green run by capturing the estate the way the appliance
does: as the unprivileged user in the app container, through the read-only mounts the
compose file gives it, against the live OpenSearch and the live Hatchet database. So the
capture runs there — the product's own :func:`perform_backup`, driven over ``docker exec``
— and everything afterwards runs here, reading the backup off the same mount an operator
would, decrypting it with the key, and restoring it into scratch databases and a
``tmp_path``. Nothing in this file writes to the appliance's own database, blob store or
search index.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.backup import restore as backup_restore
from knowledge_index.backup import restore_runs, runs as backup_runs
from knowledge_index.backup import secrets as backup_secrets
from knowledge_index.backup.components import (
    _with_database,
    collect,
    libpq_target,
    plan,
    prepare_staging,
)
from knowledge_index.backup.manifest import Manifest, new_backup_id
from knowledge_index.config import (
    AppConfig,
    BackupDestinationConfig,
    BackupRetentionConfig,
)
from knowledge_index.db.models import (
    Blob,
    Chunk,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Matter,
    Source,
    SourceObject,
)

# The ten stores this appliance keeps, in the order a backup captures them. Written out
# rather than derived, because the whole point is to notice when a run produces fewer:
# a set computed from the same code that does the capturing would agree with a backup
# holding six of ten and call it complete.
EVERY_COMPONENT = (
    "postgres/ki",
    "postgres/litellm",
    "postgres/langfuse",
    "postgres/hatchet",
    "opensearch/snapshot",
    "files/artifact-blobs",
    "files/uploaded",
    "files/watched",
    # The appliance's own configuration. Absent from the backup until it was noticed that a
    # recovery onto fresh hardware returned the estate and an appliance that had forgotten
    # where its backups go, when they run, which model does which job and how people sign
    # in. Listed here so it cannot quietly fall out again.
    "files/appliance-config",
    "volumes/keycloak",
    "volumes/hatchet-config",
    "secrets/environment",
)

# Both stores that live in another container's volume. Neither can be applied from a test
# on a developer's machine: replacing them means stopping Keycloak and Hatchet, which is
# the running stack this test suite is talking to. They are asserted on the way in
# instead — see test_the_two_container_owned_volumes_are_captured_byte_for_byte.
CONTAINER_OWNED = ("volumes/keycloak", "volumes/hatchet-config")

COMPOSE_PROJECT = os.environ.get("KI_TEST_COMPOSE_PROJECT", "knowledge-index")
UP_COMMAND = "docker compose up -d"

# Fixed rather than generated, so a run that dies before its teardown leaves a backup the
# next run can still open and delete instead of an orphan nobody has the key for.
ROUNDTRIP_BACKUP_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).decode()
ROUNDTRIP_CONNECTOR_KEY = base64.urlsafe_b64encode(b"roundtrip-connector-key-32bytes!").decode()


# --------------------------------------------------------- the two pure-logic failures


@pytest.mark.parametrize(
    ("primary", "database", "expected_uri", "expected_password"),
    [
        # The case that shipped broken. Everything here is derived from the primary by
        # swapping the database name, so a password lost in the swap takes out LiteLLM and
        # Langfuse together while the appliance's own dump keeps working.
        (
            "postgresql+pg8000://ki:ki-dev-only@postgres:5432/ki",
            "litellm",
            "postgresql://ki@postgres:5432/litellm",
            "ki-dev-only",
        ),
        (
            "postgresql+pg8000://ki:ki-dev-only@postgres:5432/ki",
            "langfuse",
            "postgresql://ki@postgres:5432/langfuse",
            "ki-dev-only",
        ),
        # A password SQLAlchemy percent-encodes on the way in and libpq would misparse
        # inside a URI: it has to survive the derivation and reach PGPASSWORD decoded.
        (
            "postgresql+pg8000://ki:p%40ss%2Fword@postgres:5432/ki",
            "langfuse",
            "postgresql://ki@postgres:5432/langfuse",
            "p@ss/word",
        ),
    ],
)
def test_a_derived_database_url_carries_its_password_out_of_band_and_never_three_stars(
    primary: str, database: str, expected_uri: str, expected_password: str
) -> None:
    """The bug that made a backup hold six of ten stores and report success.

    Two SQLAlchemy behaviours conspire. ``str()`` on a URL renders the password as ``***``,
    and ``URL.set(password=None)`` is a no-op because ``set`` ignores None arguments. Build
    a derived URL with either and you get a string that looks right, carries a literal
    ``***`` where the password belongs, and fails authentication against every database
    derived from it — which is why only LiteLLM and Langfuse broke and the failure read as
    a LiteLLM credentials problem for days.
    """
    derived = _with_database(primary, database)
    assert "***" not in derived, "the derivation rendered the password as ***"
    # Compared through the parser rather than by substring, because a password with
    # reserved characters is percent-encoded inside the URL and decoded on the way out.
    assert make_url(derived).password == expected_password, "the derivation lost the password"
    assert make_url(derived).database == database

    uri, password = libpq_target(derived)
    assert uri == expected_uri
    # Out of band, in PGPASSWORD: an argument is visible in `ps` to anything sharing the
    # namespace, and ends up in whatever process listing an operator pastes into a ticket.
    assert password == expected_password
    assert "***" not in uri
    assert expected_password not in uri


def test_every_source_that_is_switched_on_is_captured_or_names_itself_in_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session]
) -> None:
    """A source that is on and produces nothing at all is the silent two-thirds loss.

    ``plan`` decides what a backup would hold, ``collect`` captures it and the manifest
    records what arrived. Nothing makes those three agree except this: every store the plan
    says is switched on must appear in the manifest, or appear in a warning that names it.
    A component that is quietly neither is a store the firm believes is backed up and is
    not — which is exactly how a run reported success while holding six of ten.

    Deliberately run with most stores unreachable, because the disagreement to catch is the
    one where a store cannot be captured: a skip that names nothing looks identical, on the
    run row and in the admin UI, to a store that was never configured.
    """
    from tests.conftest import TEST_DATABASE_URL

    config = _config_with_every_source(tmp_path, monkeypatch, TEST_DATABASE_URL)
    # After the config, which is what sets KI_CONNECTOR_CREDENTIAL_KEY: the backup key is
    # held in the database encrypted under it, exactly as the admin UI stores it.
    backup_secrets.store(backup_secrets.ENCRYPTION_KEY, ROUNDTRIP_BACKUP_KEY, factory)
    # Reading the estate back is what the integration test below is for; here the question
    # is only which names came out, and re-reading the dumps doubles the runtime for it.
    config.backup.verify_after_write = False

    assert {item.name for item in plan(config) if item.enabled} == set(EVERY_COMPONENT)

    summary = backup_runs.perform_backup(config, new_backup_id(), session_factory=factory)
    captured = {item["name"] for item in summary["manifest"]["components"]}
    warned = set()
    for warning in summary["warnings"]:
        owners = [name for name in EVERY_COMPONENT if warning.startswith(name)]
        assert len(owners) == 1, f"no component owns this warning: {warning!r}"
        warned.add(owners[0])

    assert captured | warned == set(EVERY_COMPONENT), (
        "these stores were switched on and produced neither a component nor a warning: "
        f"{sorted(set(EVERY_COMPONENT) - (captured | warned))}"
    )
    assert not captured & warned, (
        f"these stores were both captured and skipped: {sorted(captured & warned)}"
    )
    # And the coupling is not a coincidence of the ten defaults: switching on the one
    # source that is off by default has to move it into the plan too.
    config.backup.sources.connector_staging = True
    assert "files/connector-staging" in {item.name for item in plan(config) if item.enabled}


def _config_with_every_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, primary_url: str
) -> AppConfig:
    """Every source on, the appliance's own database real, everything else out of reach."""
    artifacts = tmp_path / "data" / "artifacts"
    (artifacts / "blobs").mkdir(parents=True)
    (artifacts / "blobs" / "document.bin").write_bytes(b"a converted document")
    keycloak = tmp_path / "volumes" / "keycloak"
    keycloak.mkdir(parents=True)
    (keycloak / "keycloakdb.mv.db").write_bytes(b"an embedded database")

    monkeypatch.setenv("KI_DATABASE_URL", primary_url)
    monkeypatch.setenv("KI_BACKUP_STAGING_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("KI_CONNECTOR_CREDENTIAL_KEY", ROUNDTRIP_CONNECTOR_KEY)
    monkeypatch.setenv("KI_BACKUP_KEYCLOAK_PATH", str(keycloak))
    # Nothing listens on port 1, so these refuse immediately rather than dumping tens of
    # megabytes this test has no use for.
    monkeypatch.setenv("KI_BACKUP_LITELLM_DATABASE_URL", "postgresql://ki@127.0.0.1:1/litellm")
    monkeypatch.setenv("KI_BACKUP_LANGFUSE_DATABASE_URL", "postgresql://ki@127.0.0.1:1/langfuse")
    monkeypatch.delenv("KI_BACKUP_HATCHET_DATABASE_URL", raising=False)
    monkeypatch.delenv("KI_BACKUP_HATCHET_CONFIG_PATH", raising=False)
    monkeypatch.delenv("KI_BACKUP_OPENSEARCH_REPO_PATH", raising=False)
    monkeypatch.delenv("KI_BACKUP_OPENSEARCH_REPO_CONTAINER_PATH", raising=False)

    config = AppConfig()
    config.artifact_dir = artifacts
    config.components.orchestrator_provider = "local"
    config.backup.enabled = True
    config.backup.encrypt = True
    config.backup.require_settled_pipeline = False
    config.backup.retention = BackupRetentionConfig(prune_enabled=False)
    config.backup.destination = BackupDestinationConfig(
        kind="local", path=str(tmp_path / "destination"), prefix="knowledge-index"
    )
    return config


def test_a_store_that_cannot_be_read_is_a_named_warning_rather_than_an_empty_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session]
) -> None:
    """The shape of failure 3, without needing a 0600 file and an unprivileged user.

    Hatchet's config volume is 0600 and the backup runs as a different user, so the tar
    could not read it. What must never happen is that an unreadable store becomes a small
    archive nobody looks at; it has to abort the component and say which one, so the run
    ends with a warning that names it and the count is short by one.
    """
    from tests.conftest import TEST_DATABASE_URL

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip(
            "running as root, which can read a 0600 file it does not own — this test can "
            "only mean anything as an unprivileged user, which is what the app image uses"
        )
    unreadable = tmp_path / "volumes" / "hatchet-config"
    unreadable.mkdir(parents=True)
    (unreadable / "server.yaml").write_bytes(b"signing keys")
    (unreadable / "server.yaml").chmod(0o000)

    config = _config_with_every_source(tmp_path, monkeypatch, TEST_DATABASE_URL)
    monkeypatch.setenv("KI_BACKUP_HATCHET_CONFIG_PATH", str(unreadable))
    staging = prepare_staging("ki-backup-unreadable")

    names: list[str] = []
    warnings: list[str] = []
    for component, warning in collect(config, staging, "ki-backup-unreadable"):
        if component is not None:
            names.append(component.name)
        if warning is not None:
            warnings.append(warning)

    assert "volumes/hatchet-config" not in names
    assert any(item.startswith("volumes/hatchet-config") for item in warnings), warnings
    shutil.rmtree(staging, ignore_errors=True)


# ------------------------------------------------------------------- the live appliance


@dataclass(frozen=True)
class Appliance:
    """The running stack, and the two paths this test needs to reach into it."""

    container: str
    backups_dir: Path


@dataclass(frozen=True)
class Seed:
    """The small, real estate that goes in, described so a restore can be checked."""

    run_id: str
    source_name: str
    matter_title: str
    documents: int = 3
    versions_per_document: int = 2
    chunks_per_version: int = 2
    blob_bytes: bytes = b""
    blob_hash: str = ""
    upload_name: str = "brief.docx"
    upload_bytes: bytes = b""
    index_name: str = ""

    @property
    def version_count(self) -> int:
        return self.documents * self.versions_per_document

    @property
    def chunk_count(self) -> int:
        return self.version_count * self.chunks_per_version


@dataclass(frozen=True)
class RoundTrip:
    """One real backup of the whole estate, plus everything needed to check it."""

    appliance: Appliance
    backup_id: str
    summary: dict
    manifest: Manifest
    config: AppConfig
    factory: sessionmaker[Session]
    seed: Seed
    staged: dict[str, backup_restore.StagedFile] = field(default_factory=dict)


def _docker(*argv: str, timeout: float = 120.0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", *argv], capture_output=True, timeout=timeout, check=False
    )


def _running_container(service: str) -> str | None:
    try:
        result = _docker(
            "ps",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={COMPOSE_PROJECT}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    identifier = result.stdout.decode().strip().splitlines()
    return identifier[0] if result.returncode == 0 and identifier else None


@pytest.fixture(scope="session")
def appliance() -> Appliance:
    """The compose stack, or a skip naming the exact command that fixes it.

    Every service here is one this backup has to reach: the estate is not the appliance's
    own database, it is ten stores across seven containers, and a test that quietly runs
    without Keycloak or Hatchet is the test that passed through all five of the failures
    this file exists for.

    Deliberately not built on ``live_stack``, which additionally requires LiteLLM and
    Docling Serve. A backup calls no model and converts no document; skipping because the
    gateway is down would be a gap in coverage taken for a reason that is not true.
    """
    if shutil.which("docker") is None:
        pytest.skip(
            "docker is not on PATH — this test captures the estate the way the appliance "
            f"does, from inside its own container. Start the stack with `{UP_COMMAND}`."
        )
    for service in ("app", "postgres", "opensearch", "keycloak", "hatchet", "hatchet-postgres"):
        if _running_container(service) is None:
            pytest.skip(
                f"the compose service {service!r} is not running in project "
                f"{COMPOSE_PROJECT!r} — a backup of the whole estate has to reach all of "
                f"them, and one that is down would be captured as a warning rather than a "
                f"failure. Start the stack with `{UP_COMMAND}`."
            )
    container = _running_container("app")
    assert container is not None  # checked immediately above

    mounts = _docker(
        "inspect",
        container,
        "--format",
        "{{range .Mounts}}{{.Destination}}\t{{.Source}}\n{{end}}",
        timeout=30,
    )
    sources = dict(
        line.split("\t", 1)
        for line in mounts.stdout.decode().splitlines()
        if "\t" in line
    )
    backups = Path(sources.get("/backups", ""))
    if not str(backups) or not backups.is_dir() or not os.access(backups, os.W_OK):
        pytest.skip(
            "the appliance's /backups mount is not a directory this test can read at "
            f"{backups or '(unmounted)'}. It has to be a bind mount so the backup the "
            "appliance writes is the backup this test reads back — set KI_BACKUP_MOUNT to "
            f"a host directory and restart with `{UP_COMMAND}`."
        )
    try:
        httpx.get(_opensearch("_cluster/health"), timeout=10).raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"OpenSearch is not reachable at {_opensearch('')} "
            f"({type(exc).__name__}: {exc}) — this test seeds an index into the live "
            f"cluster and then asserts the snapshot holds it. Start it with `{UP_COMMAND}`."
        )
    return Appliance(container=container, backups_dir=backups)


def _opensearch(path: str) -> str:
    """One place the cluster's address comes from, shared with the rest of the suite."""
    from tests.conftest import OPENSEARCH_URL

    return f"{OPENSEARCH_URL.rstrip('/')}/{path.lstrip('/')}"


# The capture, performed by the appliance itself. Run through the product's own
# perform_backup rather than anything written for the test, in the container that owns the
# read-only mounts, as the unprivileged user the compose file gives it — because three of
# the five failures this file exists for are invisible anywhere else: the snapshot
# repository's ownership, Hatchet's 0600 config, and the credentials of the two databases
# derived from the primary on a server only the compose network can reach.
_CAPTURE_IN_THE_APPLIANCE = r'''
import base64
import io
import json
import os
import pathlib
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from knowledge_index.artifacts import LocalArtifactStore
from knowledge_index.backup import runs as backup_runs
from knowledge_index.config import AppConfig, BackupDestinationConfig, BackupRetentionConfig

artifacts = pathlib.Path(os.environ["KI_ROUNDTRIP_ARTIFACT_DIR"])
store = LocalArtifactStore(artifacts)
blob = base64.b64decode(os.environ["KI_ROUNDTRIP_BLOB"])
stored = store.put_blob(io.BytesIO(blob), max_bytes=1 << 20)

uploads = artifacts.parent / "browser-sources" / "roundtrip"
uploads.mkdir(parents=True, exist_ok=True)
upload = base64.b64decode(os.environ["KI_ROUNDTRIP_UPLOAD"])
(uploads / os.environ["KI_ROUNDTRIP_UPLOAD_NAME"]).write_bytes(upload)

config = AppConfig()
config.artifact_dir = artifacts
config.retrieval.index_name = os.environ["KI_ROUNDTRIP_INDEX"]
config.backup.enabled = True
config.backup.encrypt = True
config.backup.verify_after_write = True
config.backup.require_settled_pipeline = False
config.backup.retention = BackupRetentionConfig(prune_enabled=False)
config.backup.destination = BackupDestinationConfig(
    kind="local", path="/backups", prefix=os.environ["KI_ROUNDTRIP_PREFIX"]
)
sources = config.backup.sources
sources.gateway_databases = True
sources.orchestrator_database = True
sources.search_index = True
sources.artifact_blobs = True
sources.uploaded_files = True
sources.identity_volume = True
sources.orchestrator_config_volume = True
sources.environment_secrets = True
sources.connector_staging = False
sources.extra_paths = []

engine = create_engine(os.environ["KI_DATABASE_URL"])
factory = sessionmaker(engine, expire_on_commit=False)
summary = backup_runs.perform_backup(
    config, os.environ["KI_ROUNDTRIP_BACKUP_ID"], session_factory=factory
)
sys.stdout.write(json.dumps({"summary": summary, "blob_hash": stored.content_hash}, default=str))
'''


@pytest.fixture(scope="module")
def round_trip(
    appliance: Appliance,
    pg_factory: sessionmaker[Session],
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[RoundTrip]:
    """Seed a small estate, back the whole of it up from inside the appliance, stage it.

    Module-scoped because capturing ten stores costs real seconds and every assertion
    below is about the same backup. Deliberately does not use the per-test ``factory``
    fixture: that truncates every table, and the backup key lives in one of them.
    """
    from tests.conftest import TEST_DATABASE_URL

    patch = pytest.MonkeyPatch()
    # The appliance is handed this key for the capture, so the manifest records its
    # fingerprint; the same key has to be in force here or restore_plan reports the
    # connector-key mismatch it is right to report when they differ.
    patch.setenv("KI_CONNECTOR_CREDENTIAL_KEY", ROUNDTRIP_CONNECTOR_KEY)

    run_id = uuid.uuid4().hex[:12]
    prefix = f"roundtrip-{run_id}"
    backup_id = new_backup_id()
    scratch = f"/data/roundtrip-{run_id}"
    tmp_path = tmp_path_factory.mktemp("roundtrip")

    blob_bytes = f"a converted matter document {run_id}\n".encode() * 64
    seed = Seed(
        run_id=run_id,
        source_name=f"Matter archive {run_id}",
        matter_title=f"Roundtrip {run_id}",
        blob_bytes=blob_bytes,
        blob_hash=hashlib.sha256(blob_bytes).hexdigest(),
        upload_bytes=f"an uploaded brief {run_id}\n".encode() * 16,
        index_name=f"ki-roundtrip-{run_id}",
    )

    _reset(pg_factory)
    backup_secrets.store(backup_secrets.ENCRYPTION_KEY, ROUNDTRIP_BACKUP_KEY, pg_factory)
    _seed_database(pg_factory, seed)

    config = AppConfig()
    config.artifact_dir = tmp_path / "restored" / "artifacts"
    config.components.orchestrator_provider = "local"
    config.retrieval.index_name = seed.index_name
    config.backup.enabled = True
    config.backup.encrypt = True
    config.backup.retention = BackupRetentionConfig(prune_enabled=False)
    config.backup.destination = BackupDestinationConfig(
        kind="local", path=str(appliance.backups_dir), prefix=prefix
    )
    patch.setenv("KI_DATABASE_URL", TEST_DATABASE_URL)
    patch.setenv("KI_RESTORE_STAGE_DIR", str(tmp_path / "restore-staging"))

    try:
        # Both of these inside the try, not before it. The capture is the step most likely
        # to fail — it is the one that reaches all ten stores — and by the time it does it
        # has usually written most of them, so a failure here is precisely the case that
        # leaves an encrypted copy of the estate, and of the deployment's secrets, sitting
        # in the developer's real backup directory with nothing left to clean it up.
        # Verified by breaking the capture on purpose: with this outside the try,
        # `runtime/backups/roundtrip-*`, the container's `/data/roundtrip-*` and the seeded
        # index all survived the run — and a leaked index is then swept into every backup
        # the developer takes afterwards.
        _seed_search_index(seed)
        captured = _capture_in_the_appliance(appliance, seed, prefix, backup_id, scratch)
        assert captured["blob_hash"] == seed.blob_hash, (
            "the appliance stored the blob under a different content hash than the database "
            "rows point at; the seeded estate is not internally consistent"
        )
        manifest = backup_runs.load_manifest(config, backup_id, session_factory=pg_factory)
        staged = backup_restore.stage_backup(
            config, backup_id, tmp_path / "staged", session_factory=pg_factory
        )
        yield RoundTrip(
            appliance=appliance,
            backup_id=backup_id,
            summary=captured["summary"],
            manifest=manifest,
            config=config,
            factory=pg_factory,
            seed=seed,
            staged={item.name: item for item in staged},
        )
    finally:
        shutil.rmtree(appliance.backups_dir / prefix, ignore_errors=True)
        _docker("exec", appliance.container, "rm", "-rf", scratch, timeout=60)
        httpx.delete(_opensearch(seed.index_name), timeout=30)
        patch.undo()


def _reset(factory: sessionmaker[Session]) -> None:
    """Empty the test database, so a restored row count is a number rather than a floor."""
    from knowledge_index.db.models import Base

    tables = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
    with factory() as session:
        session.execute(text("SET lock_timeout = '15s'"))
        session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        session.commit()


def _seed_database(factory: sessionmaker[Session], seed: Seed) -> None:
    """One source, three documents with two versions and two chunks each, one blob.

    Small enough that the whole round trip is seconds, real enough that a restore can be
    checked by counting rows and following the foreign keys back to the blob on disk.
    """
    with factory() as session:
        source = Source(
            kind="local_fs",
            display_name=seed.source_name,
            config={"root": f"/estate/{seed.run_id}"},
            sync_policy={"mode": "manual"},
        )
        matter = Matter(title=seed.matter_title, reference_numbers=[f"M-{seed.run_id}"])
        session.add_all([source, matter])
        session.add(
            Blob(
                content_hash=seed.blob_hash,
                size_bytes=len(seed.blob_bytes),
                mime_sniffed="application/pdf",
            )
        )
        session.flush()
        for index in range(seed.documents):
            obj = SourceObject(
                source_id=source.id,
                external_id=f"obj-{seed.run_id}-{index}",
                path=f"/estate/{seed.run_id}/brief-{index}.docx",
                name=f"brief-{index}.docx",
                content_hash=seed.blob_hash,
            )
            document = Document(
                matter_id=matter.id, doc_type="brief", title=f"Brief {index}", language="de"
            )
            session.add_all([obj, document])
            session.flush()
            for ordinal in range(seed.versions_per_document):
                version = DocumentVersion(
                    document_id=document.id,
                    content_hash=seed.blob_hash,
                    ordinal=ordinal + 1,
                    status="final" if ordinal else "draft",
                )
                session.add(version)
                session.flush()
                session.add(
                    DocumentVersionSource(version_id=version.id, source_object_id=obj.id)
                )
                for part in range(seed.chunks_per_version):
                    session.add(
                        Chunk(
                            document_version_id=version.id,
                            ordinal=part,
                            text=f"clause {part} of brief {index} version {ordinal}",
                            matter_id=matter.id,
                            document_id=document.id,
                            doc_type="brief",
                        )
                    )
        session.commit()


def _seed_search_index(seed: Seed) -> None:
    """An index of this test's own, in the live cluster the snapshot will capture.

    The snapshot takes every index except OpenSearch's internals, so asserting on one this
    test put there is what distinguishes a real capture from an empty repository archived
    with a green tick on it.
    """
    base = _opensearch(seed.index_name)
    httpx.delete(base, timeout=30)
    httpx.put(
        base,
        json={"settings": {"number_of_shards": 1, "number_of_replicas": 0}},
        timeout=30,
    ).raise_for_status()
    for ordinal in range(2):
        httpx.put(
            f"{base}/_doc/{ordinal}",
            json={"text": f"clause {ordinal} of {seed.matter_title}"},
            timeout=30,
        ).raise_for_status()
    httpx.post(f"{base}/_refresh", timeout=30).raise_for_status()


def _capture_in_the_appliance(
    appliance: Appliance, seed: Seed, prefix: str, backup_id: str, scratch: str
) -> dict:
    """Run the product's own backup inside the app container, and bring back its summary."""
    from tests.conftest import TEST_DATABASE

    environment = {
        # The appliance reaches the same Postgres server this suite uses, by its name on
        # the compose network. Pointed at the test database so nothing here touches the
        # developer's own estate — but at the real server, so the LiteLLM and Langfuse
        # databases derived from it are the real ones.
        "KI_DATABASE_URL": f"postgresql+pg8000://ki:ki-dev-only@postgres:5432/{TEST_DATABASE}",
        "KI_CONNECTOR_CREDENTIAL_KEY": ROUNDTRIP_CONNECTOR_KEY,
        "KI_BACKUP_STAGING_DIR": f"{scratch}/staging",
        "KI_ROUNDTRIP_ARTIFACT_DIR": f"{scratch}/artifacts",
        "KI_ROUNDTRIP_PREFIX": prefix,
        "KI_ROUNDTRIP_BACKUP_ID": backup_id,
        "KI_ROUNDTRIP_INDEX": seed.index_name,
        "KI_ROUNDTRIP_BLOB": base64.b64encode(seed.blob_bytes).decode(),
        "KI_ROUNDTRIP_UPLOAD": base64.b64encode(seed.upload_bytes).decode(),
        "KI_ROUNDTRIP_UPLOAD_NAME": seed.upload_name,
    }
    argv = ["exec", "--interactive"]
    for name, value in environment.items():
        argv += ["--env", f"{name}={value}"]
    argv += [appliance.container, "python", "-"]
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", *argv],
        input=_CAPTURE_IN_THE_APPLIANCE.encode(),
        capture_output=True,
        timeout=3600,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "the appliance could not capture its own estate "
            f"(exit {result.returncode}):\n{result.stderr.decode()[-4000:]}"
        )
    return json.loads(result.stdout.decode())


@contextmanager
def _scratch_database(name: str) -> Iterator[tuple[str, object]]:
    """An empty database to restore into, dropped and recreated first, never the live one.

    Created and dropped on an autocommit connection, because ``DROP DATABASE`` cannot run
    inside a transaction block and a pooled ORM session opens one for every statement.
    """
    from tests.conftest import POSTGRES_ADMIN_URL, TEST_DATABASE_URL

    admin = create_engine(POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{name}"
    engine = create_engine(url)
    with engine.connect() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.commit()
    try:
        yield url, engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _extract(archive: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        tar.extractall(target, filter="data")
    return target


def _read_in_the_appliance(appliance: Appliance, path: str) -> bytes:
    result = _docker("exec", appliance.container, "cat", path, timeout=60)
    assert result.returncode == 0, f"could not read {path}: {result.stderr.decode()}"
    return result.stdout


# ------------------------------------------------------------------------ the round trip


@pytest.mark.integration
def test_a_full_backup_captures_all_ten_stores_and_reports_not_one_warning(
    round_trip: RoundTrip,
) -> None:
    """The assertion this whole file exists for.

    A backup that reports success while holding six of ten components is not a crash and
    is not visible on the run row without reading the warnings, which is why it survived
    five separate releases. So the count is not enough: the component *names* are checked
    against the ten stores this appliance keeps, and the warning list has to be empty with
    its contents in the failure message, because every one of the five failures announced
    itself there and nowhere else.
    """
    summary = round_trip.summary
    captured = [item["name"] for item in summary["manifest"]["components"]]

    assert summary["warnings"] == [], (
        "the backup reported success with these stores missing:\n  "
        + "\n  ".join(summary["warnings"])
    )
    assert sorted(captured) == sorted(EVERY_COMPONENT), (
        f"missing: {sorted(set(EVERY_COMPONENT) - set(captured))}; "
        f"unexpected: {sorted(set(captured) - set(EVERY_COMPONENT))}"
    )
    assert summary["components_captured"] == len(EVERY_COMPONENT)
    assert summary["components_planned"] == len(EVERY_COMPONENT)
    assert summary["encrypted"] is True
    # The appliance reads its own backup back before calling it done. That it did so here,
    # inside the container, is a separate fact from this test reading it back below.
    assert summary["verified"]["ok"] is True, summary["verified"]["problems"]
    assert summary["verified"]["deep"] is True
    # Every component non-empty. An archive of nothing has a valid checksum too.
    for component in summary["manifest"]["components"]:
        assert component["plaintext_bytes"] > 0, f"{component['name']} captured zero bytes"


@pytest.mark.integration
def test_the_backup_reads_back_decrypts_and_re_checksums_from_outside_the_appliance(
    round_trip: RoundTrip,
) -> None:
    """Verification done by a process that has only the share and the key.

    The appliance verifies its own backup at the end of a run, which proves the bytes it
    wrote are the bytes it can read. This proves the harder thing: that a machine which is
    not the appliance — no mounts, no containers, just the directory and the key — can open
    every component and get back what the manifest says. That is the only version of the
    claim that survives the appliance being gone.
    """
    report = backup_runs.verify_backup(
        round_trip.config, round_trip.backup_id, session_factory=round_trip.factory
    )
    assert report["ok"] is True, report["problems"]
    assert report["deep"] is True
    assert report["checked"] == len(EVERY_COMPONENT)
    assert {item["name"] for item in report["components"]} == set(EVERY_COMPONENT)
    assert all(item["decrypted"] for item in report["components"])


@pytest.mark.integration
def test_every_staged_component_is_the_bytes_its_manifest_entry_describes(
    round_trip: RoundTrip,
) -> None:
    """Staging is the half of a restore a firm should rehearse; it has to prove itself.

    ``stage_backup`` re-checks each component against the manifest as it lands and refuses
    the backup otherwise, so this re-does the check independently: a staging step that
    silently trusted the manifest would still return a list of files and still look right.
    """
    assert set(round_trip.staged) == set(EVERY_COMPONENT)
    for name, staged in sorted(round_trip.staged.items()):
        recorded = round_trip.manifest.component(name)
        assert recorded is not None, f"{name} was staged but is not in the manifest"
        digest = hashlib.sha256(staged.path.read_bytes()).hexdigest()
        assert staged.path.stat().st_size == recorded.plaintext_bytes, name
        assert digest == recorded.plaintext_sha256, f"{name} is not the bytes that went in"
        assert recorded.encrypted is True, f"{name} left the appliance unsealed"
        # Owner-only: staging turns the estate, and the connector credential key with it,
        # back into plaintext on whatever machine is rehearsing the restore.
        assert staged.path.stat().st_mode & 0o077 == 0, f"{name} is readable by others"


@pytest.mark.integration
def test_the_appliance_database_restores_into_an_empty_database_with_its_rows_intact(
    round_trip: RoundTrip,
) -> None:
    """The question the feature exists to answer, asked of a database that is not the live one.

    Restored into a scratch database rather than over the appliance, because
    ``pg_restore --clean`` drops and recreates every type underneath whatever is connected
    — which is failure 5, and doing it to the developer's running stack to prove the point
    would be the same mistake.
    """
    seed = round_trip.seed
    with _scratch_database(f"ki_roundtrip_{seed.run_id}") as (url, engine):
        result = backup_restore.apply_database(
            round_trip.config, round_trip.staged["postgres/ki"], target_url=url
        )
        assert result["ok"] is True, result["serious_errors"]

        with engine.connect() as connection:
            counts = {
                table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                for table in ("sources", "source_objects", "documents", "document_versions",
                              "chunks", "blobs")
            }
            names = connection.execute(text("SELECT display_name FROM sources")).scalars().all()
            hashes = connection.execute(text("SELECT content_hash FROM blobs")).scalars().all()

    assert counts == {
        "sources": 1,
        "source_objects": seed.documents,
        "documents": seed.documents,
        "document_versions": seed.version_count,
        "chunks": seed.chunk_count,
        "blobs": 1,
    }
    assert list(names) == [seed.source_name]
    assert list(hashes) == [seed.blob_hash]


@pytest.mark.integration
def test_the_three_databases_the_appliance_does_not_own_are_real_dumps_with_data_in_them(
    round_trip: RoundTrip,
) -> None:
    """Failure 1, end to end: a derived credential that fails is a dump that never happens.

    LiteLLM's and Langfuse's URLs are derived from the primary by swapping the database
    name, and Hatchet's is a different server reachable only on the compose network. All
    three broke as *missing* components, so the check is not that a file exists but that it
    is a pg_dump archive with table data in it — an empty dump would checksum, stage and
    restore perfectly and hold nothing.

    LiteLLM's is restored in full, because it is the one that shipped broken. The other two
    are checked through their table of contents: restoring a further 70 MB proves the same
    fact about the same code path and costs a minute of every run.
    """
    for name in ("postgres/litellm", "postgres/langfuse", "postgres/hatchet"):
        staged = round_trip.staged[name]
        listing = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["pg_restore", "--list", str(staged.path)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert listing.returncode == 0, f"{name} is not a pg_dump archive: {listing.stderr}"
        tables = [line for line in listing.stdout.splitlines() if "TABLE DATA" in line]
        assert tables, f"{name} contains no table data — it dumped an empty database"

    with _scratch_database(f"ki_roundtrip_litellm_{round_trip.seed.run_id}") as (url, engine):
        result = backup_restore.apply_database(
            round_trip.config, round_trip.staged["postgres/litellm"], target_url=url
        )
        assert result["ok"] is True, result["serious_errors"]
        assert result["database"] == "litellm"
        with engine.connect() as connection:
            restored = connection.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            ).scalar_one()
    assert restored > 0, "the gateway's database restored with no tables in it"


@pytest.mark.integration
def test_the_blob_store_and_the_uploads_come_back_byte_for_byte(
    round_trip: RoundTrip, tmp_path: Path
) -> None:
    """Documents have no upstream to re-fetch from, so the bytes are the whole of the claim.

    Extracted into a scratch artifact directory rather than over the appliance's own: the
    blob store is content-addressed, so writing into the live one would be harmless and
    still wrong — this test must not be the reason a developer's estate changes.
    """
    seed = round_trip.seed
    round_trip.config.artifact_dir = tmp_path / "restored" / "artifacts"

    backup_restore.apply_files(round_trip.config, round_trip.staged["files/artifact-blobs"])
    backup_restore.apply_files(round_trip.config, round_trip.staged["files/uploaded"])

    blob = (
        round_trip.config.artifact_dir
        / "blobs"
        / seed.blob_hash[:2]
        / seed.blob_hash[2:4]
        / seed.blob_hash
    )
    assert blob.is_file(), f"the restored blob store has no {seed.blob_hash}"
    assert blob.read_bytes() == seed.blob_bytes
    # And it is still the blob the restored database rows point at.
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == seed.blob_hash

    upload = (
        round_trip.config.artifact_dir.parent / "browser-sources" / "roundtrip" / seed.upload_name
    )
    assert upload.is_file(), "the restored upload directory is missing the uploaded file"
    assert upload.read_bytes() == seed.upload_bytes


@pytest.mark.integration
def test_the_two_container_owned_volumes_are_captured_byte_for_byte(
    round_trip: RoundTrip, tmp_path: Path
) -> None:
    """Failures 3 and 4, as far as they can honestly be taken here.

    Hatchet's config is 0600 and the backup runs as a different user, so the capture was
    silently short by one store; the check is therefore that the archive holds those exact
    files with those exact bytes, compared against the running container.

    What is deliberately *not* exercised: applying either volume. Both belong to a
    container that has to be stopped first — Keycloak's data is an embedded H2 database,
    open and memory-mapped while it runs — and the restore agent does that by stopping the
    real Keycloak and the real Hatchet, which are the ones this suite is talking to. So the
    apply path is asserted as a *plan* below, and the volume replacement itself is covered
    by ``test_backup.py``'s restore-agent tests against a stub agent. A firm rehearsing the
    real thing runs ``scripts/restore-backup.sh``.
    """
    hatchet = _extract(
        round_trip.staged["volumes/hatchet-config"].path, tmp_path / "hatchet-config"
    )
    for name in ("server.yaml", "database.yaml"):
        restored = hatchet / name
        assert restored.is_file(), f"the config archive is missing {name}"
        live = _read_in_the_appliance(
            round_trip.appliance, f"/backup-sources/hatchet-config/{name}"
        )
        assert restored.read_bytes() == live, f"{name} was not captured as it is on disk"

    keycloak = _extract(round_trip.staged["volumes/keycloak"].path, tmp_path / "keycloak")
    embedded = keycloak / "h2" / "keycloakdb.mv.db"
    assert embedded.is_file(), "the identity archive holds no embedded database"
    assert embedded.stat().st_size > 0
    assert (keycloak / "import" / "knowledge-index-realm.json").is_file()

    # Both are reported as needing the volume replaced with its container stopped, so
    # nobody is offered a restore from the admin UI that would quietly do nothing to a
    # running Keycloak. Matched on "replace the volume" rather than on "stop": the ordinary
    # directory step reads "extract over the directory, with the app and worker stopped",
    # so a substring test for "stop" passes just as happily when a store has fallen out of
    # NEEDS_CONTAINER_STOPPED and is being offered as a plain extract — which is failure 4
    # reintroduced, and is the one thing this assertion is here to notice.
    steps = {
        step["name"]: step
        for step in backup_restore.restore_plan(
            round_trip.config, round_trip.backup_id, round_trip.factory
        )["steps"]
    }
    for name in CONTAINER_OWNED:
        assert "replace the volume" in steps[name]["how"], steps[name]["how"]
    assert "replace the volume" not in steps["files/artifact-blobs"]["how"], (
        "every store is being described as a volume replacement, so the check above says "
        "nothing about these two"
    )


@pytest.mark.integration
def test_the_search_snapshot_holds_the_index_that_was_seeded_into_the_live_cluster(
    round_trip: RoundTrip, tmp_path: Path
) -> None:
    """Failure 2: OpenSearch could not write the repository it shares with the appliance.

    OpenSearch runs as uid 1000 and the shared snapshot volume was root-owned, so the
    snapshot never happened and the component was skipped. Both halves are checked: that
    OpenSearch wrote a snapshot naming the index this test seeded with no failed shards,
    and that the appliance could read the repository back — the archive holds the
    repository metadata rather than an empty directory tarred with a green tick on it.

    Not exercised: applying it. ``apply_search_index`` empties the repository volume and
    then closes and restores every index the snapshot holds, which on this stack is the
    developer's live cluster. Restoring the index is covered against a scratch cluster
    nowhere in this suite, and that is the honest statement of the gap.
    """
    detail = round_trip.manifest.component("opensearch/snapshot").detail
    assert round_trip.seed.index_name in detail["indices"], detail["indices"]
    assert detail["shards"]["failed"] == 0, detail["shards"]
    assert detail["shards"]["successful"] >= 1

    repository = _extract(round_trip.staged["opensearch/snapshot"].path, tmp_path / "repository")
    files = {path.name for path in repository.rglob("*") if path.is_file()}
    assert any(name.startswith("index-") for name in files), sorted(files)[:20]
    assert any(name.startswith("meta-") for name in files), sorted(files)[:20]
    assert (repository / "indices").is_dir(), "the repository archive holds no index data"


@pytest.mark.integration
def test_the_secrets_component_carries_the_connector_key_and_not_the_key_it_is_sealed_under(
    round_trip: RoundTrip,
) -> None:
    """Without KI_CONNECTOR_CREDENTIAL_KEY a restore works until the first token refresh.

    And with the backup key in the same file, the backup would be its own decryption key
    sitting on a share — which is why that one name is excluded by the collector rather
    than by convention.
    """
    payload = json.loads(round_trip.staged["secrets/environment"].path.read_text())
    assert payload["environment"]["KI_CONNECTOR_CREDENTIAL_KEY"] == ROUNDTRIP_CONNECTOR_KEY
    assert "KI_BACKUP_ENCRYPTION_KEY" not in payload["environment"]
    assert ROUNDTRIP_BACKUP_KEY not in json.dumps(payload)
    # The manifest is not encrypted, so its detail block carries names and never values.
    detail = round_trip.manifest.component("secrets/environment").detail
    assert "KI_CONNECTOR_CREDENTIAL_KEY" in detail["variables"]
    assert ROUNDTRIP_CONNECTOR_KEY not in json.dumps(detail)


@pytest.mark.integration
def test_restoring_the_databases_names_every_service_it_has_just_pulled_the_floor_from_under(
    round_trip: RoundTrip,
) -> None:
    """Failure 5, which turned a successful restore into an appliance that did not work.

    ``pg_restore --clean --if-exists`` drops and recreates every type and table while the
    services that own those databases are still holding pools, so they go on using cached
    plans and type OIDs for objects that no longer exist. Postgres answers them with
    "cached plan must not change result type" and "cache lookup failed for type ..." from
    then on, and they do not recover on their own. A restore that put every row back and
    left the orchestrator dead is the failure; naming the restarts is the fix.
    """
    applied = [
        {"component": name}
        for name in EVERY_COMPONENT
        if name.startswith("postgres/") or name == "volumes/hatchet-config"
    ]
    services = restore_runs.services_to_restart(applied)

    assert set(services) == {"hatchet", "litellm", "langfuse", "watcher", "app", "worker"}
    # Ordered so the appliance's own processes come back to a live orchestrator and gateway
    # rather than reconnecting to nothing.
    assert services.index("hatchet") < services.index("worker")
    assert services.index("app") < services.index("worker")

    # Restoring only the blob store asks nothing of any of them, and taking the estate down
    # anyway would turn a narrow recovery into an outage.
    assert restore_runs.services_to_restart([{"component": "files/artifact-blobs"}]) == []


@pytest.mark.integration
def test_the_backup_is_listed_as_complete_and_restorable_at_the_destination(
    round_trip: RoundTrip,
) -> None:
    """An operator during a recovery reads the share, not this test's fixtures.

    ``restore_plan`` is what they are shown before anything is applied, and a blocker there
    stops the restore — so a backup this suite has just proven restorable must not be
    reported as one they cannot use.
    """
    listed = backup_runs.list_backups(round_trip.config, session_factory=round_trip.factory)
    assert [item["backup_id"] for item in listed] == [round_trip.backup_id]
    assert listed[0]["complete"] is True
    assert listed[0]["warnings"] == []

    report = backup_restore.restore_plan(
        round_trip.config, round_trip.backup_id, round_trip.factory
    )
    assert report["blockers"] == [], report["blockers"]
    assert {step["name"] for step in report["steps"]} == set(EVERY_COMPONENT)
