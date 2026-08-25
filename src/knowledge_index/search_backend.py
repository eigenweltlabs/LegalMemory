"""OpenSearch adapter with authorization and metadata filters inside vector search."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime

import httpx

from knowledge_index.config import AppConfig
from knowledge_index.db.models import Chunk
from knowledge_index.permissions import CompiledAccessScope
from knowledge_index.retrieval_types import SearchFilters

_SOURCE_EXCLUDES = {"_source": {"excludes": ["embedding"]}}


# One pooled HTTP client for the whole process.
#
# Every call site here used the module-level ``httpx.post`` / ``httpx.get``,
# which builds a client, opens a TCP connection, and tears both down per
# request. A hybrid search issues several legs, so thirty concurrent searches
# meant hundreds of handshakes — and the cost was invisible from inside
# OpenSearch, which reported 64.6ms per query while callers measured seconds.
# A shared pool makes the measured cost the query cost.
_HTTP = httpx.Client(
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    timeout=30.0,
)


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
            "parties": getattr(chunk, "parties", None) or [],
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
        response = _HTTP.post(
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
        """BM25 over the chunk body, plus the party names the query appears to name.

        Extracted party names are otherwise reachable only through an exact filter
        nobody sets — measured over real agent traffic, `party` was passed on 1.5%
        of searches and `practice_area` never. Folding them into the ranked query
        makes the metadata earn its keep without the caller having to know it is
        there. Measured on 300 gold requests against this index: recall@20 is
        unchanged (0.870) but the answering document ranks higher — MRR 0.487 ->
        0.514, 43 requests improved against 11 worsened (p < 0.001) — for +13.7ms
        on a 20.7ms leg. It is a ranking aid, not a recall aid.
        """
        should: list[dict] = [{"match": {"text": {"query": query_text}}}]
        boost = self.config.retrieval.metadata_boost
        if boost > 0:
            for token in _named_entities(query_text):
                should.append(
                    {
                        "wildcard": {
                            "parties": {
                                "value": f"*{token}*",
                                "case_insensitive": True,
                                "boost": boost,
                            }
                        }
                    }
                )
        return {
            "size": size,
            **_SOURCE_EXCLUDES,
            "query": {
                "bool": {
                    "should": should,
                    "minimum_should_match": 1,
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
            # A metadata search returns DOCUMENTS, so it must page over documents.
            # Without the collapse below this asked for `_oversample(limit)` CHUNKS
            # and let the caller dedupe them: one 800-chunk offering memorandum then
            # consumed the whole window and the rest of the matter was never seen —
            # a matter with 33 documents returned 16, and `has_more` said false
            # because the collapse, not the corpus, had run out. Collapsing in the
            # index makes `size` count versions, so `limit` means what it says.
            #
            # The sort below is the collapse's tiebreaker as well as the caller's
            # order, and it is a total order, so paging stays stable.
            # Small headroom over the window: the ACL is already applied inside the
            # query, so the SQL re-verify only drops rows the index has gone stale
            # on. A handful covers that without going back to 5x oversampling.
            body: dict = {
                "size": min(max(limit + 10, 20), 2500),
                "collapse": {"field": "document_version_id"},
                **_SOURCE_EXCLUDES,
                "query": strict_filter,
                # F6 date-trust guard: after O10, undated docs carry a null
                # doc_date (honest) rather than a fake mtime. Sort them last
                # instead of letting the null jump to the top. A date_from/date_to
                # range filter already excludes null-dated docs (a range query
                # never matches a missing field), so bounded date searches drop
                # them entirely — undated == not date-searchable, by design.
                #
                # document_version_id breaks doc_date ties. Without it the order
                # among same-dated chunks is whatever the shards return, which
                # differs between two calls — so an offset page could repeat a
                # row the previous page already showed and skip one it did not.
                # It is a total order over versions, which is the granularity
                # the collapse downstream keeps anyway.
                "sort": [
                    {"doc_date": {"order": "desc", "missing": "_last"}},
                    {"document_version_id": {"order": "asc"}},
                ],
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
        """BM25 leg over the English-analyzed text field, ACL-scoped like kNN."""
        self.ensure_index()
        strict_filter = _combined_filter(scope, filters)
        if "match_none" in strict_filter:
            return []
        return self._run_search(self._lexical_body(query_text, strict_filter, _oversample(limit)))

    def document_versions_containing(
        self,
        phrase: str,
        *,
        scope: CompiledAccessScope,
        filters: SearchFilters,
        max_versions: int = 4000,
    ) -> set[str]:
        """Every authorized document version whose text contains ``phrase``.

        Enumeration, not ranking: one aggregation bucket per version and no
        scores, so a phrase that appears once in a long document counts exactly
        as much as one that appears fifty times — which is the difference
        between "find the top mentions" and "find every document that mentions
        it at all". The phrase runs through the same analyzer as the lexical
        leg, and the ACL scope filters before the aggregation like every other
        leg. Capped at ``max_versions`` buckets; beyond that the caller is
        enumerating the estate, not filtering it.
        """
        self.ensure_index()
        strict_filter = _combined_filter(scope, filters)
        if "match_none" in strict_filter:
            return set()
        body = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [strict_filter],
                    "must": [{"match_phrase": {"text": phrase}}],
                }
            },
            "aggs": {
                "versions": {
                    "terms": {"field": "document_version_id", "size": max_versions}
                }
            },
        }
        response = _HTTP.post(
            f"{self.base_url}/{self.index_name}/_search", json=body, timeout=30
        )
        response.raise_for_status()
        buckets = (
            response.json()
            .get("aggregations", {})
            .get("versions", {})
            .get("buckets", [])
        )
        return {bucket["key"] for bucket in buckets}

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
        query_vector: list[float] | None,
        scope: CompiledAccessScope,
        filters: SearchFilters,
        limit: int,
    ) -> dict[str, list[dict]]:
        """Run the lexical, semantic and identifier legs in ONE `_msearch` round-trip.

        Each leg is independently ACL-scoped by the same strict filter — fusion never
        sees an unauthorized row. Returns a dict keyed by leg name; a leg that cannot
        match (empty query / fully-denied scope / ``query_vector=None`` for a caller
        that disabled the semantic leg) returns an empty list without a network hop
        for that leg."""
        self.ensure_index()
        strict_filter = _combined_filter(scope, filters)
        size = _oversample(limit)
        denied = "match_none" in strict_filter

        # (leg name, body or None-if-skippable). Order is preserved end to end.
        legs: list[tuple[str, dict | None]] = [
            ("lexical", None if denied else self._lexical_body(query_text, strict_filter, size)),
            (
                "semantic",
                None
                if denied or query_vector is None
                else self._knn_body(query_vector, strict_filter, size),
            ),
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
        response = _HTTP.post(
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
        response = _HTTP.get(f"{self.base_url}/{self.index_name}/_mapping", timeout=5)
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
        response = _HTTP.post(
            f"{self.base_url}/{self.index_name}/_search",
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        return list(response.json().get("hits", {}).get("hits", []))

    def _mapping_properties(self) -> dict:
        return {
            "text": {"type": "text", "analyzer": "legal_english"},
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
            "parties": {"type": "keyword"},
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
        response = _HTTP.get(f"{self.base_url}/{self.index_name}/_mapping", timeout=5)
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

    #: filters whose stored values are human-readable enough to hand back.
    #: doc_type is deliberately absent — its values are opaque ontology node ids
    #: ("RC5oECr2QXab5MFKmjUd7y6"), which tell a caller nothing it can act on.
    _SUGGESTIBLE = {"party": "parties", "identifier": "identifiers"}

    def suggest_filter_values(
        self,
        *,
        scope: CompiledAccessScope,
        filters: SearchFilters,
        limit: int = 5,
    ) -> dict[str, list[str]]:
        """Near-miss values for the filters that just returned nothing.

        A filtered search that returns an empty list tells the caller nothing about
        *why*, so an agent either guesses again or wanders off to another matter —
        both observed. Each suggestible filter is re-run with that one filter
        dropped, and the field's surviving values in the caller's own scope are
        offered back. Only ever called on the empty path, so the normal search pays
        nothing for it.
        """
        out: dict[str, list[str]] = {}
        for name, field in self._SUGGESTIBLE.items():
            wanted = getattr(filters, name, None)
            if not wanted:
                continue
            # Drop the failing filter, keep every other constraint and the ACL.
            relaxed = replace(filters, **{name: None})
            body = {
                "size": 0,
                "query": _combined_filter(scope, relaxed),
                "aggs": {"v": {"terms": {"field": field, "size": 400}}},
            }
            try:
                response = _HTTP.post(
                    f"{self.base_url}/{self.index_name}/_search", json=body, timeout=30
                )
                response.raise_for_status()
                buckets = response.json()["aggregations"]["v"]["buckets"]
            except Exception:  # a suggestion is a courtesy; never fail the search for it
                continue
            needle = str(wanted).casefold()
            # Match on any word of the requested value, not just the whole string:
            # a caller asking for "Huang-Whitfield" should be shown the Whitfield
            # entities. Short words are dropped so "of"/"AG" cannot match everything.
            tokens = [t for t in re.split(r"[^0-9a-z]+", needle) if len(t) >= 3]
            near = [
                value
                for value in (str(b["key"]) for b in buckets)
                if not _looks_like_id(value)
                and (
                    needle in value.casefold()
                    or any(token in value.casefold() for token in tokens)
                )
            ]
            # No near miss means the value simply is not here. Offering unrelated
            # values would be worse than silence — a list of arbitrary case numbers
            # reads as a menu and invites the caller to pick one at random.
            if near:
                out[name] = near[:limit]
        return out

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
                    "analysis": {"analyzer": {"legal_english": {"type": "english"}}},
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
    """Candidate depth to request per leg for a caller that wants ``limit`` hits.

    The ceiling has to clear the deepest window a paginated caller can ask for
    (``offset + limit``), not just the first page: fusion, collapse and the ACL
    re-verify all shrink the candidate set, so a leg that stops at 500 chunks
    cannot honestly answer whether a page at offset 400 exists. A first-page
    search is unaffected — limit 8 still asks for 400, well under either cap.
    """
    return min(max(limit * 5, 50), 2500)


# Words that open a question are capitalised by grammar, not because they name
# anything — without this every request would match on its first word.
_SENTENCE_OPENERS = frozenset(
    """What Which When Where Who Whose Why How Pull Does Did Have Has Can Could Should
    Would Check Find Show Give Tell Look There This That These Those Please""".split()
)


def _named_entities(text: str, *, limit: int = 4) -> list[str]:
    """Capitalised words a query uses to name a party, cheaply and without a model.

    Deliberately crude: a false positive costs one extra should-clause that matches
    no party, which changes nothing. Bounded so a long query cannot fan out.
    """
    out: list[str] = []
    for word in text.split():
        # Keep the leading run of letters/digits only: it drops trailing punctuation
        # and the possessive/contraction tail, so "What's" is recognised as the
        # opener "What" rather than sneaking through as its own name.
        token = re.split(r"[^0-9A-Za-z]", word, maxsplit=1)[0]
        if len(token) >= 4 and token[:1].isupper() and token not in _SENTENCE_OPENERS:
            if token not in out:
                out.append(token)
        if len(out) >= limit:
            break
    return out


_UUID_SHAPE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _looks_like_id(value: str) -> bool:
    """``parties`` mixes resolved entity ids with canonical names; only names help."""
    return bool(_UUID_SHAPE.match(value))


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
    # matter_ids carries a resolved practice_area (the matters it covers); RetrievalService
    # sets it. An empty list is meaningful — the practice_area matched no matter — so it
    # short-circuits to no hits instead of being ignored.
    if filters.matter_ids is not None:
        if not filters.matter_ids:
            return {"match_none": {}}
        clauses.append({"terms": {"matter_id": filters.matter_ids}})
    # document_version_ids carries a resolved contains_all_terms intersection
    # (RetrievalService sets it); same contract as matter_ids — an empty list is
    # a real answer, not an absent filter.
    if filters.document_version_ids is not None:
        if not filters.document_version_ids:
            return {"match_none": {}}
        clauses.append(
            {"terms": {"document_version_id": filters.document_version_ids}}
        )
    for field, value in (
        ("project_id", filters.project_id),
        ("matter_id", filters.matter_id),
        ("version_status", filters.version_status),
        ("language", filters.language),
        # F3/F4 exact-term filters. `party` matches a party_id or canonical name;
        # `clause_type` a clause-facet node id on clause chunks; `chunk_kind` scopes
        # to body/profile/clause chunks. (`identifier` is handled below — a raw
        # keyword term match was too strict to be usable.)
        ("clause_type", filters.clause_type),
        ("chunk_kind", filters.chunk_kind),
        # Search inside one document (or one specific version of it).
        ("document_id", filters.document_id),
        ("document_version_id", filters.document_version_id),
    ):
        if value:
            clauses.append({"term": {field: value}})
    if filters.party:
        # `parties` is a keyword field holding BOTH resolved entity ids and canonical
        # names, so an exact term match only works for a caller who already knows
        # which of the two a given document stored, spelled exactly. Callers do not:
        # measured over real agent calls, 16 of 25 party filters that returned nothing
        # used a short form of a name that is stored in full ("Thornton" against
        # "Thornton & Associates LLP"). Match the id exactly, the name loosely.
        clauses.append(
            {
                "bool": {
                    "should": [
                        {"term": {"parties": {"value": filters.party, "case_insensitive": True}}},
                        {"prefix": {"parties": {"value": filters.party, "case_insensitive": True}}},
                        {
                            "wildcard": {
                                "parties": {
                                    "value": f"*{filters.party}*",
                                    "case_insensitive": True,
                                }
                            }
                        },
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    if filters.identifier:
        # `identifiers` is a raw keyword field holding values copied verbatim from
        # the document ("LF-2024-0917", "Agreement No. CX-MSA-2025-0042"), so an
        # exact term match is case- and punctuation-sensitive against whatever the
        # caller typed. Measured: 200 of 205 real agent calls returned zero. Accept
        # the exact value, OR the same value as a phrase over the analyzed
        # identifier text, which is case-insensitive and tolerates the caller's
        # separators and any prefix words around the value.
        clauses.append(
            {
                "bool": {
                    "should": [
                        {"term": {"identifiers": filters.identifier}},
                        {"match_phrase": {"identifiers_text": filters.identifier}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    # chunk_kinds (terms) is the broad kind scope; a single chunk_kind term above is
    # narrower and simply ANDs with it when both are set.
    if filters.chunk_kinds:
        clauses.append({"terms": {"chunk_kind": filters.chunk_kinds}})
    date_range = _date_range(filters.date_from, filters.date_to)
    if date_range:
        clauses.append({"range": {"doc_date": date_range}})
    # only_final is deliberately NOT a clause here. It selects a VERSION, and which
    # version is authoritative is a fact about the document's other versions, which
    # a per-chunk term filter cannot see: `version_status in {final, executed}` hid
    # every single-version draft too, even though nothing supersedes it. It is
    # applied after materialization, where the document's siblings are known —
    # see RetrievalService._drop_superseded.
    return {"bool": {"filter": clauses}}


def _date_range(start: datetime | None, end: datetime | None) -> dict:
    result: dict[str, str] = {}
    if start:
        result["gte"] = start.isoformat()
    if end:
        result["lte"] = end.isoformat()
    return result
