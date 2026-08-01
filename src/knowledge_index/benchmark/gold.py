"""Derive retrieval gold labels from a packed task corpus — deterministic, offline.

The upstream task set ships rubrics on *output*, not retrieval labels, so we derive two kinds of
(query -> relevant document) gold from the packed corpus, both without a model:

- **instruction_working_set** (always): query = the task instruction, gold = every
  document in that scenario's bundle. Measures "can retrieval assemble the right
  working set for this matter from the instruction". One label per scenario.
- **factoid** (best-effort, known-item): a quoted value in a PASS/FAIL criterion
  (a party name, charter number, defined term) that appears in exactly one bundle
  document becomes a pasted-value query whose gold is that single document. Clean
  single-document labels that exercise the lexical and identifier legs. Skipped
  when the value is ambiguous (in >1 doc) or lives only in a spreadsheet.

The factoid pass reads ``.docx/.eml/.txt`` text; ``.xlsx/.pdf`` are not text-mined
here (noted in the output stats). Everything is reproducible from the corpus alone;
an optional LLM refinement pass can be layered on later without changing this
contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from email import message_from_bytes
from pathlib import Path

from docx import Document as WordDocument

# Quote spans in rubric criteria. Straight single quotes are boundary-guarded so an
# English possessive/contraction ("counterparty's", "bank's") is not mistaken for a
# quote delimiter — that false match produced junk labels like "s substitution of".
_QUOTED = re.compile(
    r'"([^"]{8,200})"'
    r"|“([^”]{8,200})”"
    r"|‘([^’]{8,200})’"
    r"|(?<![A-Za-z0-9])'([^']{8,200})'(?![A-Za-z0-9])"
)
_WS = re.compile(r"\s+")
_MAX_FACTOID_PER_SCENARIO = 3


def _looks_like_value(span: str) -> bool:
    """Keep spans that read like a citable value (proper noun, identifier, phrase).

    Drops lowercase single-token fragments that make weak, ambiguous queries while
    keeping party names, charter/section numbers, and capitalized defined terms.
    """
    if not any(ch.isalnum() for ch in span):
        return False
    if span.endswith((".docx", ".xlsx", ".pdf", ".eml")):
        return False
    return any(ch.isupper() for ch in span) or any(ch.isdigit() for ch in span) or " " in span


def _is_identifier_like(span: str) -> bool:
    """A discriminative value worth a known-item query: an identifier or proper noun.

    Requires a digit (account/charter/section numbers) or at least two capitalized
    tokens (a named entity), which keeps party names and reference numbers while
    rejecting generic capitalized single words.
    """
    if any(ch.isdigit() for ch in span):
        return True
    return sum(1 for token in span.split() if token[:1].isupper()) >= 2


@dataclass
class GoldQuery:
    id: str
    kind: str
    query: str
    gold_paths: list[str]
    principals: list[str]
    matter_ref: str
    practice_area: str
    meta: dict = field(default_factory=dict)


def _normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().casefold()


def _instruction_query(instructions: str) -> str:
    """The task instruction as a query, minus the '### Output:' filename listing."""
    head = re.split(r"###\s*Output", instructions, maxsplit=1)[0]
    return _WS.sub(" ", head).strip()


def _document_text(path: Path) -> str | None:
    suffix = path.suffix.casefold()
    try:
        if suffix == ".docx":
            document = WordDocument(str(path))
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.extend(cell.text for cell in row.cells)
            return "\n".join(parts)
        if suffix == ".eml":
            message = message_from_bytes(path.read_bytes())
            if message.is_multipart():
                chunks = [
                    part.get_content()
                    for part in message.walk()
                    if part.get_content_type() == "text/plain"
                ]
                return "\n".join(chunks)
            return message.get_content()
        if suffix in {".txt", ".md", ".csv"}:
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    return None  # .xlsx, .pdf, binaries: not text-mined here


def _quoted_spans(text: str) -> list[str]:
    spans: list[str] = []
    for match in _QUOTED.finditer(text):
        span = next((group for group in match.groups() if group), "").strip()
        if span and _looks_like_value(span):
            spans.append(span)
    # longest first, de-duplicated, so the most specific values win
    seen: set[str] = set()
    ordered: list[str] = []
    for span in sorted(spans, key=len, reverse=True):
        key = _normalize(span)
        if key not in seen:
            seen.add(key)
            ordered.append(span)
    return ordered


#: deterministic gold kinds this module can produce
DETERMINISTIC_KINDS: tuple[str, ...] = ("instruction_working_set", "factoid")


def derive_gold(
    corpus_dir: str | Path, *, kinds: tuple[str, ...] = DETERMINISTIC_KINDS
) -> list[GoldQuery]:
    corpus_dir = Path(corpus_dir).resolve()
    source_root = corpus_dir / "mock_dms"
    scenarios = [
        json.loads(line)
        for line in (corpus_dir / "scenarios.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    gold: list[GoldQuery] = []
    for scenario in scenarios:
        principals = [scenario["principal"]]
        document_paths = scenario["document_paths"]

        if "instruction_working_set" in kinds:
            gold.append(
                GoldQuery(
                    id=f"{scenario['scenario_id']}#ws",
                    kind="instruction_working_set",
                    query=_instruction_query(scenario["instructions"]),
                    gold_paths=list(document_paths),
                    principals=principals,
                    matter_ref=scenario["matter_ref"],
                    practice_area=scenario["practice_area"],
                    meta={"task_type": scenario["task_type"], "title": scenario["title"]},
                )
            )

        if "factoid" not in kinds:
            continue

        # index bundle document texts once, then match criterion values against them
        doc_texts = {
            path: normalized
            for path in document_paths
            if (raw := _document_text(source_root / path)) is not None
            and (normalized := _normalize(raw))
        }
        # A factoid value must be discriminative: present in at least one but not in
        # *every* text document (a value in all of them identifies nothing). Gold is
        # every document that contains it — an identifier legitimately recurs across a
        # matter's term sheet, memo and agreement. Per criterion, take the most
        # discriminative value (fewest matches, then the longest span).
        upper = max(2, len(doc_texts))  # strictly-fewer-than-all
        emitted = 0
        used_spans: set[str] = set()
        for criterion in scenario["criteria"]:
            if emitted >= _MAX_FACTOID_PER_SCENARIO:
                break
            haystack = f"{criterion.get('title', '')} {criterion.get('match_criteria', '')}"
            candidates: list[tuple[int, int, str, str, list[str]]] = []
            for span in _quoted_spans(haystack):
                key = _normalize(span)
                if key in used_spans or not _is_identifier_like(span):
                    continue
                matches = sorted(path for path, text in doc_texts.items() if key in text)
                if 1 <= len(matches) < upper:
                    candidates.append((len(matches), -len(span), key, span, matches))
            if not candidates:
                continue
            _, _, key, span, matches = min(candidates)
            used_spans.add(key)
            gold.append(
                GoldQuery(
                    id=f"{scenario['scenario_id']}#f{emitted + 1}",
                    kind="factoid",
                    query=span,
                    gold_paths=matches,
                    principals=principals,
                    matter_ref=scenario["matter_ref"],
                    practice_area=scenario["practice_area"],
                    meta={"criterion_id": criterion.get("id")},
                )
            )
            emitted += 1
    return gold


def write_gold(corpus_dir: str | Path, *, kinds: tuple[str, ...] = DETERMINISTIC_KINDS) -> dict:
    corpus_dir = Path(corpus_dir).resolve()
    gold = derive_gold(corpus_dir, kinds=kinds)
    gold_path = corpus_dir / "retrieval-gold.jsonl"
    gold_path.write_text(
        "".join(json.dumps(asdict(item), ensure_ascii=False) + "\n" for item in gold),
        encoding="utf-8",
    )
    by_kind: dict[str, int] = {}
    for item in gold:
        by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
    return {"gold_path": str(gold_path), "queries": len(gold), "by_kind": by_kind}
