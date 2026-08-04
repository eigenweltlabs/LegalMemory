"""Set-based retrieval verification and single-pass scope compilation.

These tests pin the two properties the retrieval hot path now relies on:

- ``_bulk_authorized_sources`` / ``_collapse_and_verify`` return exactly what the
  per-candidate path used to return — fail-closed for external sources without
  grants, deny wins, ``local_fs`` delegates to the project boundary, admins bypass
  ACLs but not lifecycle — at a **bounded** SQL cost (the N+1 regression guard);
- ``compile_scope`` narrows and fingerprints exactly as before while evaluating
  the visibility predicate only once.

Postgres only — no OpenSearch or model calls; candidates are built by hand the
way the fusion step would emit them.
"""

from __future__ import annotations

import gc
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Blob,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Project,
    ProjectGrant,
    Source,
    SourceObject,
)
from knowledge_index.permissions import (
    AccessService,
    configure_access,
    replace_source_object_grants,
)
from knowledge_index.retrieval import RetrievalService, _Candidate

MEMBER = {"user:member"}
WALLED = {"user:walled", "role:authenticated"}
ADMIN = {"role:admin"}


@pytest.fixture(autouse=True)
def _default_access_mode():
    """Pin the process-global mode: another test module may have changed it."""
    configure_access(source_acl_mode="sufficient", principal_aliases={})


@pytest.fixture
def counter(session: Session):
    """Count SQL statements the session's engine executes."""
    engine = session.get_bind()
    state = {"n": 0}

    def _count(*args, **kwargs):
        state["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    yield state
    event.remove(engine, "before_cursor_execute", _count)


def _seed_project(session: Session, key: str, *, member_principal: str | None) -> Project:
    project = Project(key=key, name=f"Project {key}")
    session.add(project)
    session.flush()
    if member_principal:
        session.add(
            ProjectGrant(
                project_id=project.id,
                principal=member_principal,
                principal_kind="user",
                effect="allow",
                role="viewer",
            )
        )
        session.flush()
    return project


def _seed_source(session: Session, project: Project, kind: str) -> Source:
    source = Source(
        project_id=project.id, kind=kind, display_name=f"{kind} source", provider="native"
    )
    session.add(source)
    session.flush()
    return source


_HASH_COUNTER = {"n": 0}


def _seed_version(
    session: Session,
    project: Project,
    source: Source,
    *,
    title: str,
    status: str = "final",
    ordinal: int = 1,
    document: Document | None = None,
    acl: list[dict] | None = None,
) -> tuple[Document, DocumentVersion]:
    _HASH_COUNTER["n"] += 1
    blob = Blob(content_hash=f"{_HASH_COUNTER['n']:064x}", size_bytes=8)
    if document is None:
        document = Document(project_id=project.id, title=title)
        session.add(document)
    session.add(blob)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, content_hash=blob.content_hash, ordinal=ordinal, status=status
    )
    source_object = SourceObject(
        source_id=source.id,
        external_id=f"obj-{_HASH_COUNTER['n']}",
        path=f"{title}-{ordinal}.txt",
        name=f"{title}-{ordinal}.txt",
        acl=acl,
    )
    session.add_all([version, source_object])
    session.flush()
    session.add(DocumentVersionSource(version_id=version.id, source_object_id=source_object.id))
    if acl is not None:
        replace_source_object_grants(session, source_object.id, acl)
    session.flush()
    return document, version


def _candidate(
    document_id: str, version_id: str, project_id: str | None, status: str, score: float
) -> _Candidate:
    """Build a fused candidate from plain ids, the way OpenSearch rows arrive.

    Deliberately NOT from ORM objects: production holds no references to the
    hit rows, and the statement-count assertions below must fail if batching
    ever depends on entities being kept alive by someone else (the session
    identity map is weak — see _warm_identity_map)."""
    return _Candidate(
        chunk_id=f"chunk-{version_id}-{score}",
        source={
            "document_id": document_id,
            "document_version_id": version_id,
            "project_id": project_id,
            "text": "Inhalt des Dokuments",
            "version_status": status,
            "meta": {},
            "identifiers": [],
        },
        fused_score=score,
    )


