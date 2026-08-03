"""Rebuild the OpenSearch index from embeddings already stored in Postgres.

The vectors are not recomputed. `chunks.embedding` is populated by the insertion
pipeline and is the expensive part of indexing; a restored dump carries it, so
rebuilding search is a bulk copy from one store to another rather than a second
pass over an embedding API. On a 742k-chunk corpus that is the difference
between minutes and an hour of billed tokens.

Concurrency is the point of this script. Chunks are read in keyset-paginated
slices and pushed by a pool of workers, each owning its own database connection
and HTTP client, so the index is fed from every core at once. Defaults are sized
for the deployment host (8 vCPU) and overridable:

    KI_REINDEX_WORKERS    threads in this process (default: 2x cores)
    KI_REINDEX_BATCH      chunks per _bulk call   (default: 500)
    KI_REINDEX_SHARDS     number of processes co-operating (default: 1)
    KI_REINDEX_SHARD      which shard this process owns, 0-based

Threads alone do not saturate the machine. Building a bulk body is
`json.dumps` over 1536 floats per chunk — pure Python, holding the GIL — so a
single interpreter pins one core while the database and OpenSearch idle. Run
one process per core, each taking a disjoint shard of the ranges:

    for i in $(seq 0 5); do
      KI_REINDEX_SHARDS=6 KI_REINDEX_SHARD=$i python reindex_from_embeddings.py &
    done

Run it inside the appliance container, which already has the config and the
models:

    python /app/deploy/reindex_from_embeddings.py

It is idempotent. Documents are indexed by chunk id, so a re-run overwrites
rather than duplicates, and an interrupted run is resumed by running it again.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from knowledge_index.config_store import ConfigStore
from knowledge_index.db.models import Chunk
from knowledge_index.search_backend import OpenSearchIndex


def load_config():
    """The appliance's live configuration, same path the web app reads.

    Not the defaults: the index name is derived from the embedding signature, so
    a config built from scratch can point at a differently-named index than the
    one the corpus was written to.
    """
    return ConfigStore(pathlib.Path(".ki/config.json")).get()

CORES = os.cpu_count() or 4
WORKERS = int(os.environ.get("KI_REINDEX_WORKERS", CORES * 2))
BATCH = int(os.environ.get("KI_REINDEX_BATCH", 500))
SHARDS = int(os.environ.get("KI_REINDEX_SHARDS", 1))
SHARD = int(os.environ.get("KI_REINDEX_SHARD", 0))

_done = 0
_failed = 0
_lock = threading.Lock()


def _progress(n: int, total: int, started: float, failed: int) -> None:
    elapsed = max(time.time() - started, 1e-6)
    rate = n / elapsed
    remaining = (total - n) / rate if rate else 0
    sys.stdout.write(
        f"\r  {n:>7,}/{total:,} chunks  {rate:>6.0f}/s  "
        f"eta {remaining / 60:>4.1f}m  failed={failed}"
    )
    sys.stdout.flush()


def _worker(slice_no: int, bounds: tuple[str, str | None], dsn: str, total: int, started: float) -> int:
    """Index one contiguous id-range. Owns its own connection and client.

    A worker per range rather than a shared queue: the ranges are disjoint by
    construction, so no worker waits on another and a failure is contained to
    the slice that caused it.
    """
    global _done, _failed
    lo, hi = bounds
    engine = create_engine(dsn, pool_pre_ping=True)
    config = load_config()
    index = OpenSearchIndex(config)
    # ensure_index is racy across workers and only needs doing once; the caller
    # has already done it.
    index._index_ready = True

    indexed = 0
    with Session(engine) as session:
        cursor = lo
        while True:
            # Strictly greater: advancing the cursor by appending a NUL is how
            # you keyset-paginate a text key in most databases and how you get
            # "invalid byte sequence" from Postgres, which forbids NUL in text.
            # The lower bound is an 8-character prefix and no id equals it, so
            # a strict comparison loses nothing at the slice boundary either.
            stmt = select(Chunk).where(Chunk.id > cursor)
            if hi is not None:
                stmt = stmt.where(Chunk.id < hi)
            rows = list(session.scalars(stmt.order_by(Chunk.id).limit(BATCH)))
            if not rows:
                break
            try:
                index.bulk_sync([], rows)
                n = len(rows)
            except Exception as exc:  # noqa: BLE001 - report, do not abort the run
                n = 0
                with _lock:
                    _failed += len(rows)
                sys.stdout.write(f"\n  slice {slice_no}: {type(exc).__name__}: {exc}\n")
            indexed += n
            with _lock:
                _done += n
                _progress(_done, total, started, _failed)
            cursor = rows[-1].id
            session.expunge_all()
    engine.dispose()
    return indexed


def main() -> int:
    config = load_config()
    dsn = os.environ.get("KI_DATABASE_URL")
    if not dsn:
        print("KI_DATABASE_URL is not set", file=sys.stderr)
        return 1
    engine = create_engine(dsn)

    with Session(engine) as session:
        total = session.scalar(select(func.count()).select_from(Chunk)) or 0
        if not total:
            print("no chunks in the database — nothing to index")
            return 1
        missing = session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.embedding.is_(None))
        )

    index = OpenSearchIndex(config)
    index.ensure_index()

    if SHARD == 0:
        print(f"chunks in database : {total:,}")
        print(f"without embeddings : {missing:,}  (these index as text-only)")
        print(f"processes x threads: {SHARDS} x {WORKERS}   batch: {BATCH}")

    # Split the id space into one contiguous range per worker.
    #
    # Chunk ids are uuid4 in canonical form, so their leading hex digits are
    # uniformly distributed and the cut points can be computed rather than
    # measured: no percentile query over 742k rows, and no dependence on how a
    # given Postgres collation orders the key. Verified against the real corpus,
    # where quartile boundaries land within a percent of the arithmetic ones.
    width = 16 ** 8
    slices = WORKERS * SHARDS
    edges = ["", *[f"{i * width // slices:08x}" for i in range(1, slices)], None]
    ranges = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    # Interleaved rather than contiguous, so every process gets a mix of the id
    # space and one slow region cannot leave a single process running alone.
    ranges = ranges[SHARD::SHARDS]

    # Indexing throughput, not query latency, is what matters for the next few
    # minutes: no replicas to write twice, and no refresh between batches.
    base = config.components.opensearch_url.rstrip("/")
    name = index.index_name
    if SHARD == 0:
        httpx.put(f"{base}/{name}/_settings", timeout=30,
                  json={"index": {"refresh_interval": "-1", "number_of_replicas": 0}})

    started = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [
            pool.submit(_worker, i, r, dsn, total, started)
            for i, r in enumerate(ranges)
        ]
        for f in as_completed(futures):
            f.result()

    elapsed = time.time() - started
    print(f"\nshard {SHARD}: {_done:,} chunks in {elapsed / 60:.1f}m "
          f"({_done / max(elapsed, 1):.0f}/s) failed={_failed:,}")
    # Restoring the settings and counting is the whole index's business, not one
    # shard's; the caller does it once every process has exited.
    return 0 if not _failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
