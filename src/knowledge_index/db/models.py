"""Persistence schema. Mirrors docs/src/content/docs/concepts/data-model.md — keep the two in sync.

Postgres is the target (pgvector for embeddings, JSONB, SKIP LOCKED work claims).
JSON columns use the generic type with a JSONB variant so unit tests can run
against SQLite; anything touching `chunks.embedding` requires Postgres.

Conventions:
- uuid4 primary keys, generated client-side
- `provenance` JSON = {"model": str, "prompt_version": str, "confidence": float,
  "evidence": [...]} on every model-inferred row; null on deterministic facts
- nothing is hard-deleted: tombstones (`deleted_at`) and supersession only
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    validates,
)

from knowledge_index.entity_names import normalize_entity_name

JSONVariant = JSON().with_variant(JSONB(), "postgresql")

# Kept next to the model it constrains and reused verbatim by the Alembic migration, so
# the two can never drift into a partial index the ORM believes covers something else.
_ACTIVE_SYNC_PREDICATE = "workflow = 'source-sync' AND status IN ('queued', 'running')"


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --------------------------------------------------------------------------- source layer


class Project(TimestampMixin, Base):
    """Local authorization boundary.

    A project is deliberately separate from a legal ``Matter``.  Deployments can map one
    matter to one project, several matters to a deal room, or non-matter knowledge to an
    administrative project without weakening the permission model.
    """

    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("key", name="uq_projects_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active")
    external_refs: Mapped[dict] = mapped_column(JSONVariant, default=dict)


class ProjectGrant(TimestampMixin, Base):
    """Allow/deny grant on a project for a canonical user or group principal."""

    __tablename__ = "project_grants"
    __table_args__ = (
        UniqueConstraint("project_id", "principal", "effect", "origin", name="uq_project_grant"),
        Index("ix_project_grants_lookup", "principal", "effect", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    principal: Mapped[str] = mapped_column(String(512))
    principal_kind: Mapped[str] = mapped_column(String(20), default="group")
    effect: Mapped[str] = mapped_column(String(10), default="allow")
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    origin: Mapped[str] = mapped_column(String(20), default="manual")
    external_id: Mapped[str | None] = mapped_column(String(512))


class SourceObjectGrant(TimestampMixin, Base):
    """Normalized snapshot of a source object's externally mirrored ACL."""

    __tablename__ = "source_object_grants"
    __table_args__ = (
        UniqueConstraint("source_object_id", "principal", "effect", name="uq_source_object_grant"),
        Index("ix_source_object_grants_lookup", "principal", "effect", "source_object_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_object_id: Mapped[str] = mapped_column(ForeignKey("source_objects.id"))
    principal: Mapped[str] = mapped_column(String(512))
    principal_kind: Mapped[str] = mapped_column(String(20), default="group")
    effect: Mapped[str] = mapped_column(String(10), default="allow")
    origin: Mapped[str] = mapped_column(String(30), default="connector")
    external_id: Mapped[str | None] = mapped_column(String(512))


class Source(TimestampMixin, Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"))
    kind: Mapped[str] = mapped_column(String(50))  # sharepoint | imanage | smb | local_fs | ...
    display_name: Mapped[str] = mapped_column(String(255))
    config: Mapped[dict] = mapped_column(JSONVariant, default=dict)  # non-secret connector config
    cursor: Mapped[str | None] = mapped_column(Text)  # opaque incremental-sync state
    sync_policy: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | paused | error
    provider: Mapped[str] = mapped_column(String(30), default="native")
    provider_connection_id: Mapped[str | None] = mapped_column(String(255))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Tracked separately from last_sync_at because a permission change alters no
    # document's etag: only a full scan re-reads ACLs, so a revocation at source is not
    # mirrored until one runs. The engine forces one when this goes stale.
    last_full_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Fingerprint of the folder selection this source was last synced under. Narrowing a
    # selection makes the next scan see far fewer objects, which is indistinguishable from
    # a connector failing to enumerate — so the engine compares this and treats a genuine
    # change as an authorised re-scope rather than a suspicious mass deletion.
    selection_fingerprint: Mapped[str | None] = mapped_column(String(64))
    last_sync_summary: Mapped[dict] = mapped_column(JSONVariant, default=dict)


class ConnectorEventSubscription(TimestampMixin, Base):
    """One upstream event subscription that wakes one source's normal delta sync.

    ``target`` is the provider resource (a Drive folder/shared drive or a Graph drive
    root). A source can need several targets, so state cannot live as one blob on
    ``Source``. Provider payloads are never trusted as document truth; ``external_id``
    only maps the wake-up back to the source.
    """

    __tablename__ = "connector_event_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "adapter", "target", name="uq_connector_event_subscription_target"
        ),
        Index("ix_connector_event_subscriptions_external", "adapter", "external_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    adapter: Mapped[str] = mapped_column(String(80))
    transport: Mapped[str] = mapped_column(String(40))
    target: Mapped[str] = mapped_column(String(1024))
    external_id: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONVariant, default=dict)


class ConnectorEventCheckpoint(TimestampMixin, Base):
    """Durable cursor for event transports whose broker has no server-side ack.

    Pub/Sub owns acknowledgements itself. Azure Event Hubs does not, so the manager
    stores one offset per consumer-group partition and resumes after it on restart.
    """

    __tablename__ = "connector_event_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "transport", "partition", name="uq_connector_event_checkpoint_partition"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    transport: Mapped[str] = mapped_column(String(80))
    partition: Mapped[str] = mapped_column(String(120))
    position: Mapped[str] = mapped_column(String(255))
    sequence_number: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[dict] = mapped_column(JSONVariant, default=dict)


class SourceDeletionWatch(TimestampMixin, Base):
    """A deletion too large to trust from one scan, held while later scans agree with it.

    One snapshot cannot tell "the firm deleted this matter" from "the connector could not
    enumerate it", so a scan that would remove more than the source's tombstone fraction
    records what it believes is gone instead of acting on it. The objects stay indexed and
    searchable meanwhile; ``confirmations`` counts the consecutive scans that reported the
    *same* set, and the tombstones are applied once it reaches ``required``.
    """

    __tablename__ = "source_deletion_watches"
    __table_args__ = (UniqueConstraint("source_id", name="uq_source_deletion_watch_source"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    confirmations: Mapped[int] = mapped_column(Integer, default=1)
    required: Mapped[int] = mapped_column(Integer, default=3)
    # Size of the held set and of the live index at the last scan, kept here so the admin
    # UI can say "340 of 600 documents" without re-counting a firm's estate per poll.
    object_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SourceDeletionCandidate(Base):
    """One external id a held deletion covers.

    Rows rather than a JSON array on the watch: the identity that has to match across
    scans is the *set* of external ids (340 missing today and a different 340 tomorrow is
    a connector returning garbage, not a deletion), and a firm's estate can put six digits
    of ids in that set.
    """

    __tablename__ = "source_deletion_candidates"
    __table_args__ = (Index("ix_source_deletion_candidates_watch", "watch_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    watch_id: Mapped[str] = mapped_column(
        ForeignKey("source_deletion_watches.id", ondelete="CASCADE")
    )
    external_id: Mapped[str] = mapped_column(String(1024))


class SourceObject(TimestampMixin, Base):
    """One file/email/container as observed in one source. Unit of sync + pipeline."""

    __tablename__ = "source_objects"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_source_external"),
        Index("ix_source_objects_hash", "content_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(1024))  # source system's stable id
    path: Mapped[str] = mapped_column(Text)  # verbatim, umlauts and garbage included
    name: Mapped[str] = mapped_column(Text)
    container: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_hash: Mapped[str | None] = mapped_column(String(64))  # sha256, set by fetch stage
    source_version_label: Mapped[str | None] = mapped_column(String(100))
    mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    author_hint: Mapped[str | None] = mapped_column(String(255))
    acl: Mapped[list | None] = mapped_column(JSONVariant)  # [AccessGrant]; null = source has none
    # Set when the connector staged content during the scan; the fetch stage reads it.
    staged_path: Mapped[str | None] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # tombstone

    source: Mapped[Source] = relationship()


class Blob(Base):
    """Unique content. The same bytes in five places = one blob, five source objects."""

    __tablename__ = "blobs"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_sniffed: Mapped[str | None] = mapped_column(String(255))
    cached_path: Mapped[str | None] = mapped_column(Text)  # original cache, retention-policy bound
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Artifact(Base):
    """Immutable derived output keyed by (content_hash, producer, producer_version)."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint(
            "content_hash", "producer", "producer_version", "kind", name="uq_artifact_producer"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    content_hash: Mapped[str] = mapped_column(ForeignKey("blobs.content_hash"))
    producer: Mapped[str] = mapped_column(String(100))  # e.g. "docling", "ocr", "chunker"
    producer_version: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(50))  # structured_json | text | page_images | ...
    payload: Mapped[dict | None] = mapped_column(JSONVariant)  # small artifacts inline
    path: Mapped[str | None] = mapped_column(Text)  # large artifacts on the artifact FS
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ------------------------------------------------------------------------ knowledge layer


def _normalized_name_default(context) -> str:
    """``normalized_name`` derived from whatever ``name`` this statement writes.

    Set as a column default rather than left to the caller so no insert path can
    produce a row the uniqueness constraint and the resolver disagree about — the
    constraint is only worth its name if every row is keyed the same way.
    """
    parameters = context.get_current_parameters()
    return normalize_entity_name(parameters.get("name"))


class EntityIdentityMixin:
    """The columns that make one real-world entity one row.

    ``normalized_name`` is the identity key (see knowledge_index.entity_names) and
    carries a trigram index, so the insertion-time resolver can ask "who does the
    estate already know by roughly this name" instead of the substring test it used
    to run — which found "Verimark Hospitality Group Inc." from "Verimark
    Hospitality Group Inc" and from nothing else.

    ``identity_discriminator`` is empty for every entity the estate has no reason to
    split. Two genuinely different companies DO share a name, and the corpus plants
    exactly that case; when a mention carries a register identifier that contradicts
    the same-named incumbent's, the new row records that identifier here and the
    unique constraint admits it as a second, deliberately distinct entity. Anything
    else that shares a normalized name is the same entity. A firm's clients recur:
    a modest client list spread over many matters is what an estate looks like, and
    a resolver that splits on spelling produces the inverse — nearly as many clients
    as there are matters, almost every one of them touching exactly one.

    ``normalized_aliases`` is every OTHER name form seen for this entity, normalized,
    so a variant the corpus has already taught the resolver resolves exactly rather
    than fuzzily on the next document that uses it.
    """

    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text, default=_normalized_name_default)
    identity_discriminator: Mapped[str] = mapped_column(Text, default="", server_default="")
    aliases: Mapped[list] = mapped_column(JSONVariant, default=list)
    normalized_aliases: Mapped[list] = mapped_column(JSONVariant, default=list)
    kind: Mapped[str] = mapped_column(String(20), default="legal_entity")
    identifiers: Mapped[dict] = mapped_column(JSONVariant, default=dict)  # HRB, USt-Id, DMS code
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)

    @validates("name")
    def _keep_normalized_name(self, _key: str, value: str) -> str:
        """Renaming an entity re-keys it, in the same statement.

        The column default covers Core inserts; this covers every ORM write,
        including the ones that only touch ``name``. A row whose normalized name no
        longer matches its name is invisible to the resolver that is supposed to
        find it, which is the failure this whole change exists to end.
        """
        self.normalized_name = normalize_entity_name(value)
        return value


def _entity_identity_args(table: str) -> tuple:
    """Uniqueness and the two lookup indexes, identical for clients and parties."""
    return (
        UniqueConstraint(
            "normalized_name", "identity_discriminator", name=f"uq_{table}_identity"
        ),
        Index(
            f"ix_{table}_normalized_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
        Index(
            f"ix_{table}_normalized_aliases",
            "normalized_aliases",
            postgresql_using="gin",
            postgresql_ops={"normalized_aliases": "jsonb_path_ops"},
        ),
    )


class Client(EntityIdentityMixin, TimestampMixin, Base):
    __tablename__ = "clients"
    __table_args__ = _entity_identity_args("clients")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)


class Party(EntityIdentityMixin, TimestampMixin, Base):
    """Any natural/legal person appearing in matters or documents (incl. non-clients)."""

    __tablename__ = "parties"
    __table_args__ = _entity_identity_args("parties")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)


class Matter(TimestampMixin, Base):
    __tablename__ = "matters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"))
    reference_numbers: Mapped[list] = mapped_column(JSONVariant, default=list)  # all refs seen
    title: Mapped[str] = mapped_column(Text)
    practice_area: Mapped[str | None] = mapped_column(String(50))
    matter_kind: Mapped[str | None] = mapped_column(String(50))  # ontology node id
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    # DEFERRED by design (spec O6): filled by practice-management import (or a future
    # email-author aggregation), never model-guessed from document content — naming
    # the wrong lawyer as responsible is worse than an empty field.
    responsible: Mapped[list] = mapped_column(JSONVariant, default=list)
    time_range: Mapped[dict | None] = mapped_column(JSONVariant)  # {"from": iso, "to": iso}
    imported: Mapped[bool] = mapped_column(
        Boolean, default=False
    )  # from practice-mgmt = authoritative
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)
    # Where the matter stands: executed | closed | terminated | dormant | in_progress.
    # Distinct from `status` above, which is the triage flag for a fallback holding
    # matter. A caller asking "which financings actually closed" needs this, and
    # without it must read every document of every candidate — which agents did
    # inconsistently, including terminated matters in answers about live deals.
    lifecycle: Mapped[str | None] = mapped_column(String(20))
    # The firm's OWN practice book for this matter ("Banking & Finance", "Funds &
    # Asset Management"), distinct from practice_area, which is the area of LAW: a
    # matter whose subject is corporate entity formation can be filed in the funds
    # group, and only the firm's paperwork shows that. Denormalised from the
    # responsible partner's group in `matter_team` — a matter belongs to the group
    # of the partner who owns it — and cached here so scoping a practice is one
    # indexed predicate rather than a join.
    practice_group: Mapped[str | None] = mapped_column(String(120), index=True)
    # The rest of the matter-level profile: what the deal IS, which document
    # constitutes it, a one-line summary, and the evidence behind each. JSON rather
    # than columns because the useful fields are still being learned and a rubric
    # should not mint a migration; promote a field only once something filters on it.
    profile: Mapped[dict | None] = mapped_column(JSONVariant)


class FirmPerson(TimestampMixin, Base):
    """A lawyer or professional AT the firm, as distinct from a party to a matter.

    Kept apart from ``Party`` deliberately. A party is someone the firm deals with;
    a firm person is someone the firm staffs with. They answer different questions
    — "which matters involve Nexford" versus "which matters does Merritt run" — and
    conflating them would let a party filter match the firm's own lawyers and put
    counsel on the other side of their own deal.

    The practice group lives HERE rather than on the matter because that is where
    the firm puts it: the paperwork says "Priya Anand, Responsible Partner,
    Litigation Department". A matter's group is the group of the partner who owns
    it, which is why the same person's matters move together when they move.
    """

    __tablename__ = "firm_people"
    __table_args__ = (
        UniqueConstraint("normalized_name", name="uq_firm_people_normalized_name"),
        Index("ix_firm_people_practice_group", "practice_group"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text)
    # Seniority as the firm writes it: Partner, Of Counsel, Associate, Paralegal.
    title: Mapped[str | None] = mapped_column(String(120))
    practice_group: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255))
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)

    @validates("name")
    def _normalize(self, _key: str, value: str) -> str:
        self.normalized_name = normalize_entity_name(value or "")
        return value


class FirmPracticeGroup(TimestampMixin, Base):
    """One of the firm's own practice groups, with every spelling it goes by.

    A group is an entity, not a label, for the same reason a party is: the firm
    writes it differently in every document that mentions it, and free strings
    make each spelling its own group. Before this table an estate held "Capital
    Markets", "Capital Markets & Structured Finance", "Capital Markets &
    Regulatory" and "Structured Finance" as four separate books, so a caller
    asking what the capital markets group has done got a quarter of it and no
    indication the rest existed.

    ``aliases`` is what makes resolution stick. Case, ampersands and the trailing
    "practice group"/"department" fold deterministically into
    ``normalized_name``; anything past that — whether "Private Funds" names the
    Funds & Asset Management group or a real second book — is a judgement a model
    makes once, against the groups already recorded, and the answer is written
    here so it is never re-litigated.
    """

    __tablename__ = "firm_practice_groups"
    __table_args__ = (
        UniqueConstraint(
            "normalized_name", name="uq_firm_practice_groups_normalized_name"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The spelling the firm is taken to use, and what tool results show.
    name: Mapped[str] = mapped_column(String(120))
    normalized_name: Mapped[str] = mapped_column(Text)
    # Other spellings resolved onto this group, normalized, including the ones a
    # model judged equivalent. Looked up before any model is asked again.
    aliases: Mapped[list | None] = mapped_column(JSONVariant, default=list)
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)


class MatterTeam(TimestampMixin, Base):
    """Which firm people work a matter, and in what role.

    Role is the role ON THIS MATTER — responsible partner, billing partner, lead
    associate — not the person's title, because the same partner is responsible on
    one matter and merely supervising on another. A person can hold more than one
    role on a matter, so the role is part of the key.
    """

    __tablename__ = "matter_team"
    __table_args__ = (
        Index("ix_matter_team_person", "person_id", "role"),
    )

    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("firm_people.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(60), primary_key=True)
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)


class MatterProfileQueue(Base):
    """Matters whose profile is stale, and when they were last touched.

    A matter's practice area, kind, lifecycle and version chains are properties of
    the whole folder, but insertion is per-document and parallel, so no document
    can decide them. They are instead re-derived by a matter-level pass, and this
    table is how that pass knows what to look at.

    Every classified document upserts its matter here — one row write inside a
    transaction that is committing anyway, next to a document that just cost a
    model call. The work therefore scales with MATTERS TOUCHED, not documents: a
    million documents across thirty thousand matters run thirty thousand profiles,
    and one document added later runs one.

    ``last_marked_at`` is what makes bulk and incremental the same code path. A
    worker takes matters untouched for longer than the debounce window, so a matter
    receiving five hundred documents stays out of the queue until its flood stops
    and then profiles once, while a lone document profiles as soon as the window
    passes. Nothing has to know which mode it is in.
    """

    __tablename__ = "matter_profile_queue"
    __table_args__ = (Index("ix_matter_profile_queue_marked", "last_marked_at"),)

    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), primary_key=True)
    last_marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    profiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Non-fatal: a matter whose profile call failed keeps its old profile and is
    # retried, rather than blocking the run or silently losing its place.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class MatterClient(Base):
    __tablename__ = "matter_clients"

    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), primary_key=True)


class MatterParty(Base):
    __tablename__ = "matter_parties"

    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), primary_key=True)
    party_id: Mapped[str] = mapped_column(ForeignKey("parties.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(30), primary_key=True)  # taxonomies.PartyRole
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)


class Document(TimestampMixin, Base):
    """Logical work product; concrete states live in DocumentVersion."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"))
    matter_id: Mapped[str | None] = mapped_column(ForeignKey("matters.id"))
    doc_type: Mapped[str | None] = mapped_column(String(60))  # ontology node id
    # Visible ancestor node ids incl. the node itself — what subtree filters
    # match against ("all Agreements" hits every descendant type).
    doc_type_ancestors: Mapped[list] = mapped_column(JSONVariant, default=list)
    # Fingerprint of the ontology scope this document was typed under.
    ontology_fingerprint: Mapped[str | None] = mapped_column(String(16))
    title: Mapped[str | None] = mapped_column(Text)  # normalized, not the filename
    language: Mapped[str | None] = mapped_column(String(10))
    doc_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # date OF the doc
    parties: Mapped[list] = mapped_column(JSONVariant, default=list)  # [{party_id, role_in_doc}]
    identifiers: Mapped[list] = mapped_column(JSONVariant, default=list)  # verbatim legal ids
    latest_final_version_id: Mapped[str | None] = mapped_column(String(36))
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)


class DocumentGrant(TimestampMixin, Base):
    """Manual or connector-derived exception at document scope.

    A document grant can narrow project access.  It never bypasses a known source-system
    denial; the scope compiler intersects local and mirrored permissions.
    """

    __tablename__ = "document_grants"
    __table_args__ = (
        UniqueConstraint("document_id", "principal", "effect", "origin", name="uq_document_grant"),
        Index("ix_document_grants_lookup", "principal", "effect", "document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    principal: Mapped[str] = mapped_column(String(512))
    principal_kind: Mapped[str] = mapped_column(String(20), default="group")
    effect: Mapped[str] = mapped_column(String(10), default="allow")
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    origin: Mapped[str] = mapped_column(String(30), default="manual")
    external_id: Mapped[str | None] = mapped_column(String(512))


class DocumentVersion(TimestampMixin, Base):
    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    content_hash: Mapped[str] = mapped_column(ForeignKey("blobs.content_hash"))
    ordinal: Mapped[int | None] = mapped_column(Integer)  # 1 = earliest known
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # taxonomies.VersionStatus
    status_evidence: Mapped[dict | None] = mapped_column(JSONVariant)
    redline_against: Mapped[str | None] = mapped_column(String(36))  # version id it marks up
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)

    document: Mapped[Document] = relationship()


class DocumentVersionSource(Base):
    """All the places one exact version was observed."""

    __tablename__ = "document_version_sources"

    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), primary_key=True)
    source_object_id: Mapped[str] = mapped_column(ForeignKey("source_objects.id"), primary_key=True)


class Relation(Base):
    """Typed edges between knowledge entities (docs/src/content/docs/concepts/data-model.md §Relation).

    Deliberately a plain table, not a graph engine — see architecture doc §5.
    entity refs are (type, id) pairs: type ∈ document | document_version | thread | eval_record.
    """

    __tablename__ = "relations"
    __table_args__ = (
        Index("ix_relations_from", "from_type", "from_id"),
        Index("ix_relations_to", "to_type", "to_id"),
        UniqueConstraint("from_type", "from_id", "to_type", "to_id", "kind", name="uq_relation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    from_type: Mapped[str] = mapped_column(String(30))
    from_id: Mapped[str] = mapped_column(String(36))
    to_type: Mapped[str] = mapped_column(String(30))
    to_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(30))  # taxonomies.RelationKind
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RelationIntent(TimestampMixin, Base):
    """A relate decision whose target file could not yet be materialized.

    The relate agent may link to a neighbour that is readable but not yet assigned
    to a matter; its document entity cannot be created without a permission scope
    (matter -> project).  Instead of dropping the model's decision, it is parked
    here keyed by file ids and replayed by classify_matter the moment the target's
    assignment lands.  ``intent``: relation | thread_member | redline.
    """

    __tablename__ = "relation_intents"
    __table_args__ = (
        Index("ix_relation_intents_target", "target_source_object_id", "status"),
        UniqueConstraint(
            "source_object_id",
            "target_source_object_id",
            "intent",
            "relation_kind",
            name="uq_relation_intent",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_object_id: Mapped[str] = mapped_column(ForeignKey("source_objects.id"))
    target_source_object_id: Mapped[str] = mapped_column(ForeignKey("source_objects.id"))
    intent: Mapped[str] = mapped_column(String(20))
    relation_kind: Mapped[str] = mapped_column(String(30), default="")  # taxonomies.RelationKind
    payload: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | applied
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommunicationThread(TimestampMixin, Base):
    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    matter_id: Mapped[str | None] = mapped_column(ForeignKey("matters.id"))
    subject_norm: Mapped[str | None] = mapped_column(Text)
    participants: Mapped[list] = mapped_column(JSONVariant, default=list)
    time_range: Mapped[dict | None] = mapped_column(JSONVariant)
    # every Message-ID observed in (or referenced by) the thread's emails — the
    # RFC 5322 linkage that joins members deterministically when headers exist
    message_ids: Mapped[list] = mapped_column(JSONVariant, default=list)


class DecisionRecord(TimestampMixin, Base):
    """Anonymized negotiation/drafting rationale. The 'why' knowledge layer."""

    __tablename__ = "decision_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    matter_id: Mapped[str | None] = mapped_column(ForeignKey("matters.id"))  # internal only
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"))
    version_from: Mapped[str | None] = mapped_column(String(36))
    version_to: Mapped[str | None] = mapped_column(String(36))
    locus: Mapped[str | None] = mapped_column(Text)  # "§ 9 Abs. 2 Haftungsbegrenzung"
    change_summary: Mapped[str | None] = mapped_column(Text)
    rationale_category: Mapped[str] = mapped_column(String(40))  # taxonomies.RationaleCategory
    rationale_text: Mapped[str] = mapped_column(Text)  # ANONYMIZED prose
    generalizable: Mapped[bool] = mapped_column(Boolean, default=True)
    source_evidence: Mapped[list] = mapped_column(JSONVariant, default=list)  # ACL-protected refs
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)


class EvalRecord(TimestampMixin, Base):
    """An RL-environment record: {Legal Task, Input files, Criteria} (BigLaw-Bench style).

    Auto-generated records start as ``status='proposed'`` candidates and only become live
    firm environments after a partner approves them — raw auto-gen never masquerades as a
    benchmark. Only firm work product (``authored_internally``) is eligible; external and
    opposing-counsel documents are excluded, because the point is to capture THIS firm's
    knowledge and taste, not generic tasks."""

    __tablename__ = "eval_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    matter_id: Mapped[str | None] = mapped_column(ForeignKey("matters.id"))
    task_type: Mapped[str] = mapped_column(String(50))  # taxonomies.TaskType
    instruction: Mapped[str] = mapped_column(Text)  # reconstructed task statement
    input_refs: Mapped[list] = mapped_column(JSONVariant, default=list)  # version ids at task start
    reference_output_ref: Mapped[str | None] = mapped_column(
        String(36)
    )  # gold: human final version
    rubric: Mapped[list] = mapped_column(JSONVariant, default=list)
    # rubric item: {"id","criterion","description","weight","kind":"binary"|"scale_1_5"
    #               |"negative","essential":bool}
    verifiers: Mapped[list] = mapped_column(JSONVariant, default=list)
    # objective RLVR checks: {"check","detail"} (e.g. a required section reference resolves)
    holdout: Mapped[bool] = mapped_column(Boolean, default=True)  # excluded from retrieval index
    # curation state — a candidate is only a live environment once a partner approves it
    status: Mapped[str] = mapped_column(String(20), default="proposed")  # EnvironmentStatus
    authored_internally: Mapped[bool] = mapped_column(Boolean, default=False)
    practice_area: Mapped[str | None] = mapped_column(String(50))
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)


# -------------------------------------------------------------- structured legal knowledge
# Non-document data kept distinct from document metadata: billing (LEDES/UTBMS-aligned) and
# typed entity identifiers (SALI/register schemes). Ingested and deduplicated, not chunked.


class Timekeeper(TimestampMixin, Base):
    """A billable person (LEDES TIMEKEEPER). Dedup on (law_firm_id, ledes_timekeeper_id)."""

    __tablename__ = "timekeepers"
    __table_args__ = (UniqueConstraint("law_firm_id", "ledes_timekeeper_id", name="uq_timekeeper"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    law_firm_id: Mapped[str] = mapped_column(String(100), default="self")
    ledes_timekeeper_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str | None] = mapped_column(String(255))
    classification: Mapped[str | None] = mapped_column(String(100))  # partner/associate/paralegal
    default_rate: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)


class BillingInvoice(TimestampMixin, Base):
    """A law-firm invoice (LEDES 1998B/XML). Dedup on (law_firm_id, invoice_number)."""

    __tablename__ = "billing_invoices"
    __table_args__ = (
        UniqueConstraint("law_firm_id", "invoice_number", name="uq_billing_invoice"),
        Index("ix_billing_invoice_matter", "matter_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    law_firm_id: Mapped[str] = mapped_column(String(100), default="self")
    invoice_number: Mapped[str] = mapped_column(String(100))
    invoice_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"))
    matter_id: Mapped[str | None] = mapped_column(ForeignKey("matters.id"))
    client_matter_id: Mapped[str | None] = mapped_column(String(100))
    law_firm_matter_id: Mapped[str | None] = mapped_column(String(100))
    invoice_total: Mapped[float | None] = mapped_column(Float)
    tax_total: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    ledes_format: Mapped[str | None] = mapped_column(String(20))  # 1998B/1998BI/XML2.0/2.1
    source_object_id: Mapped[str | None] = mapped_column(String(36))
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)


class BillingLineItem(TimestampMixin, Base):
    """One LEDES line item. Dedup on (invoice_id, line_item_number)."""

    __tablename__ = "billing_line_items"
    __table_args__ = (
        UniqueConstraint("invoice_id", "line_item_number", name="uq_billing_line_item"),
        Index("ix_billing_line_item_codes", "task_code", "activity_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("billing_invoices.id"))
    line_item_number: Mapped[int] = mapped_column(Integer)
    line_type: Mapped[str | None] = mapped_column(String(4))  # F/E/IF/IE (fee/expense)
    line_item_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timekeeper_id: Mapped[str | None] = mapped_column(ForeignKey("timekeepers.id"))
    task_code: Mapped[str | None] = mapped_column(String(20))  # UTBMS task (e.g. L110)
    activity_code: Mapped[str | None] = mapped_column(String(20))  # UTBMS activity (A101)
    expense_code: Mapped[str | None] = mapped_column(String(20))  # UTBMS expense (E101)
    number_of_units: Mapped[float | None] = mapped_column(Float)  # hours / quantity
    unit_cost: Mapped[float | None] = mapped_column(Float)  # rate
    line_item_total: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)


class EntityIdentifier(TimestampMixin, Base):
    """A typed identifier for a client or party, promoted from free-form JSON.

    scheme in {lei, de_hrb, vat_ustid, sali_legal_entity, dms_code, court_register}. A shared
    identifier is the hard signal for entity resolution (never a byte hash or name equality)."""

    __tablename__ = "entity_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "entity_type", "entity_id", "scheme", "value", name="uq_entity_identifier"
        ),
        Index("ix_entity_identifier_lookup", "scheme", "value"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entity_type: Mapped[str] = mapped_column(String(20))  # client | party
    entity_id: Mapped[str] = mapped_column(String(36))
    scheme: Mapped[str] = mapped_column(String(40))
    value: Mapped[str] = mapped_column(String(255))
    provenance: Mapped[dict | None] = mapped_column(JSONVariant)


# ------------------------------------------------------------------------- pipeline layer


class ProcessingState(Base):
    """One row per (source_object, stage). The resumability backbone."""

    __tablename__ = "processing_state"
    __table_args__ = (
        UniqueConstraint("source_object_id", "stage", name="uq_processing_object_stage"),
        Index("ix_processing_claim", "stage", "status", "next_retry_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_object_id: Mapped[str] = mapped_column(ForeignKey("source_objects.id"))
    stage: Mapped[str] = mapped_column(String(30))  # taxonomies.PipelineStage
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # taxonomies.ProcessingStatus
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # stale-claim timeout
    last_error: Mapped[dict | None] = mapped_column(JSONVariant)  # {"class", "message", "trace"}
    producer_version: Mapped[str | None] = mapped_column(String(50))  # bump => re-queue
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MatterAssignment(Base):
    """Per-observation classification: path and ACL context are not content-addressed."""

    __tablename__ = "matter_assignments"

    source_object_id: Mapped[str] = mapped_column(ForeignKey("source_objects.id"), primary_key=True)
    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"))
    confidence: Mapped[float] = mapped_column(Float)
    evidence: Mapped[list] = mapped_column(JSONVariant, default=list)
    producer_version: Mapped[str] = mapped_column(String(50))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Extraction(Base):
    """Audit record for every model call that wrote knowledge-layer data."""

    __tablename__ = "extractions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    target_entity: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[str] = mapped_column(String(36))
    fields: Mapped[list] = mapped_column(JSONVariant, default=list)
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(50))
    input_artifact_refs: Mapped[list] = mapped_column(JSONVariant, default=list)
    raw_output_ref: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    """Append-only record of API and MCP access to the shadow index."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_created", "created_at"),
        Index("ix_audit_events_action", "action", "outcome"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    actor_principals: Mapped[list] = mapped_column(JSONVariant, default=list)
    action: Mapped[str] = mapped_column(String(120))
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str] = mapped_column(String(20))  # success | denied | error
    details: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageEvent(Base):
    """Normalized cost and token event from LiteLLM or a local model runtime."""

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_created", "created_at"),
        Index("ix_usage_events_project", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"))
    pipeline_stage: Mapped[str | None] = mapped_column(String(50))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(150))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    trace_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PipelineRun(TimestampMixin, Base):
    """Local mirror of an orchestrator workflow run for the unified progress UI."""

    __tablename__ = "pipeline_runs"
    __table_args__ = (
        Index("ix_pipeline_runs_status", "status", "created_at"),
        # At most one unfinished sync per source, enforced by the database rather than by
        # a disabled button. Two concurrent scans of one estate interleave tombstones
        # with observations of the same objects; the second one deciding what the first
        # has not written yet is how a source loses documents it still has. A partial
        # index rather than a constraint because finished runs are history and must be
        # allowed to accumulate.
        Index(
            "uq_pipeline_runs_active_sync",
            "source_id",
            unique=True,
            postgresql_where=text(_ACTIVE_SYNC_PREDICATE),
            sqlite_where=text(_ACTIVE_SYNC_PREDICATE),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"))
    provider: Mapped[str] = mapped_column(String(30), default="local")
    provider_run_id: Mapped[str | None] = mapped_column(String(255))
    workflow: Mapped[str] = mapped_column(String(100), default="insertion")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    current_step: Mapped[str | None] = mapped_column(String(60))
    counters: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[dict | None] = mapped_column(JSONVariant)


class ExternalClient(TimestampMixin, Base):
    """MCP/API client registration. Secrets are represented by external vault references."""

    __tablename__ = "external_clients"
    __table_args__ = (UniqueConstraint("name", name="uq_external_clients_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(20), default="mcp")
    status: Mapped[str] = mapped_column(String(20), default="active")
    principal: Mapped[str] = mapped_column(String(512))
    secret_ref: Mapped[str | None] = mapped_column(Text)
    allowed_project_ids: Mapped[list] = mapped_column(JSONVariant, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------- index layer


class Chunk(Base):
    """Retrieval unit. Denormalized filter columns so metadata-first search is one query.

    embedding is dimension-less at DDL time (model-configurable); at production scale
    set a fixed dim + HNSW index during deployment provisioning.
    """

    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_version", "document_version_id"),
        Index("ix_chunks_matter", "matter_id"),
        # One chunk per position per version — two index executions racing on a
        # merged version (each source file carries its own index task) must not
        # be able to store the corpus twice (2026-08-01 audit: 96 dup pairs).
        UniqueConstraint("document_version_id", "ordinal", name="uq_chunks_version_ordinal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONVariant, default=dict)  # section path, page, locus
    # denormalized filters (kept in sync by the index stage)
    project_id: Mapped[str | None] = mapped_column(String(36))
    document_id: Mapped[str | None] = mapped_column(String(36))
    matter_id: Mapped[str | None] = mapped_column(String(36))
    doc_type: Mapped[str | None] = mapped_column(String(60))  # ontology node id
    doc_type_ancestors: Mapped[list] = mapped_column(JSONVariant, default=list)
    version_status: Mapped[str | None] = mapped_column(String(20))
    language: Mapped[str | None] = mapped_column(String(10))
    doc_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # denormalized from Document.identifiers; feeds the OpenSearch
    # identifiers/identifiers_text fields (exact + fuzzy identifier legs). A real
    # column so it survives a resync built from DB rows — previously this was a
    # transient Python attribute the index stage set that only reached OpenSearch
    # via getattr and was never persisted.
    identifiers: Mapped[list] = mapped_column(JSONVariant, default=list)
    # denormalized from Document.parties; feeds the OpenSearch `parties` keyword
    # field (F4 party filter). Holds both each party's resolved party_id and its
    # canonical name, so a caller may filter by either. A real column for the same
    # resync-survival reason as identifiers above.
    parties: Mapped[list] = mapped_column(JSONVariant, default=list)
    allowed_principals: Mapped[list] = mapped_column(JSONVariant, default=list)
    denied_principals: Mapped[list] = mapped_column(JSONVariant, default=list)
    access_version: Mapped[int] = mapped_column(Integer, default=1)
    embedding: Mapped[list | None] = mapped_column(Vector())
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceCredential(TimestampMixin, Base):
    """Encrypted credentials for one configured source.

    Kept out of `Source.config` on purpose: that column is documented as non-secret and
    is surfaced in the admin UI and in support exports. An OAuth refresh token is a
    long-lived key to a firm's entire document estate, so it lives here as AES-256-GCM
    ciphertext (see connectors/runtime/secrets.py) and is decrypted only in the worker
    that is about to make an API call.
    """

    __tablename__ = "source_credentials"

    # Cascading is what the schema has always done; declaring it here too keeps
    # autogenerate from proposing to drop it on every unrelated migration.
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    payload: Mapped[str] = mapped_column(Text)  # base64(nonce || ciphertext)
    # Which key encrypted this row, so a key rotation can tell re-encryptable rows from
    # rows that are now unreadable instead of failing with a bare authentication error.
    key_fingerprint: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str | None] = mapped_column(String(60))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PendingSourceAuthorization(Base):
    """An OAuth handshake that has been started but has not come back yet.

    A connection the provider has not authorized is not a connection. Holding the
    operator's intent here instead of in ``sources`` is what makes an abandoned
    handshake leave nothing behind: close the browser at the consent screen and there is
    no dead row in the connections list, no orphaned credential, and nothing that claims
    to be a source while syncing zero documents forever. The row is deleted the instant
    the callback succeeds — or fails — and lapses on its own if nobody ever returns.

    ``state`` is the only field in the clear, because it is the random value the provider
    echoes back and it has to be matchable. The firm's client id and secret and the PKCE
    verifier live in ``payload`` under the same AES-256-GCM key as ``source_credentials``:
    a client secret is exactly as sensitive before a Source exists as after.
    """

    __tablename__ = "pending_source_authorizations"
    __table_args__ = (
        UniqueConstraint("state", name="uq_pending_source_auth_state"),
        Index("ix_pending_source_auth_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    state: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(50))
    display_name: Mapped[str] = mapped_column(String(255))
    # The handshake dies with the project it was meant for; a source cannot outlive it.
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    # Exactly the Source columns this handshake will materialise, held verbatim so the
    # deferred creation is the same creation — grants, scoping and all.
    config: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    sync_policy: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    provider: Mapped[str] = mapped_column(String(30), default="native")
    provider_connection_id: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[str] = mapped_column(Text)  # base64(nonce || ciphertext)
    key_fingerprint: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceGroupMember(Base):
    """One mirrored ``member → group`` edge from a source's directory.

    Documents in a firm are shared with groups, not with individuals. A grant to
    ``group:entra:<guid>`` is therefore unenforceable on its own — the appliance has to
    know who is in that group. These rows are what the permission compiler expands a
    caller's identity against, and they are refreshed by the sync that produced them.

    Nested groups are expected to arrive already flattened by the connector.
    """

    __tablename__ = "source_group_members"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "group_id", "member_id", name="uq_source_group_member"
        ),
        Index("ix_source_group_members_member", "source_id", "member_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    group_id: Mapped[str] = mapped_column(String(255))  # e.g. "entra:<guid>", "sp:owners"
    member_id: Mapped[str] = mapped_column(String(255))  # email for users, id for groups
    member_type: Mapped[str] = mapped_column(String(10))  # "user" | "group"
    group_name: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IdentityProviderCredential(TimestampMixin, Base):
    """The OAuth client an administrator registered for sign-in, held encrypted.

    Keycloak has the secret too — it must, to redeem the code — but its admin API
    returns ``**********`` on read, so a re-test months after setup would have to ask
    the administrator to fetch the secret from Google again. This copy is what makes
    "Test sign-in" answerable at any time. Same AES-256-GCM key as connector
    credentials; the plaintext exists only inside the request that stores or tests it.
    """

    __tablename__ = "identity_provider_credentials"

    alias: Mapped[str] = mapped_column(String(60), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))  # google | entra | okta | oidc
    client_id: Mapped[str] = mapped_column(String(512))
    payload: Mapped[str] = mapped_column(Text)  # base64(nonce || ciphertext)
    key_fingerprint: Mapped[str] = mapped_column(String(16))
    discovery_url: Mapped[str] = mapped_column(Text)
    # The tenant id / Okta domain the discovery URL was built from, so the form can be
    # reopened showing what was entered rather than making it be typed again.
    extra_value: Mapped[str | None] = mapped_column(Text)
    issuer: Mapped[str | None] = mapped_column(Text)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_detail: Mapped[dict] = mapped_column(JSONVariant, default=dict)


class SchedulerHeartbeat(Base):
    """The last time a background scheduler loop looked at the clock.

    A schedule switched on in the admin UI only fires if some process is watching, and
    nothing about the configuration says whether one is. ``ki serve`` starts the loop, but
    a deployment that runs the API another way, or that sets
    ``KI_BACKUP_SCHEDULE_SECONDS=0``, has a UI reading "backups run nightly at 02:00" over
    a machine where nothing has run since March.

    So the loop writes here every tick, and the backup preflight reads it. One row per
    scheduler, updated in place: this is a liveness fact, not a history, and the interesting
    question is only ever "how long ago".
    """

    __tablename__ = "scheduler_heartbeats"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Which process, and what it decided last — enough to tell two schedulers apart when a
    # deployment accidentally runs both, and to see why a due backup was not enqueued.
    detail: Mapped[dict] = mapped_column(JSONVariant, default=dict)


class BackupSecret(Base):
    """A backup secret an administrator typed into the admin UI, encrypted at rest.

    The backup key and the object-storage credentials used to be environment variable
    names in ``config.json``, with the values in the deployment's compose file — which
    means the person configuring backups on a web page could not actually set them. They
    live here instead, under the same AES-256-GCM envelope and the same
    ``KI_CONNECTOR_CREDENTIAL_KEY`` as connector OAuth tokens, which are strictly more
    sensitive and have always been stored this way.

    The environment still wins when it holds a value, so a deployment that has been
    supplying the backup key since before this table existed keeps using the same key and
    keeps its old backups readable.
    """

    __tablename__ = "backup_secrets"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)  # base64(nonce || ciphertext)
    # Non-reversible id of the value, so the UI and a restore can both say "this is the
    # key that backup was written under" without the value going anywhere near them.
    fingerprint: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
