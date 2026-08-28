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
78-file matter costs a few thousand tokens here. What it does read is whatever
names the firm's own people on the matter — the responsible partner, the group they
sit in, who else is staffed — because that is written inside the documents and
recorded nowhere structured, and no rule can say which documents. The agent searches
the folder's text and reads what it finds.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from knowledge_index.config import AppConfig
from knowledge_index.pipeline.providers import AgentTool, chat_agent
from knowledge_index.entity_names import normalize_entity_name, normalize_group
from knowledge_index.db.models import (
    Artifact,
    Client,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    FirmPerson,
    FirmPracticeGroup,
    Matter,
    MatterClient,
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


def _parse_iso_date(value: str | None) -> datetime | None:
    """A model-provided ISO date, or None when absent or malformed.

    Malformed is treated as absent rather than an error: a bad date must not
    fail the whole profile, and the evidence filenames still let a human check.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip()[:10])
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


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
    engagement_status: str = Field(
        description="The ENGAGEMENT's own status — is the firm's work on this file "
        "running or ended — valued as SALI LMSS Engagement Service Status words it: "
        "'open' (work product is still being produced), 'waiting' (on hold pending "
        "an external party), 'closed' (the work has ended), 'canceled' (abandoned, "
        "withdrawn or disengaged before the work completed). This is a DIFFERENT "
        "question from lifecycle: a matter whose deal signed can be closed here "
        "(its work ended) or open (post-closing work continues). Judge it from the "
        "file's own closing papers — a matter-closing letter, a final invoice, a "
        "disengagement letter decides 'closed' outright: READ that document and "
        "take its date. Where no such paper exists but the file's concluding "
        "mechanics are complete and nothing follows them, the work ended with the "
        "last work product. The ABSENCE of a dedicated closing letter is never "
        "evidence that work continues.",
    )
    engagement_close_date: str | None = Field(
        default=None,
        description="ISO date (YYYY-MM-DD) the work ended, for closed/canceled: "
        "the closure paper's own date, or failing that the date of the final work "
        "product. null while open/waiting.",
    )
    engagement_evidence: str | None = Field(
        default=None,
        description="The filename(s) that decided engagement_status, so the claim "
        "can be checked.",
    )
    engagement_confidence: str = Field(
        default="low",
        description="'high' when a closure paper says it in words; 'medium' when "
        "judged from completed concluding mechanics plus nothing after; 'low' when "
        "judged from silence alone or the folder is ambiguous.",
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
    client_name: str | None = Field(
        default=None,
        description="The party THIS FIRM acts for on THIS matter, as the engagement "
        "letter, the intake form or the invoices name it. Exactly one, and never the "
        "other side: not the counterparty, not the target being acquired, not a fund "
        "the client is only reviewing or investing into, not the issuer on a deal the "
        "client underwrites. On an investor-side or co-invest matter the client is the "
        "investor we act for, even though almost every document in the folder is about "
        "the fund or target instead. null when the folder does not show it.",
    )
    client_evidence: str | None = Field(
        default=None,
        description="The filename and the phrase that names the client, so the claim "
        "can be checked.",
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
    "Lifecycle and engagement_status answer DIFFERENT questions and are judged "
    "separately: lifecycle is where the matter's papers stand; engagement_status "
    "is whether the firm's own work on the file is running or ended. Firms write "
    "the end of work down — a matter-closing letter, a final invoice, a "
    "disengagement letter — and where the listing shows such a paper, read it: it "
    "decides engagement_status and its date whatever the lifecycle is.\n\n"
    "Say WHO THE FIRM ACTS FOR, and say it once. The client is not whoever the "
    "documents are about — on an acquisition the papers are full of the target, on a "
    "co-investment they are full of the fund, on an investor-side review they are the "
    "other manager's LPA — and every one of those is the OTHER side. The client is "
    "the party that engaged this firm: the counterparty to the engagement letter, the "
    "entity on the intake form, the addressee of the invoices. Where the folder shows "
    "no engagement paperwork, the client is still whoever the firm's own memoranda "
    "write for, not the party they write about.\n\n"
    "The firm's PRACTICE GROUP and its people are declared, not deduced, and finding "
    "them is your FIRST job. Somewhere in this folder the firm has written who runs "
    "this matter and which group they sit in — 'Claudia Merritt, Responsible Partner, "
    "Banking & Finance' — along with the billing partner and the associates staffed "
    "on it. WHERE it is written differs from firm to firm and from matter to matter, "
    "and no filename tells you. It may be a new-matter memorandum or engagement "
    "letter; it may equally be a signature block, the author line of a memo, the 'cc' "
    "on a cover letter, a responsibilities column in a closing checklist, or the "
    "timekeeper names on a bill. It is often SCATTERED: the partner in one file, the "
    "associates in another, a specialist from a second group in a third. So work it "
    "like a search, not a lookup. Use find_in_matter with the words a firm would "
    "actually write, several different ways, and read whole documents with "
    "read_document when a hit needs its context. Take as many calls as this needs — "
    "it is the only place these facts exist, and a team of one when the matter is "
    "staffed by five is a wrong answer, not a partial one. Assemble the complete "
    "team from everything you found, wherever you found it. Copy names, roles, titles "
    "and groups VERBATIM. The group is the firm's org chart and it is not the same "
    "question as the area of law: a matter whose subject is corporate entity "
    "formation can be filed in Funds & Asset Management, and only the paperwork shows "
    "it. Return null for a group only after searching for it and finding nothing — "
    "never infer one from the subject. Only this firm's own people belong in `team`: "
    "never the client's staff, opposing counsel or the other side's advisers, and "
    "when a document names both sides, keep the ones this firm employs.\n\n"
    "Group versions from the listing as a whole. Drafts, redlines, near-finals and "
    "execution copies of one agreement belong together, ordered earliest to latest, "
    "with the governing one named.\n\n"
    "For matter_kind_node use the service_* tools: browse from service_roots, then "
    "VERIFY a candidate with service_node and judge by its DEFINITION, never by "
    "label similarity. A root's direct child ('Transactional Practice', 'Advisory "
    "Service') describes every matter of its family and so says nothing — open its "
    "children before settling for one. Any non-null id must have appeared in a "
    "service_* result; null is a fine answer and better than a wrong one.\n\n"
    "The folder listing and the extracted fields answer everything else — read "
    "documents to find the people and to settle what the matter IS, not to redo the "
    "per-file extraction. Search out the team, browse the service taxonomy, then "
    "submit. An answer with matter_kind_node null is worth far more than no answer "
    "at all."
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


def matter_reading_tools(session: Session, matter: Matter) -> list[AgentTool]:
    """Let the agent read and search the folder it is profiling.

    Everything else in this pass comes from metadata the per-document stages
    already extracted. The people do not: who works a matter, what they are
    called, and which practice group they belong to are written inside the
    documents and recorded nowhere structured.

    Where they are written varies by firm and by matter. A new-matter memo names
    a responsible partner and a group beside them; a firm without that form has
    them in the engagement letter's signature block, in the author line of a
    memorandum, in the "cc" of a cover letter, in a closing-checklist column of
    responsibilities, or in the timekeeper names on a bill. This must work for
    all of it, so the agent gets both verbs — find text anywhere in the matter,
    and read any file whole — and decides for itself which files carry the
    answer. It is never told which filenames to look for.

    That is a correction of what was here before: an "intake document" chosen by
    matching seven substrings against the filename, truncated to its first 4,000
    characters. Firms name their paperwork whatever they name it, so the match
    failed on most matters; where it hit, a roster of who is staffed on the
    matter sits well past a letter's fourth thousand character. Neither the
    choice of file nor the amount read is a decision a rule can make.
    """
    # One load per file per matter. `find_in_matter` needs every text and
    # `read_document` usually asks for one it has already scanned, so caching
    # here is the difference between one pass over the folder and one per call.
    texts: dict[str, str] = {}
    loaded = False

    def _load() -> dict[str, str]:
        nonlocal loaded
        if loaded:
            return texts
        rows = session.execute(
            select(SourceObject.path, Artifact.payload)
            .join(
                DocumentVersionSource,
                DocumentVersionSource.source_object_id == SourceObject.id,
            )
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentVersionSource.version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(
                Artifact,
                (Artifact.content_hash == DocumentVersion.content_hash)
                & (Artifact.kind == "structured_json"),
            )
            .where(Document.matter_id == matter.id)
        ).all()
        for path, payload in rows:
            text = (payload or {}).get("text") or ""
            if text:
                texts[path] = text
        loaded = True
        return texts

    def _resolve(wanted: str) -> str | None:
        """Accept a path as the listing shows it, or as the estate stores it."""
        wanted = wanted.strip()
        for path in _load():
            if path == wanted or path.split("/", 1)[-1] == wanted or path.endswith(
                "/" + wanted
            ):
                return path
        return None

    def read(args: dict) -> str:
        wanted = str(args.get("path", ""))
        path = _resolve(wanted)
        if path is None:
            return json.dumps(
                {
                    "error": f"no readable file {wanted.strip()!r} in this matter",
                    "files": sorted(
                        p.split("/", 1)[-1] if "/" in p else p for p in _load()
                    ),
                }
            )
        # Whole text. A cap here is what hid the team roster before.
        return json.dumps({"path": path, "text": _load()[path]}, ensure_ascii=False)

    def find(args: dict) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return json.dumps({"error": "query is required"})
        needle = query.casefold()
        # Every matching line, whole, from every file — no per-file cap and no
        # cut-off at some number of hits. A line is the unit because a name in a
        # signature block, an author line or a cc list is one line, so the model
        # can usually answer from the hits and read whole files only when it
        # needs the surrounding paragraph.
        hits = [
            {
                "path": path,
                "lines": [
                    line.strip()
                    for line in text.splitlines()
                    if needle in line.casefold()
                ],
            }
            for path, text in sorted(_load().items())
        ]
        found = [hit for hit in hits if hit["lines"]]
        return json.dumps(
            {
                "query": query,
                "files_searched": len(hits),
                "files_matched": len(found),
                "matches": found,
            },
            ensure_ascii=False,
        )

    return [
        AgentTool(
            name="find_in_matter",
            description=(
                "Search the text of EVERY file in this matter for a phrase, "
                "case-insensitively, and get back every matching line with the "
                "file it came from. Nothing is capped or sampled: what comes back "
                "is all of it, so a broad word returns a lot and a phrase returns "
                "what you meant. This is how you find facts that are not in any "
                "one predictable file — who the firm's people on this matter are, "
                "what they are called, and which practice group they sit in. Try "
                "the words a firm would actually write ('Responsible Partner', "
                "'Billing Partner', 'Partner in Charge', 'Supervising', 'Attorney "
                "of record', 'cc:', 'Prepared by', 'Group', 'Department', the "
                "firm's email domain) and follow whatever the hits show you. "
                "Search several ways before concluding that nobody is named."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=find,
        ),
        AgentTool(
            name="read_document",
            description=(
                "Read one file in this matter whole, by the path shown in the "
                "FOLDER listing or returned by find_in_matter. Use it when a hit "
                "needs its surrounding paragraph, or to read a document that looks "
                "like it introduces the matter — a new-matter form, engagement "
                "letter, conflict-check memo, closing summary, whatever this firm "
                "happens to keep. Read as many as the matter needs."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=read,
        ),
    ]


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
                matter_reading_tools(session, matter)
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

    # Settle the groups first, each committed as it is decided, so that the writes
    # below never hold a lock across a model call. See resolve_team_groups.
    groups = resolve_team_groups(session, config, profile)
    matter = session.get(Matter, matter_id)
    if matter is None:  # deleted while the agent was reading
        return None
    row = session.get(MatterProfileQueue, matter_id)

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
    matter.engagement_status = (
        profile.engagement_status
        if profile.engagement_status in ("open", "waiting", "closed", "canceled")
        else None
    )
    matter.engagement_close_date = _parse_iso_date(profile.engagement_close_date)
    # Deterministic bounds over the folder's dated documents: pure MIN/MAX of the
    # per-document doc_date the extraction stages already produced. Recomputed
    # here so they stay current with the folder without any model involvement.
    first_doc, last_doc = session.execute(
        select(func.min(Document.doc_date), func.max(Document.doc_date)).where(
            Document.matter_id == matter.id
        )
    ).one()
    matter.first_document_date = first_doc
    matter.last_document_date = last_doc
    apply_client(session, matter, profile)
    apply_team(session, config, matter, profile, groups)
    matter.profile = {
        "instrument": profile.instrument,
        "group_evidence": profile.group_evidence,
        "principal_document": profile.principal_document,
        "summary": profile.summary,
        "evidence": profile.evidence,
        "engagement_evidence": profile.engagement_evidence,
        "engagement_confidence": profile.engagement_confidence
        if profile.engagement_confidence in ("high", "medium", "low")
        else "low",
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


class GroupJudgement(BaseModel):
    """Whether a newly-seen group name is one the firm already has."""

    is_practice_group: bool = Field(
        default=True,
        description="False when the text does not name a practice group of THIS firm "
        "at all: a cross-staffing note ('Government Contracts / Banking & Finance'), "
        "a parenthetical aside ('IP (cross-staffed for pricing)'), an outside firm or "
        "expert, or the firm's own name. Those are facts about the work, not books of "
        "business, and must not become groups.",
    )
    same_as: str | None = Field(
        default=None,
        description="The EXACT name of the existing group this one also names, "
        "copied from the list you were given. null when it is a different group.",
    )
    canonical_name: str | None = Field(
        default=None,
        description="When this is a new group, the firm's own name for it as it "
        "would be written on a letterhead — 'Mergers & Acquisitions', not 'mergers & "
        "acquisitions' and not a sentence fragment. null to keep it as written.",
    )
    reason: str = Field(
        description="One sentence: what in the two names or the evidence settles it."
    )


GROUP_SYSTEM = (
    "You decide whether two names refer to the same practice group of one law firm.\n\n"
    "A firm writes its own org chart loosely. The same group appears as 'Capital "
    "Markets', 'Capital Markets & Structured Finance' and 'the Capital Markets "
    "team' across three documents, and all three are one book of business. But a "
    "firm can equally run 'Capital Markets' and a separate 'Structured Finance' "
    "group, and merging those loses the distinction the firm draws.\n\n"
    "So judge on the evidence you are given, not on how similar the words look. A "
    "longer name that names the shorter one plus a specialisation is usually the "
    "same group described more fully. Two names that share no head noun are "
    "usually different groups. When the evidence does not settle it, answer null: "
    "a firm with one group too many is repairable, a firm that has silently "
    "merged two of its practices is not.\n\n"
    "Before any of that, decide whether the text names a practice group at all. "
    "Documents say 'Government Contracts / Banking & Finance' to record that two "
    "groups share a matter, put an aside in brackets, name the outside firm on the "
    "other side, or print the firm's own name on the letterhead. None of those is a "
    "book of business. A firm has far fewer groups than its documents have ways of "
    "referring to work, so treat a new group as the exception it is."
)


def resolve_group(
    session: Session,
    config: AppConfig,
    raw: str | None,
    *,
    evidence: str | None = None,
) -> str | None:
    """The firm's own name for a group, creating it the first time it is seen.

    Search-then-create against `firm_practice_groups`, the same discipline the
    people get. Three layers, cheapest first:

    1. Normalisation — case, "and"/"&", a trailing "practice group" or
       "department". Deterministic, and it catches most repeat spellings.
    2. The alias list — a spelling some earlier document used and that was
       already resolved onto a group. This is what stops the same judgement being
       paid for on every matter.
    3. A model, once, shown the groups this estate already has and the phrase the
       document used. Only reached for a spelling never seen before, and its
       answer is written back as an alias so it is never asked twice.

    Returns the group's canonical name. Nothing is merged that the model does not
    affirm: an unrecognised spelling becomes its own group rather than being
    attached to the nearest-looking one.
    """
    normalized = normalize_group(raw)
    if not normalized:
        return None
    key = normalize_entity_name(normalized)
    if not key:
        return None

    existing = session.scalars(select(FirmPracticeGroup)).all()
    for group in existing:
        if group.normalized_name == key or key in (group.aliases or []):
            return group.name

    # Bound before the branch: the first group of a fresh corpus is never judged,
    # because there is nothing to compare it against.
    judgement: GroupJudgement | None = None
    if existing:
        try:
            judgement = chat_agent(
                config.pipeline.stage("classify_matter").model,
                config,
                system=GROUP_SYSTEM,
                user=(
                    "THIS FIRM'S PRACTICE GROUPS AS ALREADY RECORDED:\n"
                    + "\n".join(f"  {group.name}" for group in existing)
                    + f"\n\nA DOCUMENT NAMES THIS GROUP: {normalized}"
                    + (f"\nWHERE IT SAYS SO: {evidence}" if evidence else "")
                    + "\n\nIs this one of the groups above, or a different one?"
                ),
                tools=[],
                final_schema=GroupJudgement,
                trace_tags=["practice_group"],
            )
        except Exception:  # noqa: BLE001 - an unjudged group is still a group
            judgement = None
        if judgement is not None and not judgement.is_practice_group:
            # A cross-staffing note, a bracketed aside, an outside firm, or our own
            # letterhead. Creating a group for each multiplies the firm's org chart
            # several times over, and every invented group then competes as a filter
            # value on the retrieval path.
            return None
        if judgement is not None and judgement.same_as:
            wanted = normalize_entity_name(normalize_group(judgement.same_as) or "")
            for group in existing:
                if group.normalized_name == wanted:
                    # Record the spelling so the next matter resolves for free.
                    group.aliases = sorted({*(group.aliases or []), key})
                    session.flush()
                    return group.name

    # The first spelling seen becomes the firm's name for the book, so prefer the
    # model's canonical form: otherwise whichever document happened to classify
    # first decides whether it is "Mergers & Acquisitions" or "mergers &
    # acquisitions" for the life of the corpus.
    display = normalized
    if judgement is not None and judgement.canonical_name:
        display = normalize_group(judgement.canonical_name) or normalized

    group = FirmPracticeGroup(
        name=display,
        normalized_name=key,
        aliases=[],
        provenance={"first_seen_as": raw, "evidence": evidence},
    )
    session.add(group)
    session.flush()
    return group.name


ROLE_PRECEDENCE = (
    "responsible partner",
    "relationship partner",
    "billing partner",
    "supervising partner",
    "lead partner",
    "partner",
)


def apply_client(session: Session, matter: Matter, profile: MatterProfile) -> str | None:
    """Record the one party the firm acts for, replacing whatever the documents accrued.

    Who the client is, is a property of the MATTER, and only the folder shows it: the
    engagement letter, the intake form, the invoices. The per-document extractor was
    asked it once per document instead, and any party a document called "the client"
    became a client of the matter -- so an LPA whose own client is someone else, a
    third-party fund under review, or a target in a purchase agreement each added one.
    Left per-document, a firm ends up with several times as many clients as it has,
    and most matters carrying more than one.

    Entity resolution already collapses spellings of a name; it cannot fix which of
    them is the client, because that is a role, not a name. So the
    profile decides it once and this writes exactly that -- the same replace-don't-add
    contract apply_team uses for the team.
    """
    raw = (profile.client_name or "").strip()
    if not raw:
        return None
    key = normalize_entity_name(raw)
    if not key:
        return None

    client = session.scalar(select(Client).where(Client.normalized_name == key))
    if client is None and session.bind is not None and session.bind.dialect.name == "postgresql":
        # The resolver's own alias list, so a client first seen here under a variant
        # still resolves to one row the next time a document spells it differently.
        # Raw containment, as in matter_search: the ORM's .contains() on a JSON column
        # compiles to a string LIKE, which matches nothing and errors on JSON input.
        client = session.scalar(
            select(Client)
            .where(text("clients.normalized_aliases @> cast(:alias as jsonb)"))
            .params(alias=json.dumps([key]))
        )
    if client is None:
        client = Client(name=raw, provenance={"decided_by": "matter_profile",
                                              "evidence": profile.client_evidence})
        session.add(client)
        session.flush()

    session.execute(delete(MatterClient).where(MatterClient.matter_id == matter.id))
    session.add(MatterClient(matter_id=matter.id, client_id=client.id))
    session.flush()
    return client.name


def resolve_team_groups(
    session: Session, config: AppConfig, profile: MatterProfile
) -> dict[str, str | None]:
    """Settle every practice group the profile names, before anything is written.

    resolve_group can call the model, and it can INSERT a new group. Doing both from
    inside apply_team meant a transaction that had already inserted `firm_people` or
    `firm_practice_groups` rows then sat on those locks for the length of another
    agent call. Two matters naming the same partner serialise on it, and at any real
    fan-out that becomes a convoy -- hundreds of transactions idle in transaction,
    holding entity locks for minutes, which stalls the per-document stages too because
    they write the same tables.

    Committing each group as it is settled keeps every INSERT..COMMIT window free of
    model calls, so the locks are held for milliseconds instead of minutes.
    """
    groups: dict[str, str | None] = {}
    evidence = profile.group_evidence
    for member in profile.team:
        raw = (member.practice_group or "").strip()
        if not raw or raw in groups:
            continue
        groups[raw] = resolve_group(session, config, raw, evidence=evidence)
        session.commit()
    return groups


def apply_team(
    session: Session,
    config: AppConfig,
    matter: Matter,
    profile: MatterProfile,
    groups: dict[str, str | None] | None = None,
) -> None:
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
    # Settled up front by resolve_team_groups so nothing below calls the model while
    # holding a write lock. Falling back keeps the CLI and tests working unchanged.
    if groups is None:
        groups = resolve_team_groups(session, config, profile)

    def group_for(member: TeamMember) -> str | None:
        raw = (member.practice_group or "").strip()
        return groups.get(raw) if raw else None

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
            person = FirmPerson(
                name=member.name.strip(),
                title=member.title,
                practice_group=group_for(member),
            )
            session.add(person)
            session.flush()
        else:
            # Later documents fill gaps but never overwrite: the first statement of
            # someone's group is as good as the fortieth, and churn here would move
            # every matter that partner owns.
            if person.title is None and member.title:
                person.title = member.title
            if person.practice_group is None and member.practice_group:
                person.practice_group = group_for(member)
        session.add(MatterTeam(matter_id=matter.id, person_id=person.id, role=role))
        rank = next(
            (i for i, r in enumerate(ROLE_PRECEDENCE) if r in role.lower()),
            len(ROLE_PRECEDENCE),
        )
        group = group_for(member) or person.practice_group
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
