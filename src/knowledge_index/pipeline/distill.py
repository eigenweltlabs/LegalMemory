"""Signal-dense ingestion helpers (retrieval plan 07, Phase 2).

Pure, deterministic string transforms — no regex, no LLM calls, no DB. Identity,
identifiers and clause segmentation are decided by the model (the folder-grouping
and metadata stages); this module only assembles the strings that get embedded:

- ``context_header`` / ``contextualize``: prepend document context to a chunk
  before it is embedded (contextual chunk embedding), while the raw chunk text is
  stored separately for display.
- ``build_profile_text``: one distilled document profile embedded as an extra row
  per logical document, so whole-document queries match ("which SPAs cap the
  seller's liability").
"""

from __future__ import annotations

__all__ = [
    "build_profile_text",
    "context_header",
    "contextualize",
]

_JOIN = " — "


def context_header(
    *,
    title: str | None,
    doc_type: str | None,
    matter_title: str | None,
) -> str:
    """One-line context prefix joining the present parts with `` — ``.

    Empty string if every part is missing.
    """
    parts = [part.strip() for part in (title, doc_type, matter_title) if part and part.strip()]
    return _JOIN.join(parts)


def contextualize(chunk_text: str, header: str) -> str:
    """Prepend ``header`` to the text that will be embedded.

    The caller still stores the raw ``chunk_text`` for display; only the embedded
    string carries the header.
    """
    if not header:
        return chunk_text
    return f"{header}\n\n{chunk_text}"


def build_profile_text(
    *,
    title: str | None,
    doc_type: str | None,
    matter_title: str | None,
    reference_numbers: list[str],
    parties: list[str],
    identifiers: list[str],
    doc_date: str | None,
    text: str,
    excerpt_chars: int = 400,
) -> str:
    """Assemble a deterministic one-paragraph document profile (no LLM, no regex).

    Every provided field becomes a labeled line; missing fields are skipped. The
    trailing excerpt is the first ``excerpt_chars`` characters of ``text``
    (whitespace-collapsed). Labels are English; the values stay in the document's
    language. Embedded as one extra row per logical document.
    """
    lines: list[str] = []
    if title and title.strip():
        lines.append(f"Title: {title.strip()}")
    if doc_type and doc_type.strip():
        lines.append(f"Document type: {doc_type.strip()}")
    if matter_title and matter_title.strip():
        lines.append(f"Matter: {matter_title.strip()}")

    refs = _clean_sequence(reference_numbers)
    if refs:
        lines.append(f"Reference numbers: {', '.join(refs)}")

    people = _clean_sequence(parties)
    if people:
        lines.append(f"Parties: {', '.join(people)}")

    idents = _clean_sequence(identifiers)
    if idents:
        lines.append(f"Identifiers: {', '.join(idents)}")

    if doc_date and doc_date.strip():
        lines.append(f"Date: {doc_date.strip()}")

    excerpt = _collapse_whitespace(text)[:excerpt_chars].strip()
    if excerpt:
        lines.append(f"Excerpt: {excerpt}")

    return "\n".join(lines)


def _clean_sequence(values: list[str] | None) -> list[str]:
    if not values:
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        stripped = (value or "").strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        cleaned.append(stripped)
    return cleaned


def _collapse_whitespace(value: str) -> str:
    return " ".join((value or "").split())
