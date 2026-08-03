"""LLM resolution of the ``(client, counterparty)`` for each firm-layout matter.

The deterministic guess in ``harvey_corpus._firm_parties`` reads the leading token of
each filename, which is noisy: document-type words ("security", "amortization",
"lender") masquerade as clients, and it can't tell *which* side the firm represents.
This resolves the parties with the model instead, in two stages:

1. **Per-scenario extraction** — the model reads the task instruction and document
   excerpts (the recitals name the parties) and returns the party the firm REPRESENTS
   (the client), the counterparty, full legal names, and short folder labels. Which
   side is the client is a reading task — "negotiate on behalf of the Lender", "protect
   our client" — exactly what filenames can't express.
2. **Global canonicalization** — one pass over every client name collected so variant
   spellings ("Meridian Capital Partners LP" vs "Meridian Capital") merge into one
   client folder while genuinely different entities that share a word stay apart.

Everything runs through the same LiteLLM gateway as the rest of the benchmark. A failed
call falls back to the deterministic guess for that scenario, so the corpus always
builds. This is a one-time corpus-generation step, not a query-time path.
"""

from __future__ import annotations

import os

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from knowledge_index.benchmark.gold import _document_text
from knowledge_index.benchmark.harvey_corpus import _firm_parties
from knowledge_index.config import AppConfig

if TYPE_CHECKING:
    from knowledge_index.benchmark.harvey_corpus import PartyResolver, _Scenario

PROMPT_VERSION = "firm-parties-1"
_MAX_DOCS = 4  # documents shown per scenario (recitals live in the first agreement/draft)
_MAX_DOC_CHARS = 1200  # per-document excerpt budget


class _PartyExtraction(BaseModel):
    client_full: str = Field(description="full legal name of the party THE FIRM REPRESENTS")
    client_short: str = Field(description="short folder label, e.g. 'Meridian' or 'BNP Paribas'")
    counterparty_full: str = Field(description="full legal name of the other side")
    counterparty_short: str = Field(description="short folder label for the counterparty")


class _Canonical(BaseModel):
    name: str = Field(description="one client name exactly as given in the input list")
    canonical: str = Field(description="the canonical short folder label to use for it")


class _CanonicalMap(BaseModel):
    mappings: list[_Canonical]


_EXTRACT_SYSTEM = (
    "You identify the parties in a law firm's matter. Given a legal task and excerpts of "
    "its documents, determine which party THE FIRM REPRESENTS (the client) and which is "
    "the counterparty.\n\n"
    "The client is the side the work is done FOR: the party whose playbook the firm "
    "holds, whose position the instruction advocates ('negotiate on behalf of the "
    "Lender', 'protect our client'), or for whom a draft/redline is prepared. The "
    "counterparty is the other side of the deal.\n"
    "Return each party's full legal name AND a short, distinctive folder label — the "
    "memorable part a lawyer would say ('Meridian' for 'Meridian Capital Partners LP', "
    "'BNP Paribas' for 'BNP Paribas S.A.'). Do NOT use document-type or role words "
    "('Lender', 'Agent', 'Borrower', 'Issuer') as a label — use the entity's actual "
    "name. If a side genuinely cannot be identified, use 'Unknown'."
)

_CANON_SYSTEM = (
    "You are consolidating client names from a law firm's matters into canonical clients. "
    "Names that refer to the SAME entity — variant spellings, added/removed legal suffixes "
    "(LP, LLC, AG, N.A., S.A., Ltd), abbreviations — must map to ONE canonical short label. "
    "Names that refer to DIFFERENT entities must stay separate even when they share a word "
    "('Meridian Capital Partners' and 'Meridian Bank' are different clients). For every "
    "input name, return the canonical short folder label to use (the distinctive part, no "
    "legal suffix). Reuse the exact same canonical label for all variants of one entity."
)


