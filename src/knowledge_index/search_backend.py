"""OpenSearch adapter with authorization and metadata filters inside vector search."""

from __future__ import annotations

import json
from datetime import datetime

import httpx

from knowledge_index.config import AppConfig
from knowledge_index.db.models import Chunk
from knowledge_index.permissions import CompiledAccessScope
from knowledge_index.retrieval_types import SearchFilters

_SOURCE_EXCLUDES = {"_source": {"excludes": ["embedding"]}}


class OpenSearchIndex:
    """Small adapter; OpenSearch owns BM25/vector indexing, not authorization policy."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.base_url = config.components.opensearch_url.rstrip("/")
        self.index_name = config.retrieval.index_name
        self._index_ready = False

    def _doc_body(self, chunk: Chunk) -> dict:
        return {
            "text": chunk.text,
            "project_id": chunk.project_id,
            "document_id": chunk.document_id,
            "document_version_id": chunk.document_version_id,
            "matter_id": chunk.matter_id,
            "doc_type": chunk.doc_type,
            "doc_type_ancestors": getattr(chunk, "doc_type_ancestors", None) or [],
            "version_status": chunk.version_status,
            "language": chunk.language,
            "doc_date": chunk.doc_date.isoformat() if chunk.doc_date else None,
            "chunk_kind": (chunk.meta or {}).get("kind"),
            "clause_type": (chunk.meta or {}).get("clause_type"),
            "identifiers": getattr(chunk, "identifiers", None) or [],
            "identifiers_text": " ".join(getattr(chunk, "identifiers", None) or []),
            "allowed_principals": chunk.allowed_principals or [],
            "denied_principals": chunk.denied_principals or [],
            "access_version": chunk.access_version,
            "embedding": list(chunk.embedding or []),
            "meta": chunk.meta or {},
        }

    def upsert(self, chunk: Chunk) -> None:
        # One code path: single-item writes delegate to bulk_sync.
        self.bulk_sync(deletes=[], upserts=[chunk])

    def delete(self, chunk_id: str) -> None:
        # One code path: single-item deletes delegate to bulk_sync.
        self.bulk_sync(deletes=[chunk_id], upserts=[])

    def bulk_sync(self, deletes: list[str], upserts: list[Chunk]) -> None:
        """Apply deletes + upserts in a single ``_bulk`` request.

        ensure_index runs once at the top (not per item). A partial failure raises
        RuntimeError listing the failed items — nothing degrades silently."""

        if not deletes and not upserts:
            return
        self.ensure_index()
        lines: list[str] = []
        for chunk_id in deletes:
            lines.append(json.dumps({"delete": {"_id": chunk_id}}))
        for chunk in upserts:
            lines.append(json.dumps({"index": {"_id": chunk.id}}))
            lines.append(json.dumps(self._doc_body(chunk)))
        body = "\n".join(lines) + "\n"
        response = httpx.post(
            f"{self.base_url}/{self.index_name}/_bulk",
            params={"refresh": "false"},
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("errors"):
            return
        failures: list[str] = []
        for item in payload.get("items", []):
            action = next(iter(item.values()))
            if action.get("error"):
                failures.append(f"{action.get('_id')}: {action['error']}")
        if failures:
            raise RuntimeError(f"OpenSearch bulk_sync failed for {len(failures)} items: {failures}")

    def _knn_body(
        self, query_vector: list[float], strict_filter: dict, size: int
    ) -> dict:
        return {
            "size": size,
            **_SOURCE_EXCLUDES,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": query_vector,
                        "k": size,
                        "filter": strict_filter,
                    }
                }
            },
        }

    def _lexical_body(self, query_text: str, strict_filter: dict, size: int) -> dict:
        return {
            "size": size,
            **_SOURCE_EXCLUDES,
            "query": {
                "bool": {
                    "must": {"match": {"text": {"query": query_text}}},
                    "filter": strict_filter["bool"]["filter"],
                }
            },
        }

    def _identifier_body(self, query_text: str, strict_filter: dict, size: int) -> dict:
        return {
            "size": size,
            **_SOURCE_EXCLUDES,
            "query": {
                "bool": {
                    "must": {"match": {"identifiers_text": {"query": query_text}}},
                    "filter": strict_filter["bool"]["filter"],
                }
            },
        }

    def search(
        self,
        *,
        query_vector: list[float] | None,
        scope: CompiledAccessScope,
        filters: SearchFilters,
        limit: int,
    ) -> list[dict]:
        """Semantic (approximate-kNN/HNSW) leg; metadata-only when no vector is supplied."""
        self.ensure_index()
        strict_filter = _combined_filter(scope, filters)
        size = _oversample(limit)
        if query_vector is None:
            body: dict = {
                "size": size,
                **_SOURCE_EXCLUDES,
                "query": strict_filter,
                "sort": [{"doc_date": {"order": "desc"}}],
            }
            return self._run_search(body)
        return self._run_search(self._knn_body(query_vector, strict_filter, size))

    def search_lexical(
        self,
        query_text: str,
        *,
        scope: CompiledAccessScope,
        filters: SearchFilters,
        limit: int,
    ) -> list[dict]:
        """BM25 leg over the German-analyzed text field, ACL-scoped like kNN."""
        self.ensure_index()
        strict_filter = _combined_filter(scope, filters)
        if "match_none" in strict_filter:
            return []
        return self._run_search(self._lexical_body(query_text, strict_filter, _oversample(limit)))

    def search_identifier(
        self,
        query_text: str,
        *,
        scope: CompiledAccessScope,
        filters: SearchFilters,
        limit: int,
    ) -> list[dict]:
        """Exact-identifier leg: match the query against the analyzed identifiers
        field, so a pasted case number / Aktenzeichen / statute reference matches
        the document that carries it. No regex parsing of the query."""
        self.ensure_index()
        if not query_text.strip():
            return []
        strict_filter = _combined_filter(scope, filters)
        if "match_none" in strict_filter:
            return []
        return self._run_search(
            self._identifier_body(query_text, strict_filter, _oversample(limit))
        )

    def multi_search(
        self,
        *,
        query_text: str,
        query_vector: list[float],
        scope: CompiledAccessScope,
        filters: SearchFilters,
        limit: int,
    ) -> dict[str, list[dict]]:
        """Run the lexical, semantic and identifier legs in ONE `_msearch` round-trip.

        Each leg is independently ACL-scoped by the same strict filter — fusion never
        sees an unauthorized row. Returns a dict keyed by leg name; a leg that cannot
        match (empty query / fully-denied scope) returns an empty list without a
        network hop for that leg."""
        self.ensure_index()
        strict_filter = _combined_filter(scope, filters)
        size = _oversample(limit)
        denied = "match_none" in strict_filter

        # (leg name, body or None-if-skippable). Order is preserved end to end.
        legs: list[tuple[str, dict | None]] = [
            ("lexical", None if denied else self._lexical_body(query_text, strict_filter, size)),
            ("semantic", self._knn_body(query_vector, strict_filter, size)),
            (
                "identifier",
                None
                if denied or not query_text.strip()
                else self._identifier_body(query_text, strict_filter, size),
            ),
        ]
        active = [(name, body) for name, body in legs if body is not None]
        results: dict[str, list[dict]] = {name: [] for name, _ in legs}
        if not active:
            return results

        lines: list[str] = []
        for _name, body in active:
            lines.append(json.dumps({}))  # per-search header targets the same index (URL)
            lines.append(json.dumps(body))
        payload = "\n".join(lines) + "\n"
        response = httpx.post(
            f"{self.base_url}/{self.index_name}/_msearch",
            params={"typed_keys": "false"},
            content=payload.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
            timeout=30,
        )
        response.raise_for_status()
        responses = response.json().get("responses", [])
        for (name, _body), leg_response in zip(active, responses, strict=False):
            if leg_response.get("error"):
                raise RuntimeError(f"OpenSearch {name} leg failed: {leg_response['error']}")
            results[name] = list(leg_response.get("hits", {}).get("hits", []))
        return results

    def _verify_dimension(self) -> None:
        """Fail loudly if the existing index was built for a different embedding
        dimension than the configured model produces. A different-dimension model
        physically cannot enter the field; different-model-same-dimension vectors are
        blocked by binding the index name to the embedding signature + the reindex flow.
        Nothing degrades silently."""
        response = httpx.get(f"{self.base_url}/{self.index_name}/_mapping", timeout=5)
        response.raise_for_status()
        mappings = response.json().get(self.index_name, {}).get("mappings", {})
        embedding = (mappings.get("properties") or {}).get("embedding") or {}
        existing = embedding.get("dimension")
        expected = self.config.retrieval.embedding_dimensions
        if existing is not None and existing != expected:
            raise RuntimeError(
                f"index {self.index_name!r} was built for embedding dimension {existing}, "
                f"but the configured embedding model produces dimension {expected}. Vectors "
                f"from two models cannot share one ANN index — trigger a rebuild "
                f"(POST /api/actions/reindex) so a fresh, uniform index is created."
            )

    def matter_hits_by_vector(self, query_vector: list[float], *, size: int = 50) -> list[dict]:
        """Unscoped kNN used ONLY by the ingestion-time matter search.

        Returns each hit's ``matter_id``/``document_id`` and score — never text — with
        no ACL scoping, because classification legitimately needs corpus-wide visibility
        to link a new document to an existing matter (a referenced master contract may
        be filed under another project). This is never called on the user query path,
        where every leg stays ACL-scoped."""
        self.ensure_index()
        body = {
            "size": size,
            "_source": {"includes": ["matter_id", "document_id"]},
            "query": {"knn": {"embedding": {"vector": query_vector, "k": size}}},
        }
        return self._run_search(body)

    def _run_search(self, body: dict) -> list[dict]:
        response = httpx.post(
            f"{self.base_url}/{self.index_name}/_search",
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        return list(response.json().get("hits", {}).get("hits", []))

    def _mapping_properties(self) -> dict:
        return {
            "text": {"type": "text", "analyzer": "legal_german"},
            "project_id": {"type": "keyword"},
            "document_id": {"type": "keyword"},
            "document_version_id": {"type": "keyword"},
            "matter_id": {"type": "keyword"},
            "doc_type": {"type": "keyword"},
            "doc_type_ancestors": {"type": "keyword"},
            "chunk_kind": {"type": "keyword"},
            "clause_type": {"type": "keyword"},
            "version_status": {"type": "keyword"},
            "language": {"type": "keyword"},
            "doc_date": {"type": "date"},
            "identifiers": {"type": "keyword"},
            "identifiers_text": {"type": "text"},
            "allowed_principals": {"type": "keyword"},
            "denied_principals": {"type": "keyword"},
            "access_version": {"type": "integer"},
            "embedding": {
                "type": "knn_vector",
                "dimension": self.config.retrieval.embedding_dimensions,
                "method": {
                    "name": "hnsw",
                    "space_type": self.config.retrieval.vector_space_type,
                    "engine": self.config.retrieval.vector_engine,
                    "parameters": {
                        "m": self.config.retrieval.hnsw_m,
                        "ef_construction": self.config.retrieval.hnsw_ef_construction,
                    },
                },
            },
            "meta": {"type": "object", "enabled": False},
        }

    def _reconcile_mapping(self) -> None:
        """Add mapped fields the live index is missing (the mapping is dynamic:false,
        so an unmapped field is silently dropped at index time — a code-side field
        addition must be pushed to indexes created before it). Additive only; docs
        indexed before the push need a re-sync to become searchable on new fields."""
        response = httpx.get(f"{self.base_url}/{self.index_name}/_mapping", timeout=5)
        response.raise_for_status()
        live = next(iter(response.json().values()))["mappings"].get("properties", {})
        missing = {
            field: spec
            for field, spec in self._mapping_properties().items()
            if field not in live
        }
        if not missing:
            return
        pushed = httpx.put(
            f"{self.base_url}/{self.index_name}/_mapping",
            json={"properties": missing},
            timeout=20,
        )
        pushed.raise_for_status()

    def ensure_index(self) -> None:
        if self._index_ready:
            return
        response = httpx.head(f"{self.base_url}/{self.index_name}", timeout=5)
        if response.status_code == 200:
            self._verify_dimension()
            self._reconcile_mapping()
            self._index_ready = True
            return
        if response.status_code != 404:
            response.raise_for_status()
        created = httpx.put(
            f"{self.base_url}/{self.index_name}",
            json={
                "settings": {
                    "index.knn": True,
                    "number_of_replicas": 0,
                    "analysis": {"analyzer": {"legal_german": {"type": "german"}}},
                },
                "mappings": {
                    "dynamic": False,
                    "properties": self._mapping_properties(),
                },
            },
            timeout=20,
        )
        if created.status_code not in {200, 201, 400}:
            created.raise_for_status()
        if created.status_code == 400 and created.json().get("error", {}).get("type") != (
            "resource_already_exists_exception"
        ):
            created.raise_for_status()
        self._index_ready = True


def _oversample(limit: int) -> int:
    return min(max(limit * 5, 50), 500)


def _combined_filter(scope: CompiledAccessScope, filters: SearchFilters) -> dict:
    access_filter = scope.opensearch_filter()
    if "match_none" in access_filter:
        return access_filter
    clauses: list[dict] = [access_filter]
    # doc_type is hierarchical: the filter value is an ontology node id, and
    # matching runs against the indexed ancestor closure — filtering by an
    # interior node ("Agreements") matches every document typed at or below it.
    if filters.doc_type:
        clauses.append({"term": {"doc_type_ancestors": filters.doc_type}})
    for field, value in (
        ("project_id", filters.project_id),
        ("matter_id", filters.matter_id),
        ("version_status", filters.version_status),
        ("language", filters.language),
        ("clause_type", filters.clause_type),
        ("chunk_kind", filters.chunk_kind),
    ):
        if value:
            clauses.append({"term": {field: value}})
    date_range = _date_range(filters.date_from, filters.date_to)
    if date_range:
        clauses.append({"range": {"doc_date": date_range}})
    return {"bool": {"filter": clauses}}


def _date_range(start: datetime | None, end: datetime | None) -> dict:
    result: dict[str, str] = {}
    if start:
        result["gte"] = start.isoformat()
    if end:
        result["lte"] = end.isoformat()
    return result
