"""Decide a matter's own facts from its whole folder, once its documents settle.

The insertion pipeline is per-document and parallel, which is right for what a
document IS — its type, its date, its parties. It is wrong for what a MATTER is.
The classify agent sees one file and is asked about the matter, so it answers
about the file: a Master Clinical Trial Agreement read alone is a contract, and
matters full of trial agreements, IRB memos and consent forms were filed under
Contract Law. Every transactional matter is contracts, so that label carries no
information — and `practice_area` is the key agents scope on, so the matter did
not merely get a poor label, it vanished from the enumeration while the
enumeration still reported itself complete.

Aggregating the per-document answers does not fix it. Replaying the stored
classifications through a confidence-weighted vote returned Contract Law with 91%
agreement: the documents genuinely are contracts, so consensus is an artifact of
asking the wrong question many times. Only the folder shows the matter.

So this pass is given what a partner gets on opening the folder — the file
listing, and the fields already extracted per document — and asked the questions
a document cannot answer:

    what practice is this, what service is being performed, what is the deal,
    which document constitutes it, did it actually happen, and which of these
    files are versions of one another.

It deliberately does NOT re-read the documents. Re-reading would repeat work the
per-document stages already paid for and would make the pass too expensive to run
whenever a matter changes — and running it on every change is the whole design.
A 78-file matter costs a few thousand tokens here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Matter,
    MatterProfileQueue,
    SourceObject,
)

# How long a matter must be untouched before it is worth profiling. Bulk loads set
# this high so a matter receiving hundreds of documents profiles once, at the end
# of its flood, instead of after every arrival; steady state sets it low so a
# single new document is reflected promptly. It is only a cost/latency knob — a
# sweep at the end of a run profiles everything regardless, so a badly chosen
# window wastes calls or delays them but cannot leave a matter wrong.
DEFAULT_DEBOUNCE_SECONDS = 600


class VersionFamily(BaseModel):
    """Files that are drafts, redlines and executions of ONE document."""

    files: list[str] = Field(
        description="Filenames in this family, EARLIEST FIRST. Copy them exactly as "
        "they appear in the folder listing."
    )
    authoritative: str | None = Field(
        default=None,
        description="The filename that governs — the executed or final one. null "
        "when every version is a draft and none supersedes the others.",
    )


class MatterProfile(BaseModel):
    practice_area_node: str | None = Field(
        default=None,
        description="Area of Law node id for what this MATTER is about — the "
        "practice group that would staff it. Not the document type: a clinical "
        "trial agreement is a contract, but the matter is healthcare. null when the "
        "folder genuinely does not show it.",
    )
    matter_kind_node: str | None = Field(
        default=None,
        description="Service node id for what the firm is DOING here. Must be an id "
        "seen in a service_* tool result. null when unclear.",
    )
    lifecycle: str = Field(
        description="Where the matter stands, judged from the documents that would "
        "record it: 'executed' (the deal closed and signed papers exist), 'closed' "
        "(work finished, no executed instrument), 'terminated' (abandoned, "
        "withdrawn, disengaged), 'dormant' (paused, may revive), 'in_progress'. "
        "Beware the word 'termination' in a filename: a UCC-3 termination or a "
        "notice terminating an OLD facility is routine housekeeping inside a "
        "SUCCESSFUL deal, not a dead matter. A matter that was dormant and then "
        "reactivated is in_progress."
    )
    instrument: str | None = Field(
        default=None,
        description="What the deal IS, in a few words — 'revolving credit "
        "facility', 'term loan B', 'senior notes offering', 'chapter 11', 'fund "
        "formation'. The matter's OWN instrument, never one it merely refinances, "
        "reviews or pays off: a term loan that repays an old revolver is a term "
        "loan. null for non-transactional matters.",
    )
    principal_document: str | None = Field(
        default=None,
        description="Filename of the document that CONSTITUTES the matter — the "
        "executed agreement, the filed petition, the offering memorandum. null when "
        "no single document does.",
    )
    summary: str = Field(
        description="One sentence: who, what, and whether it happened."
    )
    version_families: list[VersionFamily] = Field(
        default_factory=list,
        description="Groups of files that are versions of one document. Only include "
        "a family with two or more members. Judge from the whole listing at once: "
        "seen individually a near-final draft and a final draft look unrelated, "
        "which is how one agreement ended up split across two records.",
    )
    evidence: str = Field(
        description="The filenames that decided lifecycle and instrument."
    )


PROFILE_SYSTEM = (
    "You are a law-firm knowledge lawyer looking at ONE matter's folder to record "
    "what the matter is. You are not reading the documents; you are reading the "
    "file listing and the metadata already extracted from each file, exactly as a "
    "partner would when opening a matter folder.\n\n"
    "Answer only what the FOLDER shows. Any single document in a firm is a "
    "contract, a memo or a letter — that is its type, not the matter's practice. "
    "The practice is what the collection is about and which group would staff it: "
    "a folder of trial agreements, IRB memoranda and consent forms is healthcare "
    "work however the individual papers are typed.\n\n"
    "Distinguish what the matter IS from what it mentions. A term-loan matter that "
    "reviews, refinances or pays off an existing revolving facility is a term-loan "
    "matter. Distinguish what happened from what was prepared for: judge lifecycle "
    "from the document that would record the outcome, and remember that a "
    "termination notice or UCC-3 release is usually the ordinary closing mechanics "
    "of a deal that DID happen.\n\n"
    "Group versions from the listing as a whole. Drafts, redlines, near-finals and "
    "execution copies of one agreement belong together, ordered earliest to latest, "
    "with the governing one named.\n\n"
    "For matter_kind_node use the service_* tools: browse from service_roots, then "
    "VERIFY a candidate with service_node and judge by its DEFINITION, never by "
    "label similarity. A root's direct child ('Transactional Practice', 'Advisory "
    "Service') describes every matter of its family and so says nothing — open its "
    "children before settling for one. Any non-null id must have appeared in a "
    "service_* result; null is a fine answer and better than a wrong one.\n\n"
    "Everything else you need is already in the prompt — do not go looking for it. "
    "Spend at most a handful of tool calls on the service taxonomy, then submit "
    "your result. An answer with matter_kind_node null is worth far more than no "
    "answer at all."
)


def _folder_view(session: Session, matter: Matter) -> str:
    """The listing plus what the per-document stages already extracted.

    This is the whole input. Titles, types, dates and statuses are reused rather
    than recomputed — that work is done and paying for it twice is what would make
    profiling-on-every-change unaffordable.
    """
    rows = session.execute(
        select(
            SourceObject.path,
            Document.title,
            Document.doc_type,
            Document.doc_date,
            DocumentVersion.status,
        )
        .join(DocumentVersionSource, DocumentVersionSource.source_object_id == SourceObject.id)
        .join(DocumentVersion, DocumentVersion.id == DocumentVersionSource.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Document.matter_id == matter.id)
        .order_by(SourceObject.path)
    ).all()
    lines = []
    for path, title, _doc_type, doc_date, status in rows:
        rel = path.split("/", 1)[1] if "/" in path else path
        when = doc_date.date().isoformat() if doc_date else "no date"
        lines.append(f"  {rel}  [{status}, {when}] {(title or '').strip()[:70]}")
    return "\n".join(lines)


def build_prompt(session: Session, matter: Matter, config: AppConfig) -> str:
    """The user turn: the matter's identity, its folder, and the area menu."""
    area_scope = (
        config.ontology_facet("area_of_law")
        if "area_of_law" in config.ontology.active_facets
        else None
    )
    menu = ""
    if area_scope is not None:
        menu = "\n\nAREA OF LAW MENU:\n" + area_scope.indented_menu()
    ref = (matter.reference_numbers or ["?"])[0]
    return (
        f"MATTER {ref}\nTITLE: {matter.title}\n\n"
        f"FOLDER ({len(_folder_view(session, matter).splitlines())} files) — "
        f"path [version status, document date] extracted title:\n"
        f"{_folder_view(session, matter)}{menu}"
    )


