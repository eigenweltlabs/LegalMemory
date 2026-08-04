"""Dependency-light retrieval request types shared with search backends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
    practice_area: str | None = None
    matter_ids: list[str] | None = None
