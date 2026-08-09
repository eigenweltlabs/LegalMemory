"""Dependency-light retrieval request types shared with search backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Page:
    """One window of a longer result set, plus how to ask for the next one.

    Every list-shaped tool returns this rather than a bare list. A bare list
    cannot distinguish "that is everything" from "that is the first 20 of
    1,300", so a caller that received exactly ``limit`` rows had no way to know
    whether the corpus continued — it either stopped early or re-issued the
    same call with a bigger limit until it hit the cap.

    ``total`` is the exact number of matching items and is present only where it
    can be counted without doing the work of every page (a SQL count, or a set
    the tool already materialized in full). ``has_more`` is always exact and is
    the field a caller should branch on.
    """

    items: list[Any] = field(default_factory=list)
    offset: int = 0
    limit: int = 0
    has_more: bool = False
    total: int | None = None

    @classmethod
    def slice(
        cls, items: list[Any], *, offset: int, limit: int, total: int | None = None
    ) -> "Page":
        """Page a list the caller already holds in full, so ``total`` is exact."""
        window = items[offset : offset + limit]
        return cls(
            items=window,
            offset=offset,
            limit=limit,
            has_more=offset + len(window) < len(items),
            total=len(items) if total is None else total,
        )

    @classmethod
    def probe(cls, probed: list[Any], *, offset: int, limit: int) -> "Page":
        """Page a list fetched with one extra row (the ``limit + 1`` probe).

        The extra row is the only thing that distinguishes a full last page from
        a full page with more behind it, and it costs one row instead of a count
        over the whole matching set. ``total`` stays unknown.
        """
        return cls(
            items=probed[:limit],
            offset=offset,
            limit=limit,
            has_more=len(probed) > limit,
            total=None,
        )

    def as_dict(self) -> dict:
        """The ``page`` block of a tool result. ``total`` is omitted when unknown."""
        page = {
            "offset": self.offset,
            "limit": self.limit,
            "returned": len(self.items),
            "has_more": self.has_more,
            "next_offset": self.offset + len(self.items) if self.has_more else None,
        }
        if self.total is not None:
            page["total"] = self.total
        return page


@dataclass
class SearchFilters:
    project_id: str | None = None
    matter_id: str | None = None
    doc_type: str | None = None
    version_status: str | None = None
    language: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    # True → restrict to authoritative versions (version_status in {final, executed}).
    # Composes (AND) with version_status: a more specific version_status narrows further,
    # and a contradictory one (e.g. draft) simply yields no hits rather than erroring.
    only_final: bool = False
    # Exact-term filters (all AND with the above).
    # identifier: an exact identifier value (case number, Aktenzeichen, HRB, statute
    #   ref) — distinct from the fuzzy identifier ranking leg, which matches loosely.
    # party: a resolved party_id or a party's exact canonical name.
    # clause_type: a clause-facet node id; matches only clause chunks carrying it.
    # chunk_kind: restrict to a chunk kind — "chunk" (body) | "profile" | "clause".
    identifier: str | None = None
    party: str | None = None
    clause_type: str | None = None
    chunk_kind: str | None = None
    # chunk_kinds: restrict to a *set* of chunk kinds (terms semantics). Set by
    #   RetrievalService from config.retrieval.search_chunk_kinds; a single
    #   chunk_kind above is the narrower filter and wins when both are present.
    chunk_kinds: list[str] | None = None
    # practice_area: an Area-of-Law ontology node id, SUBTREE semantics (a parent area
    #   matches its children). The practice area lives on the Matter, not on the chunk, so
    #   RetrievalService resolves this into ``matter_ids`` (the matters it covers) BEFORE the
    #   query reaches the search backend — the backend never reads this field.
    # matter_ids: restrict to this set of matters (ANDs with ``matter_id``). Carries the
    #   resolved practice_area matter set to the backend; an empty list is meaningful — it
    #   means the practice_area filter matched no matters, so the search returns nothing.
    # practice_group / firm_person / lifecycle: the same story as practice_area — all
    #   three are properties of the MATTER, not of a chunk, so RetrievalService resolves
    #   them into ``matter_ids`` before the query reaches the backend, which never reads
    #   these fields. practice_group matches any group staffed on the matter and
    #   firm_person any of its lawyers, exactly as list_matters does, so a filtered
    #   search and a filtered listing always describe the same set of matters.
    practice_area: str | None = None
    # matter_kind: a Service node id, SUBTREE semantics, resolved to matter_ids the
    #   same way practice_area is. What the firm is DOING, as against what body of
    #   law applies.
    matter_kind: str | None = None
    practice_group: str | None = None
    firm_person: str | None = None
    lifecycle: str | None = None
    matter_ids: list[str] | None = None
