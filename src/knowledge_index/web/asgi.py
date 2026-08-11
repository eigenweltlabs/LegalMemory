"""Importable ASGI application, for serving with more than one worker.

``uvicorn.run(app_object)`` runs one process. That is the right shape for a
laptop and the wrong one for an appliance answering agents: retrieval is
CPU-bound Python — normalizing, grouping, building dicts — so one process is
one GIL, and measured against twenty concurrent agents the container sat at
114% of a single core while thirty-one others idled and every tool call
queued behind the others (``search_semantic`` 0.78s alone, 36s under that
load).

Serving N workers means uvicorn forks children that IMPORT the application
rather than inheriting an object, which is what this module is for. It builds
the app the same way the CLI does and deliberately starts no background
schedulers: the sync scheduler, the event manager and the nightly backup
belong to one process, not to every worker. ``ki serve`` keeps them in the
parent.
"""

from __future__ import annotations

from knowledge_index.web.app import create_app

app = create_app()