def _scenario_excerpts(scenario: _Scenario) -> str:
    parts: list[str] = []
    for document in scenario.documents:
        if len(parts) >= _MAX_DOCS:
            break
        text = _document_text(document)
        if not text or not text.strip():
            continue  # .xlsx/.pdf carry no minable recitals here
        parts.append(f"### {document.name}\n{text.strip()[:_MAX_DOC_CHARS]}")
    return "\n\n".join(parts)


def _extract_prompt(scenario: _Scenario) -> str:
    import json

    task = json.loads(scenario.task_json.read_text(encoding="utf-8"))
    return (
        f"TASK: {task.get('title', '')}\n"
        f"TASK TYPE: {scenario.task_type}\n"
        f"INSTRUCTION: {task.get('instructions', '')[:1500]}\n\n"
        f"DOCUMENTS (filename + excerpt):\n{_scenario_excerpts(scenario)}"
    )


def _clean_label(name: str) -> str:
    """A folder-safe display label; empty/unknown collapse to the deterministic sentinel."""
    label = name.replace("/", "-").strip()
    if not label or label.casefold() in {"unknown", "n/a", "none", "unassigned"}:
        return ""
    return label


def resolve_parties_llm(
    scenarios: list[_Scenario],
    config: AppConfig,
    *,
    model_name: str | None = None,
) -> list[tuple[str, str]]:
    """Return an aligned ``[(client, counterparty), ...]`` for ``scenarios`` via the model.

    Stage 1 extracts each matter's parties; stage 2 canonicalizes the client names so
    variants collapse to one folder. Any scenario whose extraction fails falls back to the
    deterministic filename guess, so the returned list always matches ``scenarios`` in
    length and the corpus still builds.
    """
    from knowledge_index.pipeline.providers import chat_json

    model = model_name or os.environ.get("KI_LLM_MODEL", "")

    # stage 1 — per-scenario extraction, deterministic fallback on any failure
    clients_full: list[str] = []
    clients_short: list[str] = []
    counterparties: list[str] = []
    for scenario in scenarios:
        det_client, det_counterparty = _firm_parties([d.name for d in scenario.documents])
        try:
            extracted = chat_json(
                model,
                config,
                system=_EXTRACT_SYSTEM,
                user=_extract_prompt(scenario),
                schema=_PartyExtraction,
                max_output_tokens=2000,
            )
        except Exception:  # one bad scenario must not abort corpus generation
            clients_full.append(det_client)
            clients_short.append(det_client)
            counterparties.append(det_counterparty)
            continue
        client_short = _clean_label(extracted.client_short) or det_client
        clients_full.append(_clean_label(extracted.client_full) or client_short)
        clients_short.append(client_short)
        counterparties.append(_clean_label(extracted.counterparty_short) or det_counterparty)

    # stage 2 — one global pass to merge client-name variants into canonical folders
    canonical = _canonicalize(clients_full, config, model, chat_json)

    clients = [
        canonical.get(full) or short
        for full, short in zip(clients_full, clients_short, strict=True)
    ]
    return list(zip(clients, counterparties, strict=True))


def _canonicalize(
    names: list[str], config: AppConfig, model: str, chat_json
) -> dict[str, str]:
    """Map each distinct client name to a canonical short label (best-effort, one call)."""
    unique = sorted({n for n in names if n})
    if len(unique) < 2:
        return {}
    listing = "\n".join(f"- {name}" for name in unique)
    try:
        result = chat_json(
            model,
            config,
            system=_CANON_SYSTEM,
            user=f"Client names:\n{listing}",
            schema=_CanonicalMap,
            max_output_tokens=4000,
        )
    except Exception:  # canonicalization is a refinement; fall back to stage-1 shorts
        return {}
    return {
        m.name: label
        for m in result.mappings
        if m.name in set(unique) and (label := _clean_label(m.canonical))
    }


def make_llm_party_resolver(
    config: AppConfig, *, model_name: str | None = None
) -> PartyResolver:
    """Bind ``config`` into a :data:`~harvey_corpus.PartyResolver` for the firm builder."""

    def resolver(scenarios: list[_Scenario]) -> list[tuple[str, str]]:
        return resolve_parties_llm(scenarios, config, model_name=model_name)

    return resolver