def _detach_all(session: Session) -> None:
    """Drop every strong ORM reference, as a fresh request would start out."""
    session.expunge_all()
    gc.collect()


def test_bulk_authorized_sources_keeps_single_version_semantics(session: Session) -> None:
    project = _seed_project(session, "P-BULK", member_principal="user:member")
    local = _seed_source(session, project, "local_fs")
    external = _seed_source(session, project, "sharepoint_online")

    _, v_local = _seed_version(session, project, local, title="Local")
    _, v_allowed = _seed_version(
        session,
        project,
        external,
        title="Shared",
        acl=[{"principal": "role:authenticated", "principal_kind": "role", "effect": "allow"}],
    )
    _, v_denied = _seed_version(
        session,
        project,
        external,
        title="Walled",
        acl=[
            {"principal": "role:authenticated", "principal_kind": "role", "effect": "allow"},
            {"principal": "user:walled", "principal_kind": "user", "effect": "deny"},
        ],
    )
    _, v_nogrant = _seed_version(session, project, external, title="Ungoverned")

    service = RetrievalService(session, AppConfig())
    all_ids = [v_local.id, v_allowed.id, v_denied.id, v_nogrant.id]

    member_view = service._bulk_authorized_sources(all_ids, MEMBER)
    # local_fs delegates to the project grant; ungoverned external fails closed.
    assert set(member_view) == {v_local.id}

    walled_view = service._bulk_authorized_sources(all_ids, WALLED)
    # role:authenticated is allowed on both external docs, but the explicit deny
    # on the walled document wins for this caller.
    assert set(walled_view) == {v_allowed.id}

    admin_view = service._bulk_authorized_sources(all_ids, ADMIN)
    # Admins bypass ACLs (not lifecycle): every version with a live observation.
    assert set(admin_view) == set(all_ids)

    # The single-version wrapper is the same decision, one id at a time.
    for version_id in all_ids:
        assert bool(service._authorized_sources(version_id, MEMBER)) == (
            version_id in member_view
        )
        assert bool(service._authorized_sources(version_id, WALLED)) == (
            version_id in walled_view
        )

    # Deleted observations disappear for everyone.
    session.execute(
        SourceObject.__table__.update().values(
            deleted_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    session.flush()
    assert service._bulk_authorized_sources(all_ids, ADMIN) == {}


def test_collapse_and_verify_filters_collapses_and_stays_off_the_n_plus_1_path(
    session: Session, counter
) -> None:
    project = _seed_project(session, "P-COLLAPSE", member_principal="user:member")
    local = _seed_source(session, project, "local_fs")
    external = _seed_source(session, project, "sharepoint_online")

    # One document with a draft and a final version at the same fused score: the
    # collapse must prefer the latest final, exactly like the per-candidate path.
    doc_tie, v_draft = _seed_version(
        session, project, local, title="Tie", status="draft", ordinal=1
    )
    _, v_final = _seed_version(
        session, project, local, title="Tie", status="final", ordinal=2, document=doc_tie
    )
    doc_tie.latest_final_version_id = v_final.id
    session.flush()

    # A document the caller must never see (external, no grants).
    doc_hidden, v_hidden = _seed_version(session, project, external, title="Hidden")

    # A stack of plainly visible documents.
    visible: list[tuple[Document, DocumentVersion]] = []
    for index in range(10):
        visible.append(_seed_version(session, project, local, title=f"Doc-{index}"))

    project_id = project.id
    tie = (doc_tie.id, v_draft.id, v_final.id)
    hidden = (doc_hidden.id, v_hidden.id)
    visible_ids = [(document.id, version.id) for document, version in visible]
    _detach_all(session)

    candidates = [
        _candidate(tie[0], tie[1], project_id, "draft", 5.0),
        _candidate(tie[0], tie[2], project_id, "final", 5.0),
        # Best provisional score, unauthorized.
        _candidate(hidden[0], hidden[1], project_id, "final", 9.0),
    ]
    for index, (document_id, version_id) in enumerate(visible_ids):
        candidates.append(
            _candidate(document_id, version_id, project_id, "final", 4.0 - index * 0.1)
        )

    service = RetrievalService(session, AppConfig())
    counter["n"] = 0
    hits = service._collapse_and_verify(
        candidates,
        principals=MEMBER,
        query_terms=set(),
        collapse=True,
        max_per_document=3,
        needed=50,
    )
    statements = counter["n"]

    returned_docs = [hit.document_id for hit in hits]
    assert hidden[0] not in returned_docs
    assert len(returned_docs) == len(set(returned_docs)) == 11  # collapsed per document
    tie_hit = next(hit for hit in hits if hit.document_id == tie[0])
    assert tie_hit.version_id == tie[2]  # equal score → latest final wins
    assert all(hit.citations and hit.source_paths for hit in hits)

    # 13 candidates / 12 documents used to cost ~3 statements per candidate;
    # bulk verification must stay a small constant per batch, not per candidate.
    assert statements <= 16, f"verification issued {statements} SQL statements"


def test_collapse_and_verify_stops_after_the_needed_prefix(
    session: Session, counter
) -> None:
    project = _seed_project(session, "P-LAZY", member_principal="user:member")
    local = _seed_source(session, project, "local_fs")
    project_id = project.id
    seeded_ids = [
        (document.id, version.id)
        for document, version in (
            _seed_version(session, project, local, title=f"Ranked-{index}")
            for index in range(30)
        )
    ]
    _detach_all(session)
    candidates = [
        _candidate(document_id, version_id, project_id, "final", float(30 - index))
        for index, (document_id, version_id) in enumerate(seeded_ids)
    ]

    service = RetrievalService(session, AppConfig())
    counter["n"] = 0
    hits = service._collapse_and_verify(
        candidates,
        principals=MEMBER,
        query_terms=set(),
        collapse=True,
        max_per_document=3,
        needed=5,
    )
    statements = counter["n"]

    # The top of the ranking is exact ...
    scores = [hit.score for hit in sorted(hits, key=lambda item: item.score, reverse=True)]
    assert scores[:5] == [30.0, 29.0, 28.0, 27.0, 26.0]
    # ... and only the first batch was verified (30 fully-verified documents
    # would take several batches and several times this statement count).
    assert statements <= 14, f"expected one lazy batch, saw {statements} statements"


def test_compile_scope_matches_the_composed_form_and_pins_the_fingerprint(
    session: Session,
) -> None:
    project_a = _seed_project(session, "P-A", member_principal="user:member")
    project_b = _seed_project(session, "P-B", member_principal=None)
    local_a = _seed_source(session, project_a, "local_fs")
    local_b = _seed_source(session, project_b, "local_fs")
    doc_a, _ = _seed_version(session, project_a, local_a, title="A-doc")
    doc_b, _ = _seed_version(session, project_b, local_b, title="B-doc")

    access = AccessService(session)

    # Equivalence with the composed (pre-rewrite) form for a plain caller.
    scope = access.compile_scope(MEMBER)
    assert set(scope.document_ids) == set(access.visible_document_ids(MEMBER)) == {doc_a.id}
    assert set(scope.project_ids) == set(access.visible_project_ids(MEMBER)) == {project_a.id}

    # Requested-project narrowing, both the authorized and the walled direction.
    narrowed = access.compile_scope(MEMBER, project_ids=[project_b.id])
    assert narrowed.project_ids == ()
    assert narrowed.document_ids == ()
    assert narrowed.opensearch_filter() == {"match_none": {}}

    admin_narrowed = access.compile_scope(ADMIN, project_ids=[project_b.id])
    assert admin_narrowed.project_ids == (project_b.id,)
    assert admin_narrowed.document_ids == (doc_b.id,)

    # Admin sees all projects, even empty ones, and every live document.
    admin_scope = access.compile_scope(ADMIN)
    assert set(admin_scope.project_ids) == {project_a.id, project_b.id}
    assert set(admin_scope.document_ids) == {doc_a.id, doc_b.id}

    # The fingerprint format is part of the audit contract — pin it byte for byte.
    raw = "\n".join(
        (
            *sorted(p.casefold() for p in MEMBER),
            "--projects",
            *sorted({project_a.id}),
            "--documents",
            *sorted({doc_a.id}),
        )
    )
    assert scope.fingerprint == sha256(raw.encode("utf-8")).hexdigest()[:20]
    # Deterministic: compiling twice yields the identical scope.
    assert access.compile_scope(MEMBER) == scope
