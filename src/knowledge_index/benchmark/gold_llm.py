"""LLM-assisted retrieval gold — propose questions, then verify them against source text.

The deterministic pass (``gold.py``) only catches values quoted verbatim in a
criterion, so it misses composed values ("… a Delaware limited liability company,
acting as Administrative Agent") and can't produce natural-language questions. This
adds two richer label kinds through the model gateway, each **propose-then-verify** so
a hallucinated label never enters the benchmark:

- ``llm_factoid`` — a specific value a criterion tests plus which bundle document
  states it, as a pasted-value known-item query.
- ``llm_question`` — a natural-language question a lawyer would ask whose answer lives
  in the bundle, testing *semantic* retrieval rather than lexical overlap.

Grounding is the point: the model proposes ``(query, answer, source document)``; we
accept the label only if the answer text actually appears in a bundle document, and
gold becomes exactly the documents that contain it (the model's document guess is a
hint, not trusted). Ungrounded proposals are dropped. Every accepted label carries
provenance (model + prompt version) per the ontology's "provenance on every inference"
rule and a ``needs_review`` flag so a human can spot-check before it gates anything.

This runs through the same LiteLLM gateway as every other model call, so an air-gapped
install generates gold with its local model. It needs only the gateway — no database,
no index.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from knowledge_index.benchmark.gold import GoldQuery, _document_text, _normalize
from knowledge_index.config import DEFAULT_LLM_ENV, AppConfig

PROMPT_VERSION = "llm-gold-1"
_MAX_DOC_CHARS = 4000  # per-document text budget in the prompt


class _Proposal(BaseModel):
    kind: Literal["llm_factoid", "llm_question"]
    query: str = Field(description="the pasted value (factoid) or the question")
    answer: str = Field(description="the exact value that must appear verbatim in a source doc")
    source_document: str = Field(description="filename of the document that states the answer")
    leg: Literal["identifier", "lexical", "semantic"]


class _Proposals(BaseModel):
    proposals: list[_Proposal]


_SYSTEM = (
    "You write test questions for a law firm's document assistant. Given a task and its "
    "source documents, you write questions a real lawyer at the firm would actually ask "
    "the assistant, whose answer is stated in exactly ONE of the documents. The 'answer' "
    "MUST appear verbatim (as a substring) in that document. Never invent values.\n\n"
    "Write the way a busy lawyer talks — short, natural, conversational. Reference the "
    "deal by the SHORTHAND a lawyer would use: a party name or short deal name (e.g. 'the "
    "Trellis deal', 'the Meridian ISDA') — NOT a recitation of full legal entity names "
    "and document types. Include just enough to point at one matter, no more.\n"
    "- GOOD: 'What cross-default threshold did we agree for Trellis on the Meridian "
    "ISDA?'\n"
    "- TOO ROBOTIC: 'In the Trellis Bank AG / Meridian Capital Partners LP matter, what "
    "Cross-Default Threshold does the draft ISDA schedule set for Trellis Bank AG?'\n"
    "- TOO VAGUE (unanswerable — which deal?): 'What is the cross-default threshold?'\n"
    "Never use document-internal placeholders like 'Party A', 'Party B', 'the draft'.\n\n"
    "Prefer questions with a concrete, distinctive answer (amounts, deadlines, dates). "
    "Use 'llm_question' for these natural questions."
)


def _build_user_prompt(scenario: dict, doc_texts: dict[str, str], per_scenario: int) -> str:
    listing = []
    for path, text in doc_texts.items():
        name = path.rsplit("/", 1)[-1]
        listing.append(f"### {name}\n{text[:_MAX_DOC_CHARS]}")
    criteria = "\n".join(
        f"- {c.get('title', '')}: {c.get('match_criteria', '')}"
        for c in scenario.get("criteria", [])[:20]
    )
    return (
        f"MATTER: {scenario.get('matter_ref', '')} — {scenario.get('matter_title', '')}\n"
        f"TASK: {scenario.get('title', '')}\n"
        f"INSTRUCTION: {scenario.get('instructions', '')[:1500]}\n\n"
        f"RUBRIC CRITERIA:\n{criteria}\n\n"
        f"SOURCE DOCUMENTS (filename + text excerpt):\n" + "\n\n".join(listing) + "\n\n"
        f"Produce up to {per_scenario} labels. Put the exact filename from the list in "
        f"'source_document'. 'answer' must be a verbatim substring of that document. "
        f"Phrase each question the way a lawyer would naturally ask — reference the deal "
        f"by a party-name shorthand, conversational, not the full formal names."
    )


def _ground_proposal(
    proposal: _Proposal,
    document_paths: list[str],
    doc_texts: dict[str, str],
    scenario: dict,
    model: str,
) -> GoldQuery | None:
    """Accept a proposal only if its answer really appears in a bundle document."""
    answer = _normalize(proposal.answer)
    if len(answer) < 4 or not proposal.query.strip():
        return None
    matches = sorted(path for path, text in doc_texts.items() if answer in text)
    if not matches:
        return None  # ungrounded / hallucinated — drop it
    # the model's document guess is only a hint; gold is what actually contains it
    return GoldQuery(
        id="",  # assigned by the caller once accepted
        kind=proposal.kind,
        query=proposal.query.strip(),
        gold_paths=matches,
        principals=[scenario["principal"]],
        matter_ref=scenario["matter_ref"],
        practice_area=scenario["practice_area"],
        meta={
            "answer": proposal.answer,
            "leg": proposal.leg,
            "source_hint": proposal.source_document,
            "extracted_by": f"{model}/{PROMPT_VERSION}",
            "needs_review": True,
        },
    )


def generate_llm_gold(
    corpus_dir: str | Path,
    config: AppConfig,
    *,
    per_scenario: int = 4,
    model: str = "",
    limit_scenarios: int | None = None,
) -> dict:
    """Generate, ground, and append LLM gold to ``retrieval-gold.jsonl``.

    Reuses whatever deterministic gold already exists; LLM labels are added with
    distinct ``kind`` values and deduplicated by (kind, normalized query) against the
    whole file, so this is safe to re-run.
    """
    from knowledge_index.pipeline.providers import chat_json

    corpus_dir = Path(corpus_dir).resolve()
    source_root = corpus_dir / "mock_dms"
    model = model or os.environ.get(DEFAULT_LLM_ENV, "")
    scenarios = [
        json.loads(line)
        for line in (corpus_dir / "scenarios.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if limit_scenarios is not None:
        scenarios = scenarios[:limit_scenarios]

    gold_path = corpus_dir / "retrieval-gold.jsonl"
    existing = (
        [json.loads(line) for line in gold_path.read_text(encoding="utf-8").splitlines() if line]
        if gold_path.exists()
        else []
    )
    seen = {(item["kind"], _normalize(item["query"])) for item in existing}

    accepted: list[GoldQuery] = []
    stats = {"scenarios": len(scenarios), "proposed": 0, "grounded": 0, "duplicate": 0, "errors": 0}
    for scenario in scenarios:
        doc_texts = {
            path: normalized
            for path in scenario["document_paths"]
            if (raw := _document_text(source_root / path)) is not None
            and (normalized := _normalize(raw))
        }
        if not doc_texts:
            continue
        try:
            result = chat_json(
                model,
                config,
                system=_SYSTEM,
                user=_build_user_prompt(scenario, doc_texts, per_scenario),
                schema=_Proposals,
                max_output_tokens=1500,
            )
        except Exception:  # gateway/schema failure on one scenario must not abort the run
            stats["errors"] += 1
            continue
        for index, proposal in enumerate(result.proposals[:per_scenario]):
            stats["proposed"] += 1
            grounded = _ground_proposal(
                proposal, scenario["document_paths"], doc_texts, scenario, model
            )
            if grounded is None:
                continue
            key = (grounded.kind, _normalize(grounded.query))
            if key in seen:
                stats["duplicate"] += 1
                continue
            seen.add(key)
            grounded.id = f"{scenario['scenario_id']}#{grounded.kind}-{index + 1}"
            accepted.append(grounded)
            stats["grounded"] += 1

    from dataclasses import asdict

    with gold_path.open("a", encoding="utf-8") as handle:
        for item in accepted:
            handle.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
    stats["gold_path"] = str(gold_path)
    stats["appended"] = len(accepted)
    return stats