def mark_matter_dirty(session: Session, matter_id: str, *, now: datetime | None = None) -> None:
    """Record that a matter changed. Called once per classified document.

    One upsert inside a transaction that is committing anyway. Repeats collapse
    onto the same row, which is what turns a million documents into one profile per
    matter instead of one per document.
    """
    stamp = now or datetime.now(UTC)
    row = session.get(MatterProfileQueue, matter_id)
    if row is None:
        session.add(MatterProfileQueue(matter_id=matter_id, last_marked_at=stamp))
    else:
        row.last_marked_at = stamp


def due_matters(
    session: Session,
    *,
    debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
    limit: int = 50,
    ignore_debounce: bool = False,
) -> list[str]:
    """Matters that have stopped changing for long enough to be worth profiling.

    ``ignore_debounce`` is the end-of-run sweep: it takes everything queued
    regardless of the window, which is what makes the window a cost knob rather
    than a correctness one.
    """
    statement = select(MatterProfileQueue.matter_id)
    if not ignore_debounce:
        cutoff = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() - debounce_seconds, tz=UTC
        )
        statement = statement.where(MatterProfileQueue.last_marked_at <= cutoff)
    return list(
        session.scalars(
            statement.order_by(MatterProfileQueue.last_marked_at).limit(limit)
        ).all()
    )


