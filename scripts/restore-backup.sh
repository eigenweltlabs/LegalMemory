#!/usr/bin/env bash
# Restore a full-appliance backup, including the two volumes that need the stack stopped.
#
# `ki backup-restore` handles everything reachable over a protocol — the databases, the
# search index, the file stores — and it can do that while the appliance is running.
# Keycloak's data volume and Hatchet's config volume cannot: replacing a container's
# volume means stopping that container, and a process inside the stack cannot stop the
# stack it is running in. That is the gap this script exists to close, and it is why a
# whole-appliance restore is an operator running a script rather than a button in the UI.
#
# What it does, in order: stop everything that writes, stage and verify the backup,
# replace the two volumes, restore the databases, restore the search index, put the file
# stores back, and start the stack. Nothing is destroyed before the backup has been read
# back in full and checksummed — a damaged backup stops the restore while the appliance is
# still the way it was.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

usage() {
  cat <<'EOF'
Usage: scripts/restore-backup.sh BACKUP_ID --yes [--stage-to DIR]

Restores a backup written by the appliance's backup feature. BACKUP_ID is the id shown
under Backup in the admin UI or by `ki backup-list`, e.g. ki-backup-20260728T020000Z.

This REPLACES the current Knowledge Index database, the LiteLLM and Langfuse databases,
Hatchet's database and config, the OpenSearch index, the artifact and upload stores, and
Keycloak's users and realm keys. It cannot be undone.

Before running it, make sure the deployment's own secrets — above all
KI_CONNECTOR_CREDENTIAL_KEY — are the ones this backup was taken under. They are in the
backup's secrets/environment component; the restore refuses to proceed if the connector
key does not match, because a mismatch leaves every stored OAuth token undecryptable.

  --stage-to DIR   where to download and verify into (default: runtime/restore/BACKUP_ID).
                   Needs room for the largest single component.
EOF
}

backup_id="${1:-}"
confirm="${2:-}"
if [[ -z "$backup_id" || "$confirm" != "--yes" ]]; then
  usage >&2
  exit 2
fi
shift 2

stage_to="$root/runtime/restore/$backup_id"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stage-to) stage_to="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for command_name in docker jq; do
  command -v "$command_name" >/dev/null || {
    echo "Required command is missing: $command_name" >&2
    exit 1
  }
done

echo "==> Checking whether this backup can become this appliance"
# The plan reports blockers — a wrong backup key, a mismatched connector credential key —
# before anything is stopped, so a restore that cannot work costs no downtime.
docker compose up -d postgres >/dev/null
plan="$(docker compose run --rm --no-deps -T app \
  python -c "
import json, sys
from knowledge_index.backup import restore
from knowledge_index.config_store import ConfigStore
import os
config = ConfigStore(os.environ.get('KI_CONFIG_PATH', '/data/config.json')).get()
print(json.dumps(restore.restore_plan(config, sys.argv[1])))
" "$backup_id")"

if [[ "$(jq -r '.ok' <<<"$plan")" != "true" ]]; then
  echo "Refusing to restore:" >&2
  jq -r '.blockers[]' <<<"$plan" | sed 's/^/  - /' >&2
  exit 1
fi
jq -r '.warnings[]?' <<<"$plan" | sed 's/^/  warning: /' >&2

echo "==> Stopping everything that writes"
docker compose stop -t 60 worker watcher app 2>/dev/null || true
docker compose stop -t 30 hatchet opensearch keycloak litellm langfuse 2>/dev/null || true

echo "==> Staging and verifying the backup into $stage_to"
mkdir -p "$stage_to"
# Staging first, and separately: it decrypts and re-checks every component against the
# manifest without touching anything. If the backup is damaged, this is where the restore
# stops — with the appliance still intact.
docker compose run --rm --no-deps \
  -v "$stage_to:/restore" \
  app ki backup-restore "$backup_id" --stage-to /restore

echo "==> Replacing the volumes that need the stack stopped"
restore_volume() {
  local volume_name="$1" archive="$2"
  if [[ ! -f "$stage_to/$archive" ]]; then
    echo "  $volume_name: not in this backup, left as it is"
    return
  fi
  echo "  $volume_name <- $archive"
  # A helper container so the volume can be replaced without its owner running. The
  # contents are cleared first: extracting over a live volume merges two appliances'
  # state, which for Keycloak means stale realm keys beside the restored ones.
  docker run --rm \
    -v "knowledge-index_${volume_name}:/target" \
    -v "$stage_to:/staged:ro" \
    alpine:3 sh -c "rm -rf /target/* /target/.[!.]* 2>/dev/null; tar -xzf /staged/$archive -C /target"
}
restore_volume keycloak_data volumes-keycloak.tar.gz
restore_volume hatchet_config volumes-hatchet-config.tar.gz

echo "==> Restoring the databases, the search index and the file stores"
docker compose up -d postgres hatchet-postgres opensearch >/dev/null
# --reuse-staged, because everything here was already downloaded, decrypted and checksummed
# by the staging step above. Without it this transfers and decrypts the whole estate a
# second time, which on a NAS or an S3 endpoint is the longest part of the restore, paid
# twice on the day it matters most. Each component is still re-hashed against the manifest
# before it is applied.
docker compose run --rm \
  -v "$stage_to:/restore" \
  app ki backup-restore "$backup_id" \
    --stage-to /restore --reuse-staged \
    --apply-databases --apply-search-index --apply-files \
    --i-understand-this-destroys-current-data

echo "==> Starting the stack"
docker compose up -d

cat <<EOF

Restore complete from $backup_id.

Two things still need a human:

  1. Hatchet's client token. The restored config volume makes the old token valid again;
     if it was rotated since this backup, re-mint it with scripts/bootstrap-hatchet.sh.
  2. The deployment secrets in $stage_to/environment.json. Compare them against this
     deployment's .env and place any that differ — KI_CONNECTOR_CREDENTIAL_KEY above all.

Then check the appliance agrees: open the admin UI, confirm the source list and the
document counts, and run one search.
EOF
