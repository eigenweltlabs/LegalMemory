"""Low-latency monitoring for local / mounted filesystem connectors.

A mounted folder can *tell* this appliance that a file changed; no API-backed connector
can. That is the whole reason this component exists, and since
:mod:`knowledge_index.sync.scheduler` took over the timetable for every source, it is the
only reason. The watcher's job is latency — a partner drops a signed contract into a
matter folder and it is queued within a second — not "keeping local folders current",
which is now the same mechanism that keeps SharePoint current.

**Why the interval reconcile moved out.** OS filesystem events are advisory and lossy:
macOS FSEvents coalesces bursts and sets ``MustScanSubDirs`` when it drops events;
watchdog/notify can report a move as create+delete. So the event stream is never the
source of truth, and a periodic full scan has to run regardless. That periodic scan used
to be this loop's timer, which made local folders the only kind of source with a
timetable at all — and meant two components would now be enqueuing the same folder on two
different clocks. The safety-net reconcile is unchanged in substance; it is simply the
scheduler's tick, on the same ``sync_policy.interval``, for every kind of source.

Both paths hand work to :func:`knowledge_index.sync.runs.enqueue_sync`, so an event-driven
sync, a scheduled one and an operator's click are the same orchestrated run in the same
ledger and cannot overlap each other. The run's own handoff starts insertion, so a
monitored folder still flows all the way to the index without anyone intervening.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import Source
from knowledge_index.sync.runs import enqueue_sync
from knowledge_index.sync.scheduler import is_continuous

WATCHED_KINDS = ("local_fs", "plugin_drop")
# How long to watch before reloading the source list. Not a sync interval — the syncs are
# the scheduler's — just how quickly a folder added in the UI starts being watched.
RELOAD_SECONDS = 30.0
DEBOUNCE_MS = 500


def _log(message: str) -> None:
    print(f"[ki watch] {message}", file=sys.stderr, flush=True)


class _WatchedSource:
    __slots__ = ("root", "source_id")

    def __init__(self, source_id: str, root: Path) -> None:
        self.source_id = source_id
        self.root = root


def _load_watched_sources(session_factory: sessionmaker) -> list[_WatchedSource]:
    watched: list[_WatchedSource] = []
    with session_factory() as session:
        sources = session.scalars(
            select(Source).where(
                Source.provider == "native",
                Source.kind.in_(WATCHED_KINDS),
                Source.status != "paused",
            )
        ).all()
        for source in sources:
            policy = source.sync_policy or {}
            # Only continuous sources are monitored; "manual" ones sync on demand.
            if not is_continuous(policy):
                continue
            root_value = (source.config or {}).get("root")
            if not root_value:
                continue
            root = Path(root_value)
            if not root.is_dir():
                _log(f"source {source.id} root not present in this container: {root}")
                continue
            watched.append(_WatchedSource(source.id, root.resolve()))
    return watched


def _sources_for_changes(
    watched: list[_WatchedSource], changed_paths: set[str]
) -> set[str]:
    affected: set[str] = set()
    resolved = [Path(path) for path in changed_paths]
    for source in watched:
        for path in resolved:
            if path == source.root or _is_relative_to(path, source.root):
                affected.add(source.source_id)
                break
    return affected


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except AttributeError:  # pragma: no cover - Python <3.9 safety net
        return str(path).startswith(str(root))


def _enqueue_sync(
    session_factory: sessionmaker,
    config_getter: Callable[[], AppConfig],
    source_ids: set[str],
) -> None:
    """Hand the changed sources to the same orchestrated path the sync button uses.

    The watcher used to scan inline and trigger insertion itself, which made a
    watcher-driven sync a different thing from an operator-driven one: no run row, no
    progress, no record that it happened. It is now the same thing, and a source that is
    already syncing — because an operator clicked, or because it was due — is skipped
    instead of scanned twice.
    """
    if not source_ids:
        return
    result = enqueue_sync(
        session_factory, config_getter(), source_ids=source_ids, trigger="watch"
    )
    for run in result.runs:
        _log(f"queued sync for source {run.source_id} (run {run.run_id})")
    for skipped in result.skipped:
        _log(f"source {skipped.source_id} not queued: {skipped.reason}")


def run_watch_loop(
    session_factory: sessionmaker,
    config_getter: Callable[[], AppConfig],
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Monitor local source roots until ``stop_event`` is set (or forever)."""
    from watchfiles import watch

    external_stop = stop_event or threading.Event()
    _log("watcher started")
    while not external_stop.is_set():
        watched = _load_watched_sources(session_factory)
        if not watched:
            # Nothing to watch yet; check back for newly added sources.
            if external_stop.wait(RELOAD_SECONDS):
                break
            continue

        roots = [str(source.root) for source in watched]
        _log(f"watching {len(roots)} source root(s); reloading every {RELOAD_SECONDS:.0f}s")

        # Break out of watch() periodically to pick up sources added or removed in the UI.
        cycle_stop = threading.Event()
        timer = threading.Timer(RELOAD_SECONDS, cycle_stop.set)
        timer.daemon = True
        timer.start()

        try:
            for changes in watch(
                *roots,
                stop_event=cycle_stop,
                debounce=DEBOUNCE_MS,
                rust_timeout=int(RELOAD_SECONDS * 1000),
                yield_on_timeout=True,
                raise_interrupt=False,
            ):
                if external_stop.is_set():
                    break
                if not changes:  # timeout heartbeat — the outer loop reloads the sources
                    continue
                changed_paths = {path for _change, path in changes}
                affected = _sources_for_changes(watched, changed_paths)
                if affected:
                    _enqueue_sync(session_factory, config_getter, affected)
        finally:
            timer.cancel()
        # loop reloads sources on the next iteration (picks up adds/removes)
    _log("watcher stopped")
