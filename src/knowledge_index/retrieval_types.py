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
    # clause-facet node id; matches only clause chunks carrying that type
    clause_type: str | None = None
    # body | profile | clause — scope search to one chunk kind
    chunk_kind: str | None = None
