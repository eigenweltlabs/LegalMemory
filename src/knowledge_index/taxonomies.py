"""Controlled vocabularies from docs/ontology.md.

Leaf ids are stable API: they appear in the database, in MCP tool filters, and in
LLM extraction prompts. All ids and labels are English; localized display names
are a UI concern. Firms extend these via config (custom entries land in the same
tables with a firm namespace prefix).
"""

from enum import StrEnum


class TaskType(StrEnum):
    CONTRACT_DRAFTING = "contract_drafting"
    CONTRACT_REVIEW = "contract_review"
    NEGOTIATION = "negotiation"
    DUE_DILIGENCE = "due_diligence"
    LEGAL_OPINION = "legal_opinion"
    CLAIM_DRAFTING = "claim_drafting"
    DEFENSE_DRAFTING = "defense_drafting"
    LEGAL_RESEARCH = "legal_research"
    SUMMARIZATION = "summarization"
    LEGAL_TRANSLATION = "legal_translation"
    COMPLIANCE_REVIEW = "compliance_review"
    OTHER = "other"


class PartyRole(StrEnum):
    CLIENT = "client"
    OPPOSING_PARTY = "opposing_party"
    OPPOSING_COUNSEL = "opposing_counsel"
    COURT = "court"
    AUTHORITY = "authority"
    NOTARY = "notary"
    ADVISOR = "advisor"
    OTHER = "other"


class VersionStatus(StrEnum):
    DRAFT = "draft"
    FINAL = "final"
    EXECUTED = "executed"
    UNKNOWN = "unknown"


class RelationKind(StrEnum):
    # Only kinds the pipeline actually emits. Duplicates are handled by merging into a
    # shared version, not by an edge; near-duplicate detection and work_product_of edges
    # are not implemented (see docs/ontology.md).
    ANNEX_OF = "annex_of"
    REFERENCES = "references"
    AMENDS = "amends"
    SUPERSEDES = "supersedes"
    RESPONDS_TO = "responds_to"
    BELONGS_TO_THREAD = "belongs_to_thread"


class RationaleCategory(StrEnum):
    LEGAL_RISK = "legal_risk"
    MARKET_STANDARD = "market_standard"
    NEGOTIATION_CONCESSION = "negotiation_concession"
    REGULATORY_REQUIREMENT = "regulatory_requirement"
    DRAFTING_ERROR = "drafting_error"
    CLIENT_INSTRUCTION = "client_instruction"
    TACTICAL = "tactical"


class PipelineStage(StrEnum):
    FETCH = "fetch"
    CONVERT = "convert"
    CLASSIFY_MATTER = "classify_matter"
    RELATE = "relate"
    EXTRACT_METADATA = "extract_metadata"
    EXTRACT_DECISIONS = "extract_decisions"
    GEN_EVALS = "gen_evals"
    INDEX = "index"


# The insertion pipeline no longer runs GEN_EVALS inline. Building RL-environment
# candidates is a separate, human-in-the-loop flow (see pipeline/environments.py): it
# must be sparse, firm-work-product only, and partner-approved, none of which fits a
# per-document stage that fires on every final version. GEN_EVALS stays in the enum for
# the standalone builder and for back-compat, but not in the insertion DAG.
PIPELINE_STAGE_ORDER: list[PipelineStage] = [
    PipelineStage.FETCH,
    PipelineStage.CONVERT,
    PipelineStage.CLASSIFY_MATTER,
    PipelineStage.RELATE,
    PipelineStage.EXTRACT_METADATA,
    PipelineStage.EXTRACT_DECISIONS,
    PipelineStage.INDEX,
]


class EnvironmentStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETIRED = "retired"


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    SKIPPED = "skipped"


# Three different things are stored as ``skipped`` and only one of them is a decision the
# pipeline made about the document.
#
# A downstream stage is parked as ``skipped`` while its predecessor is unfinished, because
# ``pending`` would make the claim query hand it to a worker that cannot run it yet. That
# storage detail must not reach an operator: a dashboard showing every stage skipped for
# the whole corpus says the pipeline looked at the work and declined it. It is waiting, so
# it is reported as waiting.
#
# A stage switched off in config parks its rows the same way. Counting those as ``skipped``
# made the reverse error: a panel read "skipped by config: 45" while every stage was on,
# because 45 handler skips were sitting in that bucket — which is what taught an operator
# that the enabled toggle does nothing. The toggle's effect has to be countable on its own
# or it cannot be believed.
WAITING_FOR_PREVIOUS_STAGE = "waiting_for_previous_stage"
DISABLED_BY_CONFIGURATION = "disabled_by_configuration"
# A source ACL changed while the document bytes stayed identical. The index stage uses
# this durable marker to update only its denormalized access fields; parsing, model work,
# chunking and embeddings remain untouched.
ACCESS_ONLY_REINDEX = "access_only_reindex"
STAGE_BUCKET_WAITING = "waiting"
STAGE_BUCKET_DISABLED = "disabled"

# Every skip reason that is not the handler's own judgement about the document.
_SKIP_REASON_BUCKETS = {
    WAITING_FOR_PREVIOUS_STAGE: STAGE_BUCKET_WAITING,
    DISABLED_BY_CONFIGURATION: STAGE_BUCKET_DISABLED,
}


def stage_bucket(status: str, last_error: dict | None) -> str:
    """Reporting bucket for one ``processing_state`` row.

    Identical to the stored status except that a stage blocked on its predecessor and a
    stage switched off in config each get their own bucket, so neither is ever counted or
    displayed as a handler's "skipped".
    """
    if status == ProcessingStatus.SKIPPED.value:
        return _SKIP_REASON_BUCKETS.get((last_error or {}).get("reason"), status)
    return status
