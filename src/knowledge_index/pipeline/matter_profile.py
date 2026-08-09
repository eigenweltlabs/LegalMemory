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

It works from the metadata the per-document stages already extracted rather than
re-reading the folder: repeating that work would make the pass too expensive to run
whenever a matter changes, and running it on every change is the whole design. A
78-file matter costs a few thousand tokens here. The one thing it does read is the
matter's intake paperwork, which it opens itself with `read_document`, because the
responsible partner and the practice group they sit in are written there and are
recorded nowhere else.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from knowledge_index.config import AppConfig
from knowledge_index.pipeline.providers import AgentTool
from knowledge_index.entity_names import normalize_entity_name
from knowledge_index.db.models import (
    Artifact,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    FirmPerson,
    Matter,
    MatterProfileQueue,
    MatterTeam,
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


class TeamMember(BaseModel):
    """One of this firm's people working the matter."""

    name: str = Field(description="Full name as written, e.g. 'Claudia Merritt'.")
    role: str = Field(
        description="Role ON THIS MATTER, as the document words it: 'Responsible "
        "Partner', 'Billing Partner', 'Lead Associate', 'Working Associate', "
        "'Supervising Partner', 'Of Counsel', 'Paralegal'. The role is per matter — "
        "the same partner is responsible on one and supervising on another."
    )
    title: str | None = Field(
        default=None,
        description="Their standing at the firm: Partner, Of Counsel, Associate, "
        "Paralegal. null when not stated.",
    )
    practice_group: str | None = Field(
        default=None,
        description="The person's practice group, copied VERBATIM where the document "
        "attaches one to their name — 'Banking & Finance', 'Funds & Asset "
        "Management', 'Litigation Department'. This is the firm's org chart, not an "
        "area of law, and it is never inferred from the subject matter. null when "
        "the documents do not state it.",
    )
    email: str | None = Field(default=None, description="Firm email if one appears.")


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
    team: list[TeamMember] = Field(
        default_factory=list,
        description="Everyone at THIS FIRM who works the matter. Return the COMPLETE "
        "team including anyone already listed as known — this replaces the stored "
        "team, so omitting a known member removes them. Add people the documents "
        "name that the known list is missing. Never include client contacts, "
        "opposing counsel or other side's advisers: only this firm's own people.",
    )
    group_evidence: str | None = Field(
        default=None,
        description="The filename and the phrase that named the responsible partner "
        "and their group, so the claim can be checked.",
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
    "The firm's PRACTICE GROUP and its people are declared, not deduced, and finding "
    "them is your FIRST job. The matter's own intake paperwork names the responsible "
    "partner and the group beside them — 'Claudia Merritt, Responsible Partner, "
    "Banking & Finance' — along with the billing partner and the associates staffed "
    "on it. Pick the likeliest file from the FOLDER listing and read it with "
    "read_document: a new-matter memorandum, engagement letter, conflict-check memo, "
    "deal summary or closing memo, whichever this matter has. Nothing in the listing "
    "reliably marks which file it is, so judge from the names and titles and read "
    "more than one when the first does not name anybody — this is worth several "
    "calls, because it is the only place these facts exist. Copy names, roles, titles "
    "and groups VERBATIM. The group is the firm's org chart and it is not the same "
    "question as the area of law: a matter whose subject is corporate entity "
    "formation can be filed in Funds & Asset Management, and only the memo shows it. "
    "If you have read the plausible files and none names a group, return null rather "
    "than inferring one from the subject. Only this firm's own people belong in "
    "`team` — never the client's staff, opposing counsel or the other side's "
    "advisers.\n\n"
    "Group versions from the listing as a whole. Drafts, redlines, near-finals and "
    "execution copies of one agreement belong together, ordered earliest to latest, "
    "with the governing one named.\n\n"
    "For matter_kind_node use the service_* tools: browse from service_roots, then "
    "VERIFY a candidate with service_node and judge by its DEFINITION, never by "
    "label similarity. A root's direct child ('Transactional Practice', 'Advisory "
    "Service') describes every matter of its family and so says nothing — open its "
    "children before settling for one. Any non-null id must have appeared in a "
    "service_* result; null is a fine answer and better than a wrong one.\n\n"
    "Apart from the intake paperwork, everything you need is already in the prompt — "
    "do not go re-reading the folder. Browse the service taxonomy, read the intake "
    "documents, then submit. An answer with matter_kind_node null is worth far more "
    "than no answer at all."
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


def read_document_tool(session: Session, matter: Matter) -> AgentTool:
    """Lets the agent read any file in the folder it is profiling.

    The pass works from extracted metadata for everything else, but the firm's
    practice group and its responsible partner are STATED in the intake
    paperwork and nowhere else — "Claudia Merritt, Responsible Partner, Banking
    & Finance" — so that text has to be reachable.

    Which file that is, is a judgement: it is an engagement letter in one matter,
    a conflict-check memo in the next, a closing memo in a third, and the file
    names agree on nothing. This used to pick it by matching seven substrings
    against the filename, which found it in a minority of matters and returned
    empty for the rest — and an empty intake reads to the model exactly like a
    matter whose paperwork names no one, so it invented nothing and the team
    came back blank. The folder listing already names every file; the model
    picks from it and reads, which is the same judgement the rest of this
    pipeline makes with a model rather than a pattern.
    """

    def read(args: dict) -> str:
        wanted = str(args.get("path", "")).strip()
        rows = session.execute(
            select(SourceObject.path, DocumentVersion.content_hash)
            .join(
                DocumentVersionSource,
                DocumentVersionSource.source_object_id == SourceObject.id,
            )
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentVersionSource.version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.matter_id == matter.id)
        ).all()
        # The listing shows paths relative to the estate root, so accept either
        # form rather than making the model reconstruct a prefix it never saw.
        match = next(
            (
                (path, chash)
                for path, chash in rows
                if path == wanted or path.split("/", 1)[-1] == wanted
                or path.endswith("/" + wanted)
            ),
            None,
        )
        if match is None:
            return json.dumps(
                {
                    "error": f"no file {wanted!r} in this matter",
                    "files": sorted(
                        path.split("/", 1)[-1] if "/" in path else path
                        for path, _ in rows
                    ),
                }
            )
        path, chash = match
        artifact = session.scalar(
            select(Artifact).where(
                Artifact.content_hash == chash, Artifact.kind == "structured_json"
            )
        )
        text = ((artifact.payload or {}).get("text") if artifact else "") or ""
        if not text:
            return json.dumps({"path": path, "error": "no extracted text for this file"})
        return json.dumps({"path": path, "text": text}, ensure_ascii=False)

    return AgentTool(
        name="read_document",
        description=(
            "Read the extracted text of one file in this matter, by the path shown "
            "in the FOLDER listing. Use it on the matter's own intake paperwork — "
            "the new-matter form, engagement letter, conflict-check memo, deal or "
            "closing summary, whatever this matter has — because that is where the "
            "firm writes down its responsible partner and the practice group they "
            "sit in. Read more than one if the first does not name them."
        ),
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=read,
    )


def _known_team(session: Session, matter: Matter) -> str:
    """Who is already recorded on this matter.

    Given to the agent so a re-run is a review rather than a fresh guess: it can
    confirm the people already known, and add the ones a newly-arrived document
    names. Without this every pass would re-derive the team from scratch and the
    membership would flap as documents land.
    """
    rows = session.execute(
        select(FirmPerson.name, MatterTeam.role, FirmPerson.title, FirmPerson.practice_group)
        .join(MatterTeam, MatterTeam.person_id == FirmPerson.id)
        .where(MatterTeam.matter_id == matter.id)
        .order_by(MatterTeam.role, FirmPerson.name)
    ).all()
    if not rows:
        return "\n\nKNOWN FIRM TEAM: none recorded yet."
    lines = [
        f"  {name} — {role}" + (f", {title}" if title else "")
        + (f" ({group})" if group else "")
        for name, role, title, group in rows
    ]
    return (
        "\n\nKNOWN FIRM TEAM (already recorded — confirm these and ADD anyone the "
        "documents name that is missing):\n" + "\n".join(lines)
    )


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
        f"{_folder_view(session, matter)}"
        f"{_known_team(session, matter)}{menu}"
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
                [read_document_tool(session, matter)]
                + (
                    service_navigation_tools(service_scope, visited)
                    if service_scope is not None
                    else []
                )
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
    apply_team(session, matter, profile)
    matter.profile = {
        "instrument": profile.instrument,
        "group_evidence": profile.group_evidence,
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


def normalize_group(value: str | None) -> str | None:
    """One spelling for a group a firm writes several ways.

    "Healthcare & Life Sciences Practice Group", "Litigation Department" and
    "Banking and Finance" all name a group whose members would otherwise not match
    each other, or a caller's filter. The firm's own wording is kept, minus the
    organisational suffix.
    """
    if not value:
        return None
    group = value.strip().strip(" ,.-")
    for suffix in (" Practice Group", " Practice", " Group", " Department", " Team"):
        if group.endswith(suffix):
            group = group[: -len(suffix)].strip()
            break
    return group.replace(" and ", " & ") or None


ROLE_PRECEDENCE = (
    "responsible partner",
    "relationship partner",
    "billing partner",
    "supervising partner",
    "lead partner",
    "partner",
)


def apply_team(session: Session, matter: Matter, profile: MatterProfile) -> None:
    """Record who works the matter, and take the matter's group from its owner.

    People are resolved by normalised name so the same lawyer across forty matters
    is one row, and their title and group are filled in from whichever matter
    happened to state them — a partner named without a group on one matter is still
    that partner.

    The matter's practice group is then the RESPONSIBLE PARTNER's group, falling
    back down the seniority order. That is how a firm actually files: a matter
    belongs to the book of the partner who owns it, which is why moving a partner
    moves their matters. Deriving it from subject matter instead is what filed a
    fund matter under Corporate.
    """
    if not profile.team:
        return
    wanted: dict[tuple[str, str], TeamMember] = {}
    for member in profile.team:
        key = (normalize_entity_name(member.name or ""), (member.role or "").strip())
        if key[0] and key[1]:
            wanted[key] = member

    session.execute(delete(MatterTeam).where(MatterTeam.matter_id == matter.id))
    owner_group = None
    best_rank = len(ROLE_PRECEDENCE)
    for (normalized, role), member in wanted.items():
        person = session.scalar(
            select(FirmPerson).where(FirmPerson.normalized_name == normalized)
        )
        if person is None:
            person = FirmPerson(name=member.name.strip(), title=member.title,
                                practice_group=normalize_group(member.practice_group))
            session.add(person)
            session.flush()
        else:
            # Later documents fill gaps but never overwrite: the first statement of
            # someone's group is as good as the fortieth, and churn here would move
            # every matter that partner owns.
            if person.title is None and member.title:
                person.title = member.title
            if person.practice_group is None and member.practice_group:
                person.practice_group = normalize_group(member.practice_group)
        session.add(MatterTeam(matter_id=matter.id, person_id=person.id, role=role))
        rank = next(
            (i for i, r in enumerate(ROLE_PRECEDENCE) if r in role.lower()),
            len(ROLE_PRECEDENCE),
        )
        group = normalize_group(member.practice_group) or person.practice_group
        if group and rank < best_rank:
            owner_group, best_rank = group, rank
    if owner_group:
        matter.practice_group = owner_group
    session.flush()


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
