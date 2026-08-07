"""Stage schemas and prompts for model-backed extraction.

Every schema is enforced by pydantic in providers.chat_json; enum fields are
re-validated against the taxonomies so a hallucinated label quarantines the
document instead of polluting the knowledge layer. Document typing is NOT an
enum: the metadata agent walks the deployment's ontology (knowledge_index.
ontology) with deterministic tools and may only submit a node id it visited.
Instructions are English; free-text outputs (titles, summaries, rationales)
stay in the document's language so firm knowledge reads naturally.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from knowledge_index.taxonomies import PartyRole

from knowledge_index.taxonomies import RationaleCategory, TaskType


class MatterClassification(BaseModel):
    matter_id: str | None = Field(
        default=None,
        description="The id of the matter this document belongs to — copied EXACTLY from a "
        "search_matters, peek_matter, or create_matter result in this conversation. null "
        "only when no assignment is possible.",
    )
    matter_ref: str | None = Field(
        description="The matter's reference number, e.g. 'M-2026-0042'. Prefer a reference "
        "from the known-matters list; otherwise the one found in the document or path; "
        "null only if no assignment is possible."
    )
    matter_title: str | None = Field(
        description="Short matter title in the document's language, "
        "e.g. 'Projekt Falke — Unternehmenskauf'."
    )
    logical_title: str = Field(
        description="Normalized logical document title without version/status suffixes "
        "(e.g. the contract's own title instead of 'contract_final_v3(2)'). Drafts, "
        "redlines and finals of the same document MUST share one logical title. If the "
        "document is a version of an already-known document, copy that title EXACTLY "
        "as it appeared in a tool result you saw (peek_matter lists a matter's "
        "document titles). Keep the document's language."
    )
    is_new_matter: bool = Field(
        default=False,
        description="True if this document started a NEW matter you created via create_matter; "
        "False if it joins an existing matter you found via search_matters. When False, "
        "matter_ref MUST be a reference number from a search result, copied exactly.",
    )
    candidates_considered: list[str] = Field(
        default_factory=list,
        description="Reference numbers of the existing matters you inspected before deciding.",
    )
    practice_area_node: str | None = Field(
        default=None,
        description="The Area of Law node id from the menu that best describes this "
        "matter's practice area. null when unclear — never guess.",
    )
    matter_kind_node: str | None = Field(
        default=None,
        description="The Service node id from the kind-of-engagement menu describing "
        "what the firm is DOING in this matter. null when unclear — never guess.",
    )
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(description="One sentence naming the decisive evidence.")


class NotableClause(BaseModel):
    locus: str = Field(description="Clause heading/reference, e.g. '§ 9 Haftung'.")
    text: str = Field(description="The clause's own text, verbatim.")
    clause_type_node: str | None = Field(
        default=None,
        description="The clause-type ontology node id for what this clause IS — must "
        "be an id you saw in a clause_search result in this conversation. null when "
        "no listed clause type describes it.",
    )


class TypedIdentifier(BaseModel):
    scheme: str = Field(
        description="Identifier scheme id, e.g. de_hrb (Handelsregister), lei, ust_id, "
        "tax_id, or another well-known register scheme."
    )
    value: str = Field(description="The identifier value, verbatim from the document.")


class ExtractedParty(BaseModel):
    """One party named on the document, resolved to a firm-wide entity by the agent.

    The agent decides link-vs-create with the search_entities tool: it searches the
    firm's known parties/clients and, only when a candidate is genuinely the SAME
    real-world entity, reuses its id — a matching name alone is never enough.
    """

    name: str = Field(
        description="The party's canonical legal name only — not its address/seat, register "
        "number, or defined-term label. Register numbers/LEI belong in identifiers. Write the "
        "same entity identically everywhere so mentions across documents match."
    )
    role: PartyRole = Field(description="This party's role on THIS document.")
    existing_id: str | None = Field(
        default=None,
        description="The id of an existing entity returned by search_entities, when this "
        "party IS that already-known entity. null to create a new one. Reuse an id ONLY "
        "when identifiers or the matter context confirm it — never on name similarity alone. "
        "null is only accepted for a party you actually searched with search_entities.",
    )
    kind: str = Field(
        default="legal_entity",
        description="legal_entity for a company/authority/court, natural_person for a person.",
    )
    identifiers: list[TypedIdentifier] = Field(
        default_factory=list,
        description="Register numbers/LEI/tax ids printed for THIS party, if any.",
    )


class DocumentMetadata(BaseModel):
    type_node: str | None = Field(
        default=None,
        description="The ontology node id that best describes what this document IS — "
        "must be an id you saw in an ontology tool result in this conversation. An "
        "interior node is correct when no child fits better. null when NOTHING in the "
        "ontology describes this kind of document — an honest 'no type' beats a wrong "
        "or meaninglessly broad one.",
    )
    language: str = Field(pattern="^(de|en|mixed)$")
    doc_date: str | None = Field(
        description="The date OF the document (signing date, letter date) as YYYY-MM-DD, "
        "taken from the content — not the file date. null if not determinable."
    )
    title: str = Field(description="Normalized document title in the document's language.")
    parties: list[ExtractedParty] = Field(
        default_factory=list,
        description="Every party named on the face of the document, each resolved to a "
        "firm-wide entity via search_entities (see the party-resolution protocol).",
    )
    identifiers: list[str] = Field(
        default_factory=list,
        description="Formal reference codes the document carries, assigned by a court, a "
        "public register, a statute, or the firm's matter system — for example a case or "
        "docket number, a company- or commercial-register number, a statute or regulation "
        "citation, or a matter/file reference. Copy them verbatim and deduplicate. NOT "
        "monetary amounts, dates, percentages or rates, party or company names, or document "
        "titles. Most documents carry few or none; an empty list is normal — never invent an "
        "identifier from an amount, a date, or a name.",
    )
    notable_clauses: list[NotableClause] = Field(
        default_factory=list,
        description="The individually significant clauses/sections of the document "
        "(e.g. liability, warranties, non-compete, purchase price). For each: the "
        "locus/heading and the clause's own text, verbatim from the document. Empty "
        "for documents that have no clause structure (emails, short notes). At most 15.",
    )
    confidence: float = Field(ge=0, le=1)


class DecisionExtraction(BaseModel):
    has_decision: bool = Field(
        description="true only if the changes carry a substantive decision with "
        "recognizable reasoning."
    )
    locus: str | None = Field(
        description="Clause/location, e.g. '§ 9 Abs. 2 Haftung', in the document's language."
    )
    change_summary: str | None = Field(
        description="What changed (one or two sentences, document's language)."
    )
    rationale_category: str | None = Field(description="Category id from the taxonomy.")
    rationale_text: str | None = Field(
        description="ANONYMIZED rationale in the document's language: replace party names "
        "with roles (e.g. 'the seller', 'the client'), no person names, no concrete amounts."
    )
    generalizable: bool = Field(
        default=False,
        description="false for matter-specific instructions or tactics; true if the "
        "rationale is reusable as firm knowledge.",
    )
    confidence: float = Field(default=0.0, ge=0, le=1)


class RubricItem(BaseModel):
    id: str
    criterion: str = Field(
        description="A SPECIFIC, checkable requirement — never a vague quality. State the exact "
        "thing: a named clause, a statute, a number, a party, a deadline, a computed value. "
        "'Cites § 433 BGB and § 280 BGB', not 'cites the right law'. 'Caps liability at the "
        "purchase price', not 'handles liability well'."
    )
    description: str = Field(
        description="How to check it against the gold output: the exact condition that scores "
        "full marks, stated so two graders would agree."
    )
    weight: float = Field(gt=0, le=1)
    kind: str = Field(
        pattern="^(binary|scale_1_5|negative)$",
        description="binary / scale_1_5 for positive criteria; 'negative' for pitfalls "
        "(hallucination, wrong position, wrong tone) that COST points — resists reward hacking.",
    )
    essential: bool = Field(
        default=False,
        description="true if omitting this makes the whole answer unacceptable, "
        "regardless of the other criteria.",
    )
    verifiable: bool = Field(
        default=False,
        description="true if this can be scored DETERMINISTICALLY against the gold output or the "
        "corpus (a specific string, citation, number, date is present or matches) WITHOUT a "
        "subjective judge call. Prefer verifiable criteria.",
    )
    gold_evidence: str | None = Field(
        default=None,
        description="The exact, quantified evidence in the GOLD output that satisfies this "
        "criterion — a short quote or precise reference (e.g. '§ 9.2: Haftung auf Kaufpreis "
        "begrenzt'). This is what makes the reward verifiable: the grader checks the answer "
        "against THIS, not against a vague notion of quality.",
    )


class EnvVerifier(BaseModel):
    check: str = Field(description="Short id of an objective, machine-checkable requirement.")
    detail: str = Field(description="What must be objectively true (e.g. '§ 433 BGB is cited').")
    expected: str | None = Field(
        default=None,
        description="The expected value read from the gold output (the citation, number, party, "
        "or date the answer must contain), so the check is a concrete assertion.",
    )


class EvalGeneration(BaseModel):
    authored_internally: bool = Field(
        description="true ONLY if THIS FIRM produced this document as its own work product "
        "(a contract it drafted, a pleading it filed, an opinion it wrote). False for inbound "
        "documents: opposing-counsel drafts, court judgments/orders, third-party expert reports, "
        "register extracts. Only firm work product may become a benchmark."
    )
    eligible: bool = Field(
        description="true only if the document is a completed piece of legal work product "
        "from which a meaningful, self-contained benchmark task can be derived."
    )
    task_type: str | None = Field(description="Task type id from the taxonomy.")
    instruction: str | None = Field(
        description="Reconstructed task statement as a partner would have phrased it, "
        "in the document's language."
    )
    rubric: list[RubricItem] = Field(
        default_factory=list,
        description="3 to 6 weighted criteria derived from what the final version actually "
        "does (clauses present, positions taken, formalities met). Mark the load-bearing ones "
        "essential; add at least one 'negative' pitfall criterion.",
    )
    verifiers: list[EnvVerifier] = Field(
        default_factory=list,
        description="Objective, machine-checkable requirements (a specific statute cited, a "
        "deadline computed, a party named) — scored before the rubric.",
    )
    confidence: float = Field(default=0.0, ge=0, le=1)


class FileRelation(BaseModel):
    target_ref: str = Field(
        description="Source-object ref returned by open_file; never invent a ref from a path."
    )
    direction: str = Field(
        pattern="^(current_to_target|target_to_current)$",
        description="Direction of the relation relative to the current input file.",
    )
    kind: str = Field(
        pattern="^(annex_of|responds_to|references|amends)$",
        description="annex_of | responds_to | references | amends",
    )
    evidence: str


class FileRelationResult(BaseModel):
    logical_title: str = Field(
        description="Normalized title of this file's logical document, without version suffixes."
    )
    status: str = Field(description="draft | final | executed | unknown")
    identity: str = Field(
        pattern="^(new_document|new_version|duplicate)$",
        description="How the current file relates to an existing file that was opened.",
    )
    same_document_ref: str | None = Field(
        default=None,
        description="For new_version, one opened file ref belonging to the same logical document.",
    )
    duplicate_of: str | None = Field(
        default=None,
        description="For duplicate, the opened source-object ref with the same semantic content.",
    )
    relative_order: str = Field(
        default="unknown",
        pattern="^(before|after|same|unknown)$",
        description="Current file's order relative to same_document_ref.",
    )
    redline_of: str | None = Field(
        default=None, description="Opened source-object ref that this current file marks up."
    )
    redline_by: str | None = Field(
        default=None,
        description="Opened source-object ref that is a tracked-changes markup OF the "
        "current file (the reverse of redline_of, for when the current file is the base).",
    )
    relations: list[FileRelation] = Field(default_factory=list)
    thread_subject: str | None = Field(
        default=None, description="Normalized email thread subject, if the current file is email."
    )
    thread_member_refs: list[str] = Field(
        default_factory=list,
        description="Other opened email refs belonging to the current file's thread.",
    )
    confidence: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def require_identity_evidence(self) -> FileRelationResult:
        if self.identity == "new_version" and not self.same_document_ref:
            raise ValueError("same_document_ref is required for a new_version")
        if self.identity == "duplicate" and not self.duplicate_of:
            raise ValueError("duplicate_of is required for a duplicate")
        return self


class InvoiceLine(BaseModel):
    line_item_number: int
    line_type: str | None = Field(default=None, description="F/E/IF/IE (fee or expense line).")
    date: str | None = Field(default=None, description="ISO date of the line, if present.")
    timekeeper_id: str | None = Field(default=None, description="Firm timekeeper id, if shown.")
    timekeeper_name: str | None = None
    task_code: str | None = Field(default=None, description="UTBMS task code, e.g. L110.")
    activity_code: str | None = Field(default=None, description="UTBMS activity code, e.g. A101.")
    expense_code: str | None = Field(default=None, description="UTBMS expense code, e.g. E101.")
    units: float | None = Field(default=None, description="Hours worked or quantity.")
    unit_cost: float | None = Field(default=None, description="Rate per unit.")
    line_total: float | None = None
    description: str | None = None


class BillingExtraction(BaseModel):
    is_invoice: bool = Field(
        description="true ONLY if this document is an actual law-firm invoice or fee statement "
        "with billable line items — not an engagement letter or a fee agreement."
    )
    invoice_number: str | None = None
    invoice_date: str | None = Field(default=None, description="ISO date.")
    currency: str | None = Field(default=None, description="ISO 4217 currency, e.g. EUR.")
    invoice_total: float | None = None
    tax_total: float | None = None
    client_name: str | None = None
    lines: list[InvoiceLine] = Field(default_factory=list)


BILLING_SYSTEM = (
    "You extract structured billing data from a law-firm invoice, aligned to the LEDES / UTBMS "
    "standard. Set is_invoice=false unless the document is a real invoice with billable line "
    "items. For each line, capture the timekeeper, UTBMS task (L/C/P codes), activity (A codes) "
    "and expense (E codes) where present, the units (hours), the rate, and the amount. Read "
    "amounts and dates exactly; do not invent codes that are not in the document."
)


TASK_TYPES = ", ".join(item.value for item in TaskType)
RATIONALE_CATEGORIES = ", ".join(item.value for item in RationaleCategory)

CLASSIFY_SYSTEM = (
    "You assign a law-firm document to its matter (the firm's file). A firm has hundreds to "
    "thousands of matters, so you are NOT given the full list — you must SEARCH for the right "
    "one. You have tools:\n"
    "- search_matters(query): find existing matters by party, reference number (Aktenzeichen), "
    "title, or topic. Search with the reference number and parties you read in the document, "
    "and with the matter name from the folder path. It returns one PAGE of candidates plus a "
    "page block: page.has_more=true means more candidates exist, so a full page of near "
    "misses is not proof the matter is absent — page with offset=page.next_offset, or search "
    "a different term, before you conclude there is no matter for this document.\n"
    "- list_folder(): see this document's own folder plus two levels up and down, so you can "
    "tell which matter its neighbours belong to.\n"
    "- peek_matter(matter_id): inspect a candidate matter's details before committing.\n"
    "- create_matter(reference_number, title): create the matter when your searches found no "
    "existing one. Other documents of the same matter classify in parallel and only see this "
    "matter once it is created — so the moment your searches come up empty, call create_matter "
    "as your IMMEDIATELY NEXT action; never search, then read around, then create. It is "
    "get-or-create: an already-taken reference number returns the existing matter "
    "(created=false) instead of a duplicate — then join that one.\n\n"
    "Reason from the FOLDER STRUCTURE and the DOCUMENT CONTENTS together: firms file by matter, "
    "so a document usually belongs to the matter of its folder — but a document can be misfiled, "
    "in which case its contents (matter reference, parties, subject) decide. Prefer joining an "
    "EXISTING matter you found by search over creating a new one; only call create_matter "
    "when no existing matter fits. Submit the chosen matter's id (from a search_matters, "
    "peek_matter, or create_matter result) in matter_id, and copy its reference number "
    "EXACTLY into matter_ref. Documents may be in German or English."
)

MATTER_KIND_INSTRUCTION = (
    "\n\nAlso choose the matter's KIND of service (matter_kind_node) with the "
    "service_* tools: what the firm is DOING in this matter — the service "
    "performed, never its subject or industry, which belong in the area of law. "
    "Search or browse from service_roots, then VERIFY the candidate with "
    "service_node and judge by its DEFINITION, never by label similarity — a "
    "near-sounding label with a non-matching definition is wrong. When no deeper "
    "node's definition matches the work exactly, the broader node IS the answer: "
    "the facet does not cover every specialty, and a correct broad node always "
    "beats a wrong specific one. Any non-null matter_kind_node must be an id that "
    "appeared in a service_* tool result. null when genuinely unclear — never "
    "guess.\n"
)

AREA_OF_LAW_INSTRUCTION = (
    "\n\nAdditionally, choose the matter's AREA OF LAW: pick the id from the menu below "
    "that best describes the practice area of the whole matter (not just this document) "
    "and put it in practice_area_node. Prefer the most specific fitting entry; null when "
    "genuinely unclear — never guess.\n"
)

FILE_RELATION_SYSTEM = (
    "You relate ONE newly arriving law-firm file to files already visible in its filing "
    "neighbourhood or corpus. You receive the current file's exact path and content, plus a "
    "complete directory listing for one folder level above and below its own folder. Do not "
    "assume that location determines the relation: files can be misfiled. Use open_file(path) "
    "to read any listed file needed to establish identity, version order, duplication, an "
    "annex/main-document link, a response, reference, amendment, or email thread. Sibling "
    "folders appear name-only: call list_folder(path) to see inside any folder — a file that "
    "transmits, answers, marks up, or annexes something often has its counterpart one folder "
    "over (Drafts/, Correspondence/, Reference/). Use search_documents(query) only when the "
    "current file refers to something outside the listed neighbourhood, then open the returned "
    "path before linking it. Never emit a target ref you did not receive from open_file.\n\n"
    "Classify only the CURRENT file. identity=new_document unless an opened file proves it is a "
    "duplicate or another version of the same work product. Emails and letters are NEVER "
    "versions of each other: each email is its own document with status final — connect a "
    "reply to what it answers with responds_to and the thread fields, not as a version. For a "
    "new_version, same_document_ref names one opened version and relative_order says whether "
    "the current file is before or after it. For a duplicate, duplicate_of is required. "
    "Relations may only use annex_of (an annex or exhibit -> its main document), responds_to "
    "(a reply -> what it answers), references (a document -> another it cites by name), or "
    "amends (an amendment agreement -> the contract it amends), and must be supported by "
    "content.\n\n"
    "TRACKED CHANGES: the current file and open_file results carry tracked_changes "
    "{count, changes: [{kind: ins/del, text, author, date}]} extracted from the raw "
    "document. All text you see is the ACCEPTED view — deleted text appears ONLY in "
    "this digest, so the digest is the one reliable way to recognize a markup. A file "
    "with tracked changes is almost always a redline of a neighbouring version: find "
    "its base by matching the changed text against the neighbours' text and set "
    "redline_of (such a file is also a new_version of that base, not a new_document). "
    "Conversely, if an OPENED file's tracked changes mark up the current file, set "
    "redline_by to that ref."
)

METADATA_SYSTEM = (
    "You extract metadata from ONE law-firm document and classify its type against "
    "the deployment's document-type ontology with tools:\n"
    "- ontology_search(query): find candidate nodes anywhere in the tree by label, "
    "synonym, or definition (deterministic).\n"
    "- ontology_roots(): the top-level branches.\n"
    "- ontology_children(node_id): a node's children, with definitions.\n"
    "- ontology_node(node_id): full detail for one node, including its path.\n"
    "- clause_search(query): find CLAUSE-TYPE nodes (a separate, flat vocabulary) "
    "by the clause's conventional English name.\n\n"
    "Classification protocol:\n"
    "1. Read the document first and decide what it IS — its FORM — never its subject "
    "matter. A document that discusses, transmits, negotiates, or summarizes another "
    "kind of document does not take that document's type. When both a node for the "
    "form and a node for the function, purpose, or content could apply, the form wins.\n"
    "2. SEARCH for that form with ontology_search; try a different phrasing if the "
    "first query surfaces nothing fitting. Browse the roots and their children when "
    "search does not settle it.\n"
    "3. VERIFY before submitting: open the best candidates with ontology_node and "
    "check their definitions, parents, and siblings (ontology_children on the parent) "
    "— a sibling or parent may fit better. Judge by definitions, not label similarity.\n"
    "4. Stop at the most specific node that truly describes the document. An INTERIOR "
    "node is the correct answer when none of its children fits — never force a deeper "
    "choice, and never settle for a wrong sibling.\n"
    "5. If the ontology has no node for this document's FORM, submit type_node: null "
    "— never substitute a node describing its function, topic, or the transaction it "
    "belongs to, and do not park it at a broad root. Untyped is an honest, expected "
    "outcome.\n"
    "6. Any non-null type_node must be an id that appeared in a tool result in this "
    "conversation.\n\n"
    "Metadata rules: use only evidence from the document itself. "
    "(1) A document keeps its own identity: an annex, exhibit, or attachment gets its "
    "own type, never the type of the document it belongs to or travels with. "
    "(2) identifiers are formal reference codes the document carries — assigned by a court, "
    "a public register, a statute, or a matter system (a case or docket number, a register "
    "number, a statute or regulation citation, a matter/file reference), copied verbatim. "
    "They are NEVER monetary amounts, dates, percentages or rates, party or company names, "
    "or document titles; those belong to other fields or nowhere. Most documents carry few "
    "or none, so an empty list is the normal, correct outcome — never invent one from an "
    "amount, a date, or a name. "
    "(3) notable_clauses ONLY when the supplied version_status is final or executed AND "
    "the document has clause structure; otherwise return an empty list. "
    "(4) For each notable clause, look up its type with clause_search using the "
    "clause's single conventional English name (e.g. 'governing law', 'force "
    "majeure'); set clause_type_node to an id from the results only when its label "
    "truly names what the clause IS, else null — the clause vocabulary is "
    "incomplete and an untyped clause is an honest outcome.\n\n"
    "Party resolution — for every party named on the face of the document (the client "
    "the firm acts for, the contracting parties and counterparties, guarantors and "
    "agents, the court or authority, notaries, and any advising law firms):\n"
    "(a) Give its role on THIS document, and its CANONICAL legal name only — strip the "
    "address/seat, register text, and defined-term labels so the same entity reads "
    "identically everywhere.\n"
    "(b) Call search_entities for EVERY party — with its name (and any register "
    "number/LEI) — BEFORE deciding whether it is new. If a returned candidate is the "
    "SAME real-world entity, put its id in existing_id; a matching NAME ALONE is never "
    "enough — different companies share names, so reuse an id only when identifiers or "
    "the matter context agree. Otherwise leave existing_id null to create a new entity.\n"
    "(c) Any non-null existing_id MUST be an id returned by search_entities in this "
    "conversation, and a null existing_id is only accepted for a party you actually "
    "searched."
)

DECISION_SYSTEM = (
    "You analyze changes (tracked-changes redlines, comments) between contract versions "
    "at a law firm and extract the decision behind them as anonymized firm knowledge. "
    f"Categories: {RATIONALE_CATEGORIES}. "
    "client_instruction and tactical are not generalizable."
)

EVAL_SYSTEM = (
    "You propose a candidate RL-environment (an internal benchmark) "
    "from ONE completed piece of law-firm work: a Legal Task, the Input files needed "
    "to do it, and Criteria to score an answer. FIRST decide authored_internally: only THIS "
    "firm's own work product qualifies (a contract it drafted, a pleading it filed, an opinion "
    "it wrote) — NOT opposing-counsel drafts, court judgments/orders, third-party expert "
    "reports or register extracts, which encode someone else's work, not this firm's taste. If "
    "it is not firm work product, set authored_internally=false and eligible=false.\n\n"
    "You are given the GOLD output (the final version). Because you know the gold, every "
    "criterion MUST be VERIFIABLE and QUANTIFIED, not vague: state the exact thing an answer "
    "must contain and quote the gold_evidence that satisfies it. Write 'Cites § 433 and § 280 "
    "BGB' with gold_evidence '§ 433 BGB, § 280 BGB', not 'cites the right law'. Write 'Caps "
    "liability at the purchase price' with gold_evidence '§ 9.2: Haftung auf den Kaufpreis "
    "begrenzt', not 'handles liability well'. Set verifiable=true whenever the criterion can be "
    "checked deterministically against the gold (a specific citation, number, party, date, "
    "clause is present or matches). Reserve judge-graded (verifiable=false) criteria for genuine "
    "quality that cannot be checked mechanically, and keep them minimal.\n\n"
    "Derive 3-6 weighted criteria from what the final version actually does; mark the "
    "load-bearing ones essential; include at least one 'negative' pitfall (a hallucination or "
    "wrong position that costs points). Add objective verifiers with the expected value read "
    "from the gold. Be demanding: propose only tasks that genuinely capture the firm's judgment. "
    f"Task types: {TASK_TYPES}."
)
