---
title: Local folders
description: Index a mounted directory read-only, with permissions supplied by an ACL map.
---

The local-folder source indexes any directory the appliance can see — a local
path, a mounted NAS share, or an exported DMS directory. No app registration,
no OAuth.

| | |
| --- | --- |
| Syncs | Files and folders under a root path |
| Incremental | Watch + reconcile (continuous monitoring) |
| Permission mirror | Via an ACL map file, or a default ACL |
| Auth | None — filesystem access |

## What the appliance can see

In the Compose deployment, the host filesystem is mounted **read-only** into
the containers; `KI_LOCAL_MOUNT` in `.env` controls which directory is
exposed. The read-only mount is enforced by Docker, not by convention — the
shadow-index guarantee holds even against a bug.

## Add a folder

From **Connectors → Files from this computer**, pick the root directory. Or
from the CLI:

```bash
docker compose exec app ki add-source /path/to/estate \
  --name "Estate" --acl-map /path/to/acl-by-path.json
```

Configuration:

- `root` — the directory to index.
- `acl_by_path` — optional JSON mapping paths to principals, so filesystem
  content gets real permissions in the index (the demo estate ships one as
  `acl-by-path.json`).
- `default_acl` — the ACL applied where the map says nothing. Without any ACL
  information, documents fall under the unknown-ACL policy, which fails
  closed.

## Sync behaviour

Local folders are continuously monitored — a filesystem watcher picks up
changes between scheduled reconciliation scans. Everything else behaves like
any other connection: deletion confirmation, tombstones, and handoff to the
[insertion pipeline](/product/pipeline/).

For customer DMS exports with their own observation format, see the
[plugin connector contract](/development/plugin-connectors/).
