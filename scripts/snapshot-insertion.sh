#!/usr/bin/env bash
# Capture a completed insertion so model-comparison runs can be restored exactly.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

usage() {
  cat <<'EOF'
Usage: scripts/snapshot-insertion.sh [SNAPSHOT_DIRECTORY]

The default destination is runtime/snapshots/insertion-<UTC timestamp>.
Set KI_SNAPSHOT_ROOT to choose a different default parent directory.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -gt 1 ]]; then
  usage
  [[ $# -le 1 ]] || exit 2
  exit 0
fi

for command_name in docker jq curl shasum; do
  command -v "$command_name" >/dev/null || {
    echo "Required command is missing: $command_name" >&2
    exit 1
  }
done

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
snapshot_root="${KI_SNAPSHOT_ROOT:-$root/runtime/snapshots}"
snapshot_dir="${1:-$snapshot_root/insertion-$timestamp}"
mkdir -p "$snapshot_dir"
snapshot_dir="$(cd "$snapshot_dir" && pwd)"
if find "$snapshot_dir" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  echo "Snapshot directory is not empty: $snapshot_dir" >&2
  exit 1
fi

docker compose up -d postgres hatchet-postgres >/dev/null

unfinished="$(docker compose exec -T postgres psql -U ki -d ki -Atc \
  "SELECT count(*) FROM processing_state WHERE status IN ('pending','running','failed');")"
unexpected_quarantines="$(docker compose exec -T postgres psql -U ki -d ki -Atc \
  "SELECT count(*) FROM processing_state WHERE status='quarantined' AND NOT (stage='convert' AND last_error->>'class'='UnsupportedDocument');")"
if [[ "$unfinished" != "0" || "$unexpected_quarantines" != "0" ]]; then
  echo "Refusing to snapshot an unsettled insertion: unfinished=$unfinished unexpected_quarantines=$unexpected_quarantines" >&2
  exit 1
fi

app_container="$(docker compose ps -q app)"
worker_container="$(docker compose ps -q worker)"
opensearch_container="$(docker compose ps -q opensearch)"
hatchet_container="$(docker compose ps -q hatchet)"
postgres_container="$(docker compose ps -q postgres)"
hatchet_postgres_container="$(docker compose ps -q hatchet-postgres)"
for container_id in "$app_container" "$worker_container" "$opensearch_container" "$hatchet_container" "$postgres_container" "$hatchet_postgres_container"; do
  [[ -n "$container_id" ]] || {
    echo "The full compose stack must exist before taking a snapshot." >&2
    exit 1
  }
done

appdata_volume="$(docker inspect "$app_container" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
opensearch_volume="$(docker inspect "$opensearch_container" --format '{{range .Mounts}}{{if eq .Destination "/usr/share/opensearch/data"}}{{.Name}}{{end}}{{end}}')"
hatchet_config_volume="$(docker inspect "$hatchet_container" --format '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Name}}{{end}}{{end}}')"
helper_image="$(docker inspect "$app_container" --format '{{.Config.Image}}')"

db_counts="$(docker compose exec -T postgres psql -U ki -d ki -Atc \
  "SELECT json_build_object(
      'source_objects', (SELECT count(*) FROM source_objects WHERE deleted_at IS NULL),
      'documents', (SELECT count(*) FROM documents),
      'indexed_objects', (SELECT count(*) FROM processing_state WHERE stage='index' AND status='done'),
      'chunks', (SELECT count(*) FROM chunks),
      'unfinished', (SELECT count(*) FROM processing_state WHERE status IN ('pending','running','failed')),
      'unexpected_quarantines', (SELECT count(*) FROM processing_state WHERE status='quarantined' AND NOT (stage='convert' AND last_error->>'class'='UnsupportedDocument')),
      'unsupported_documents', (SELECT count(*) FROM processing_state WHERE stage='convert' AND status='quarantined' AND last_error->>'class'='UnsupportedDocument')
  );")"

managed_source_files="$(docker run --rm --user 0 -v "$appdata_volume:/data:ro" --entrypoint sh "$helper_image" -c \
  "find /data/browser-sources -type f 2>/dev/null | wc -l | tr -d ' '")"
artifact_files="$(docker run --rm --user 0 -v "$appdata_volume:/data:ro" --entrypoint sh "$helper_image" -c \
  "find /data/artifacts -type f 2>/dev/null | wc -l | tr -d ' '")"
# Record the effective runtime configuration. Deployments backed by the database do
# not necessarily have the legacy /data/config.json file in the appdata volume.
models="$(curl -fsS -H 'X-KI-Principals: role:admin' \
  'http://127.0.0.1:8000/api/config' | jq -c '.models // {}')"
opensearch_documents="$(curl -fsS 'http://localhost:9200/knowledge-index-chunks-v1/_count' | jq -r '.count')"
hatchet_tables="$(docker compose exec -T hatchet-postgres psql -U hatchet -d hatchet -Atc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")"

paused=0
resume_stack() {
  if [[ "$paused" == "1" ]]; then
    echo "Restarting the application stack ..."
    docker compose up -d opensearch hatchet app worker watcher >/dev/null
  fi
}
trap resume_stack EXIT

echo "Pausing write-producing services ..."
docker compose stop -t 60 worker watcher app >/dev/null
docker compose stop -t 30 hatchet opensearch >/dev/null
paused=1

echo "Dumping Knowledge Index PostgreSQL ..."
docker compose exec -T postgres pg_dump -U ki -d ki --format=custom --compress=6 > "$snapshot_dir/ki.dump"

echo "Dumping Hatchet PostgreSQL ..."
docker compose exec -T hatchet-postgres pg_dump -U hatchet -d hatchet --format=custom --compress=6 > "$snapshot_dir/hatchet.dump"

echo "Archiving managed sources and converted artifacts ..."
docker run --rm --user 0 --entrypoint tar \
  -v "$appdata_volume:/source:ro" -v "$snapshot_dir:/backup" "$helper_image" \
  -czf /backup/appdata.tar.gz -C /source .

echo "Archiving OpenSearch ..."
docker run --rm --user 0 --entrypoint tar \
  -v "$opensearch_volume:/source:ro" -v "$snapshot_dir:/backup" "$helper_image" \
  -czf /backup/opensearch.tar.gz -C /source .

echo "Archiving Hatchet configuration ..."
docker run --rm --user 0 --entrypoint tar \
  -v "$hatchet_config_volume:/source:ro" -v "$snapshot_dir:/backup" "$helper_image" \
  -czf /backup/hatchet-config.tar.gz -C /source .

cp deploy/litellm/config.yaml "$snapshot_dir/litellm-config.yaml"

git_commit="$(git rev-parse HEAD)"
git_dirty=false
[[ -z "$(git status --porcelain)" ]] || git_dirty=true
created_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
worker_slots="$(docker inspect "$worker_container" --format '{{json .Config.Cmd}}' | jq -r '.[-1]')"
document_concurrency="$(docker inspect "$worker_container" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^KI_HATCHET_DOCUMENT_CONCURRENCY=//p')"
relation_concurrency="$(docker inspect "$worker_container" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^KI_RELATE_MODEL_CONCURRENCY=//p')"

jq -n \
  --argjson format_version 1 \
  --arg created_at "$created_at" \
  --arg git_commit "$git_commit" \
  --argjson git_dirty "$git_dirty" \
  --argjson db "$db_counts" \
  --argjson models "$models" \
  --argjson managed_source_files "$managed_source_files" \
  --argjson artifact_files "$artifact_files" \
  --argjson opensearch_documents "$opensearch_documents" \
  --argjson hatchet_tables "$hatchet_tables" \
  --argjson worker_slots "$worker_slots" \
  --argjson document_concurrency "$document_concurrency" \
  --argjson relation_concurrency "$relation_concurrency" \
  --arg app_image "$(docker inspect "$app_container" --format '{{.Config.Image}}')" \
  --arg app_image_id "$(docker inspect "$app_container" --format '{{.Image}}')" \
  --arg postgres_image "$(docker inspect "$postgres_container" --format '{{.Config.Image}}')" \
  --arg opensearch_image "$(docker inspect "$opensearch_container" --format '{{.Config.Image}}')" \
  --arg hatchet_image "$(docker inspect "$hatchet_container" --format '{{.Config.Image}}')" \
  --arg hatchet_postgres_image "$(docker inspect "$hatchet_postgres_container" --format '{{.Config.Image}}')" \
  '{
    format_version: $format_version,
    created_at: $created_at,
    git: {commit: $git_commit, dirty: $git_dirty},
    counts: ($db + {
      managed_source_files: $managed_source_files,
      artifact_files: $artifact_files,
      opensearch_documents: $opensearch_documents,
      hatchet_tables: $hatchet_tables
    }),
    concurrency: {
      worker_slots: $worker_slots,
      document: $document_concurrency,
      relation: $relation_concurrency
    },
    models: $models,
    images: {
      app: {name: $app_image, id: $app_image_id},
      postgres: {name: $postgres_image},
      opensearch: {name: $opensearch_image},
      hatchet: {name: $hatchet_image},
      hatchet_postgres: {name: $hatchet_postgres_image}
    }
  }' > "$snapshot_dir/manifest.json"

(
  cd "$snapshot_dir"
  shasum -a 256 \
    ki.dump hatchet.dump appdata.tar.gz opensearch.tar.gz hatchet-config.tar.gz \
    litellm-config.yaml manifest.json > SHA256SUMS
)

resume_stack
paused=0
trap - EXIT

echo "Snapshot created: $snapshot_dir"
echo "Verify it with: scripts/verify-insertion-snapshot.sh '$snapshot_dir'"
