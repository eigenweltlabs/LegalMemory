---
title: Plugin connectors
description: The drop-directory contract for integrating customer DMS systems (RA-MICRO, AnNoText, Advoware, in-house exports) without changing the product.
---

This is a developer integration contract, not an alternative folder picker
for ordinary operators. It is intentionally hidden from the normal connector
catalog; register it through the API when shipping a custom DMS adapter.

## “Files from this computer” versus a connector plugin

They are different audiences, not two competing ways to choose a folder:

- **Files from this computer** is the normal built-in connector. An administrator
  selects an existing mounted folder and the appliance reads the files directly.
- A **connector plugin** is developer infrastructure for a source the product does not
  natively understand. A customer-specific exporter writes a machine-owned drop
  directory in the schema below; operators never browse or manage that directory.

Therefore the ordinary UI says **Files from this computer** and does not show “plugin
directory”. The latter term belongs only in this developer document and the source API.

This is the official, zero-repo-change way to integrate a customer's document
management system (RA-MICRO, AnNoText, Advoware, or an in-house SQL export) into
the index. An integrator ships a small standalone
script that reads the DMS and writes a **drop directory** in a versioned schema.
The `plugin_drop` connector reads that directory. No connector code lands in this
repo per customer — the script is the customer-specific part, and it copies from
one reference template.

Plugin scripts emit rows in one shared schema and everything downstream — sync,
dedup, ACL compilation, extraction, retrieval — works unchanged.

## Lifecycle

```
customer DMS  --(plugin script)-->  drop directory  --(plugin_drop connector)-->  SyncEngine
```

1. The plugin script walks/queries the DMS and writes a drop directory.
2. A `Source` of kind `plugin_drop` with `config.root` pointing at that directory
   is registered.
3. `SyncEngine.sync()` runs `full_scan()`, creating/updating `SourceObject` rows,
   mirroring each object's ACL into `SourceObjectGrant`, and tombstoning anything
   no longer present.
4. Re-run the plugin script (a cron job, a webhook, or on demand) to refresh, then
   sync again. The drop directory is a full snapshot each time.

## Drop directory layout

```
<root>/
  observations.jsonl     one JSON object per line, first field "schema"
  files/                 content bytes, referenced by each row's content_file
```

## observation schema — `ki-plugin-observation/v1`

One JSON object per line in `observations.jsonl`. The first field **must** be the
schema marker; an unknown or missing marker aborts the sync and tells the integrator to
regenerate the drop directory with a plugin that targets this connector version.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `schema` | string | yes | Must equal `ki-plugin-observation/v1`. |
| `external_id` | string | yes | Stable id in the source system. Survives rename/move. Unit of sync. |
| `path` | string | yes | Human-readable location (e.g. `Mandate/AKTE-2026-42/Vertrag.pdf`). |
| `name` | string | yes | Display name / filename. |
| `content_file` | string | yes (unless `deleted`) | Path **relative to `<root>/files/`** holding the bytes. Must not escape `files/`. |
| `mime_type` | string | no | Content type. Sniffed downstream if absent. |
| `size_bytes` | integer | no | Byte size. |
| `mtime` | string | no | ISO-8601 timestamp. Naive values are treated as UTC. |
| `acl` | list | no | Grants; see below. **Absent means unknown.** |
| `deleted` | bool | no | `true` marks an explicit tombstone (see lifecycle). |

### ACL entries

Each `acl` element is `{"principal": ..., "principal_kind": ..., "access": ...}`:

| Field | Meaning |
|---|---|
| `principal` | e.g. `group:litigation`, `user:anna@kanzlei.de`, `role:authenticated`. |
| `principal_kind` | `group` \| `user` \| `role`. Defaults to `group`. |
| `access` | `allow` \| `deny`. |

**ACL semantics — absent means deny-by-default.** If a row omits `acl`, the object's
access is *unknown*, and the engine's policy applies: external connectors are
fail-closed, so an object with no mirrored grant is not readable through any local
project/document grant (see `permissions.version_predicate`). To make an object
readable, the plugin must emit at least one `allow` grant. Deny wins over allow at
every scope. Grants are re-mirrored on every sync, so revoking access in the DMS
and re-running removes the grant.

## Tombstones

The drop directory is a full snapshot. On each `full_scan`:

- An object **present** (and not `deleted`) is created or updated.
- An object **absent** from the snapshot is tombstoned by the engine's diffing.
- An object emitted with `"deleted": true` is treated exactly as absent — it is not
  yielded, so it tombstones on the next sync. Use it when it is easier for the
  plugin to emit an explicit delete row than to omit the object.

## Failure behavior — fail loudly

A plugin bug must never half-sync a customer's index silently:

- A malformed JSONL line raises `ValueError` naming the 1-based line number
  (`observations.jsonl line 7: ...`).
- A missing/unknown `schema` marker raises `ValueError` telling the integrator to
  regenerate/upgrade.
- A `content_file` that escapes `<root>/files/` (absolute, `..`, or through a
  symlink) is rejected on fetch.
- A missing `observations.jsonl` or a missing content file raises rather than
  producing an empty or partial sync.

## Reference plugin and conformance

`examples/plugins/reference_export.py` is the template to copy per customer. It
walks an input folder and writes a conformant drop directory (stdlib only, ~80
lines); swap the folder walk for the DMS export and keep the output shape.

Produce a drop directory:

```bash
python examples/plugins/reference_export.py <input_dir> <out_dir> --group group:kanzlei
```

Run the conformance harness (real Postgres on `localhost:5439`) to prove a plugin's
output syncs end to end — schema, ACLs, tombstones, path-escape rejection, and the
reference export itself:

```bash
.venv/bin/pytest tests/test_plugin_source.py -q
```

## Wiring

Register a source of kind `plugin_drop` with `config = {"root": "<abs path to drop dir>"}`.
The runner's `connector_from_source` constructs `PluginDropSource(config["root"])`.
Examples named as the canonical long-tail targets: **RA-MICRO**, **AnNoText**,
**Advoware**.

## Optional live-event adapter for a native connector

The drop-directory contract above is snapshot-based. A native connector that has a
provider event system can additionally register:

```python
ConnectorSpec(
    ...,
    incremental=True,
    event_adapter="customer_connector.events:CustomerEventAdapter",
)
```

`CustomerEventAdapter` implements `ConnectorEventAdapter.desired/create/renew/delete`.
It maps the current source scope to provider subscription targets. The shared event
manager persists lifecycle/error state and expiry, renews subscriptions, consumes the
configured outbound broker, coalesces duplicate wakes, and enqueues the normal
`source-sync`. The adapter must not insert objects from an event payload: it wakes the
connector's durable delta cursor, which remains the only source of change truth.
