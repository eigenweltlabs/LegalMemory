"""Controlled vocabularies from docs/src/content/docs/concepts/data-model.md.

Leaf ids are stable API: they appear in the database, in MCP tool filters, and in
LLM extraction prompts. All ids and labels are English; localized display names
are a UI concern. Firms extend these via config (custom entries land in the same
tables with a firm namespace prefix).
"""

from enum import StrEnum


class PracticeArea(StrEnum):
    CORPORATE_MA = "corporate_ma"
    COMMERCIAL = "commercial"
    LABOR = "labor"
    REAL_ESTATE = "real_estate"
    LITIGATION = "litigation"
    IP_IT = "ip_it"
    TAX = "tax"
    BANKING_FINANCE = "banking_finance"
    INSOLVENCY = "insolvency"
    PUBLIC = "public"
    CRIMINAL = "criminal"
    FAMILY_INHERITANCE = "family_inheritance"
    OTHER = "other"


class DocType(StrEnum):
    # contract
    PURCHASE_AGREEMENT = "purchase_agreement"
    SHARE_PURCHASE_AGREEMENT = "share_purchase_agreement"
    LEASE_AGREEMENT = "lease_agreement"
    EMPLOYMENT_AGREEMENT = "employment_agreement"
    MANAGING_DIRECTOR_AGREEMENT = "managing_director_agreement"
    NDA = "nda"
    ARTICLES_OF_ASSOCIATION = "articles_of_association"
    LOAN_AGREEMENT = "loan_agreement"
    LICENSE_AGREEMENT = "license_agreement"
    DATA_PROCESSING_AGREEMENT = "data_processing_agreement"
    AMENDMENT_AGREEMENT = "amendment_agreement"
    OTHER_CONTRACT = "other_contract"
    # pleading
    STATEMENT_OF_CLAIM = "statement_of_claim"
    STATEMENT_OF_DEFENSE = "statement_of_defense"
    APPEAL_BRIEF = "appeal_brief"
    OTHER_PLEADING = "other_pleading"
    MOTION = "motion"
    # court
    JUDGMENT = "judgment"
    COURT_ORDER = "court_order"
    COURT_DIRECTIVE = "court_directive"
    HEARING_MINUTES = "hearing_minutes"
    # correspondence
    EMAIL = "email"
    LETTER = "letter"
    SECURE_MAILBOX_MESSAGE = "secure_mailbox_message"
    CLIENT_MEMO = "client_memo"
    # internal
    INTERNAL_NOTE = "internal_note"
    LEGAL_OPINION = "legal_opinion"
    RESEARCH_MEMO = "research_memo"
    DUE_DILIGENCE_REPORT = "due_diligence_report"
    CHECKLIST = "checklist"
    NOTE = "note"
    # evidence
    COMMERCIAL_REGISTER_EXTRACT = "commercial_register_extract"
    LAND_REGISTER_EXTRACT = "land_register_extract"
    POWER_OF_ATTORNEY = "power_of_attorney"
    INVOICE = "invoice"
    EXTERNAL_EXPERT_REPORT = "external_expert_report"
    OTHER_ANNEX = "other_annex"
    # administration
    FEE_AGREEMENT = "fee_agreement"
    ENGAGEMENT_AGREEMENT = "engagement_agreement"
    DEADLINE_NOTE = "deadline_note"
    OTHER_ADMIN = "other_admin"


DOC_TYPE_GROUPS: dict[str, list[DocType]] = {
    "contract": [
        DocType.PURCHASE_AGREEMENT,
        DocType.SHARE_PURCHASE_AGREEMENT,
        DocType.LEASE_AGREEMENT,
        DocType.EMPLOYMENT_AGREEMENT,
        DocType.MANAGING_DIRECTOR_AGREEMENT,
        DocType.NDA,
        DocType.ARTICLES_OF_ASSOCIATION,
        DocType.LOAN_AGREEMENT,
        DocType.LICENSE_AGREEMENT,
        DocType.DATA_PROCESSING_AGREEMENT,
        DocType.AMENDMENT_AGREEMENT,
        DocType.OTHER_CONTRACT,
    ],
    "pleading": [
        DocType.STATEMENT_OF_CLAIM,
        DocType.STATEMENT_OF_DEFENSE,
        DocType.APPEAL_BRIEF,
        DocType.OTHER_PLEADING,
        DocType.MOTION,
    ],
    "court": [
        DocType.JUDGMENT,
        DocType.COURT_ORDER,
        DocType.COURT_DIRECTIVE,
        DocType.HEARING_MINUTES,
    ],
    "correspondence": [
        DocType.EMAIL,
        DocType.LETTER,
        DocType.SECURE_MAILBOX_MESSAGE,
        DocType.CLIENT_MEMO,
    ],
    "internal": [
        DocType.INTERNAL_NOTE,
        DocType.LEGAL_OPINION,
        DocType.RESEARCH_MEMO,
        DocType.DUE_DILIGENCE_REPORT,
        DocType.CHECKLIST,
        DocType.NOTE,
    ],
    "evidence": [
        DocType.COMMERCIAL_REGISTER_EXTRACT,
        DocType.LAND_REGISTER_EXTRACT,
        DocType.POWER_OF_ATTORNEY,
        DocType.INVOICE,
        DocType.EXTERNAL_EXPERT_REPORT,
        DocType.OTHER_ANNEX,
    ],
    "administration": [
        DocType.FEE_AGREEMENT,
        DocType.ENGAGEMENT_AGREEMENT,
        DocType.DEADLINE_NOTE,
        DocType.OTHER_ADMIN,
    ],
}

DOC_TYPE_GROUP_OF: dict[DocType, str] = {
    dt: group for group, members in DOC_TYPE_GROUPS.items() for dt in members
}


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


class MatterKind(StrEnum):
    TRANSACTION = "transaction"
    LITIGATION = "litigation"
    ADVISORY = "advisory"
    REGULATORY = "regulatory"
    INTERNAL = "internal"


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
    ANNEX_OF = "annex_of"
    REFERENCES = "references"
    AMENDS = "amends"
    SUPERSEDES = "supersedes"
    RESPONDS_TO = "responds_to"
    DUPLICATE_OF = "duplicate_of"
    NEAR_DUPLICATE_OF = "near_duplicate_of"
    BELONGS_TO_THREAD = "belongs_to_thread"
    WORK_PRODUCT_OF = "work_product_of"


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