def profile_matter(session: Session, config: AppConfig, matter_id: str) -> MatterProfile | None:
    """Re-derive one matter's own facts and write them. Idempotent.

    Same folder and same extracted fields produce the same profile, so re-running
    costs a call and never correctness — which is what lets this be triggered on
    every change rather than once at a moment nobody can identify. A live DMS
    connector has no "insertion finished" event to hang a one-shot pass on.
    """
    from knowledge_index.pipeline.ontology_tools import service_navigation_tools
    from knowledge_index.pipeline.providers import chat_agent

    matter = session.get(Matter, matter_id)
    row = session.get(MatterProfileQueue, matter_id)
    if matter is None:
        if row is not None:
            session.delete(row)
        return None
    if matter.imported:
        # Practice-management imports are authoritative; the folder does not
        # outrank the firm's own system of record.
        if row is not None:
            session.delete(row)
        return None

    service_scope = (
        config.ontology_facet("service")
        if "service" in config.ontology.active_facets
        else None
    )
    visited: set[str] = set()
    stage = config.pipeline.stage("classify_matter")
    try:
        profile = chat_agent(
            stage.model,
            config,
            system=PROFILE_SYSTEM,
            user=build_prompt(session, matter, config),
            tools=(
                service_navigation_tools(service_scope, visited)
                if service_scope is not None
                else []
            ),
            final_schema=MatterProfile,
            trace_tags=["matter_profile"],
        )
    except Exception as exc:  # noqa: BLE001 - a failed profile must not fail the run
        if row is not None:
            row.attempts = (row.attempts or 0) + 1
            row.last_error = f"{type(exc).__name__}: {exc}"[:500]
        return None

    area_scope = (
        config.ontology_facet("area_of_law")
        if "area_of_law" in config.ontology.active_facets
        else None
    )
    if area_scope is not None and profile.practice_area_node:
        if resolved := area_scope.resolve(profile.practice_area_node):
            matter.practice_area = resolved
    if service_scope is not None and profile.matter_kind_node:
        if resolved := service_scope.resolve(profile.matter_kind_node):
            matter.matter_kind = resolved
    matter.lifecycle = profile.lifecycle
    matter.profile = {
        "instrument": profile.instrument,
        "principal_document": profile.principal_document,
        "summary": profile.summary,
        "evidence": profile.evidence,
        "version_families": [f.model_dump() for f in profile.version_families],
        "model": stage.model,
        "prompt_version": stage.producer_version,
    }
    apply_version_families(session, matter, profile)
    if row is not None:
        row.profiled_at = datetime.now(UTC)
        row.last_error = None
        session.delete(row)
    return profile


def apply_version_families(
    session: Session, matter: Matter, profile: MatterProfile
) -> int:
    """Reunite version chains the per-document relate stage could not see.

    Relate compares a file pairwise against ONE neighbour, so a six-draft
    agreement can end up as two documents of three with nothing linking them —
    which is how a caller came to cite a near-final draft while the final sat on a
    record it never saw. Shown the whole listing at once the grouping is obvious,
    so the profile decides it and this applies it.

    Only merges are applied, never splits: a family the profile omits is left
    exactly as it is. A wrong merge is visible and repairable; silently tearing
    apart a chain some other evidence established is neither.
    """
    merged = 0
    for family in profile.version_families:
        names = [f.rsplit("/", 1)[-1] for f in family.files]
        if len(names) < 2:
            continue
        found: list[tuple[str, DocumentVersion]] = []
        for name in names:
            row = session.execute(
                select(SourceObject.path, DocumentVersion)
                .join(DocumentVersionSource,
                      DocumentVersionSource.source_object_id == SourceObject.id)
                .join(DocumentVersion,
                      DocumentVersion.id == DocumentVersionSource.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Document.matter_id == matter.id,
                       SourceObject.path.like(f"%/{name}"))
            ).first()
            if row is not None:
                found.append((name, row[1]))
        if len(found) < 2:
            continue
        documents = {version.document_id for _, version in found}
        if len(documents) == 1:
            survivor = documents.pop()
        else:
            # Keep the record holding the authoritative version, since its title
            # and typing already describe the finished agreement rather than an
            # early turn.
            survivor = next(
                (v.document_id for n, v in found if n == family.authoritative),
                found[-1][1].document_id,
            )
            merged += 1
        for ordinal, (_, version) in enumerate(found, start=1):
            version.document_id = survivor
            version.ordinal = ordinal
        session.flush()
        document = session.get(Document, survivor)
        if document is not None:
            document.latest_final_version_id = next(
                (v.id for n, v in found if n == family.authoritative), None
            )
    return merged
