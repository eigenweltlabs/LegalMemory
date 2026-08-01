#!/usr/bin/env bash
# Restore every stateful component that belongs to an insertion snapshot.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

usage() {
  cat <<'EOF'
Usage: scripts/restore-insertion.sh SNAPSHOT_DIRECTORY --yes

This replaces the current Knowledge Index DB, Hatchet DB/queue, managed source and
artifact volume, Hatchet config, and OpenSearch index. LiteLLM spend and Keycloak
users are deliberately not changed.
EOF
}

if [[ $# -ne 2 || "${2:-}" != "--yes" ]]; then
  usage >&2
  exit 2
fi

for command_name in docker jq shasum curl; do
  command -v "$command_name" >/dev/null || {
    echo "Required command is missing: $command_name" >&2
    exit 1
  }
done

snapshot_dir="$(cd "$1" && pwd)"
required=(
  manifest.json SHA256SUMS ki.dump hatchet.dump appdata.tar.gz
  opensearch.tar.gz hatchet-config.tar.gz litellm-config.yaml
)
for name in "${required[@]}"; do
  [[ -f "$snapshot_dir/$name" ]] || {
    echo "Snapshot is incomplete; missing $name" >&2
    exit 1
  }
done

[[ "$(jq -r '.format_version' "$snapshot_dir/manifest.json")" == "1" ]] || {
  echo "Unsupported snapshot format" >&2
  exit 1
}
(
  cd "$snapshot_dir"
  shasum -a 256 -c SHA256SUMS
)

echo "Stopping all services that can read or mutate insertion state ..."
docker compose stop -t 60 worker watcher app 2>/dev/null || true
docker compose stop -t 30 hatchet opensearch 2>/dev/null || true
docker compose up -d postgres hatchet-postgres >/dev/null
docker compose create app opensearch hatchet >/dev/null

app_container="$(docker compose ps -aq app | head -1)"
opensearch_container="$(docker compose ps -aq opensearch | head -1)"
hatchet_container="$(docker compose ps -aq hatchet | head -1)"
postgres_container="$(docker compose ps -q postgres)"
hatchet_postgres_container="$(docker compose ps -q hatchet-postgres)"

appdata_volume="$(docker inspect "$app_container" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
opensearch_volume="$(docker inspect "$opensearch_container" --format '{{range .Mounts}}{{if eq .Destination "/usr/share/opensearch/data"}}{{.Name}}{{end}}{{end}}')"
hatchet_config_volume="$(docker inspect "$hatchet_container" --format '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Name}}{{end}}{{end}}')"
helper_image="$(docker inspect "$app_container" --format '{{.Config.Image}}')"

for value in "$appdata_volume" "$opensearch_volume" "$hatchet_config_volume"; do
  [[ -n "$value" ]] || {
    echo "Could not resolve an exact compose volume; refusing destructive restore." >&2
    exit 1
  }
done

echo "Restoring Knowledge Index PostgreSQL ..."
docker cp "$snapshot_dir/ki.dump" "$postgres_container:/tmp/ki-restore.dump"
docker exec "$postgres_container" dropdb -U ki --if-exists --force ki
docker exec "$postgres_container" createdb -U ki -O ki ki
docker exec "$postgres_container" pg_restore -U ki -d ki --no-owner --no-privileges /tmp/ki-restore.dump
docker exec "$postgres_container" rm -f /tmp/ki-restore.dump

echo "Restoring Hatchet PostgreSQL and durable queue ..."
docker cp "$snapshot_dir/hatchet.dump" "$hatchet_postgres_container:/tmp/hatchet-restore.dump"
docker exec "$hatchet_postgres_container" dropdb -U hatchet --if-exists --force hatchet
docker exec "$hatchet_postgres_container" createdb -U hatchet -O hatchet hatchet
docker exec "$hatchet_postgres_container" pg_restore -U hatchet -d hatchet --no-owner --no-privileges /tmp/hatchet-restore.dump
docker exec "$hatchet_postgres_container" rm -f /tmp/hatchet-restore.dump

restore_volume() {
  local volume_name="$1"
  local archive_name="$2"
  echo "Restoring $volume_name from $archive_name ..."
  docker run --rm --user 0 --entrypoint sh \
    -v "$volume_name:/target" -v "$snapshot_dir:/backup:ro" "$helper_image" -c \
    'find /target -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && tar -xzf "/backup/'"$archive_name"'" -C /target'
}

restore_volume "$appdata_volume" appdata.tar.gz
restore_volume "$opensearch_volume" opensearch.tar.gz
restore_volume "$hatchet_config_volume" hatchet-config.tar.gz

echo "Starting restored infrastructure ..."
docker compose up -d opensearch hatchet >/dev/null
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:9200/_cluster/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS http://localhost:9200/_cluster/health >/dev/null

# Client tokens are secrets and are not stored in snapshots. Rotate one against the
# restored Hatchet DB/config instead, then recreate clients with that token.
scripts/bootstrap-hatchet.sh --rotate >/dev/null
docker compose up -d --force-recreate app worker watcher >/dev/null

for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8000/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS http://localhost:8000/healthz >/dev/null

expected_source_objects="$(jq -r '.counts.source_objects' "$snapshot_dir/manifest.json")"
expected_indexed_objects="$(jq -r '.counts.indexed_objects' "$snapshot_dir/manifest.json")"
expected_managed_files="$(jq -r '.counts.managed_source_files' "$snapshot_dir/manifest.json")"
expected_artifacts="$(jq -r '.counts.artifact_files' "$snapshot_dir/manifest.json")"
expected_opensearch_documents="$(jq -r '.counts.opensearch_documents' "$snapshot_dir/manifest.json")"

actual_source_objects="$(docker compose exec -T postgres psql -U ki -d ki -Atc "SELECT count(*) FROM source_objects WHERE deleted_at IS NULL;")"
actual_indexed_objects="$(docker compose exec -T postgres psql -U ki -d ki -Atc "SELECT count(*) FROM processing_state WHERE stage='index' AND status='done';")"
actual_managed_files="$(docker compose exec -T app sh -c "find /data/browser-sources -type f | wc -l | tr -d ' '")"
actual_artifacts="$(docker compose exec -T app sh -c "find /data/artifacts -type f | wc -l | tr -d ' '")"
actual_opensearch_documents="$(curl -fsS http://localhost:9200/knowledge-index-chunks-v1/_count | jq -r '.count')"
unfinished="$(docker compose exec -T postgres psql -U ki -d ki -Atc "SELECT count(*) FROM processing_state WHERE status IN ('pending','running','failed');")"

assert_equal() {
  local label="$1" expected="$2" actual="$3"
  [[ "$expected" == "$actual" ]] || {
    echo "Restore verification failed for $label: expected $expected, got $actual" >&2
    exit 1
  }
}
assert_equal source_objects "$expected_source_objects" "$actual_source_objects"
assert_equal indexed_objects "$expected_indexed_objects" "$actual_indexed_objects"
assert_equal managed_source_files "$expected_managed_files" "$actual_managed_files"
assert_equal artifact_files "$expected_artifacts" "$actual_artifacts"
assert_equal opensearch_documents "$expected_opensearch_documents" "$actual_opensearch_documents"
assert_equal unfinished 0 "$unfinished"

echo "Restore complete and verified: $snapshot_dir"

