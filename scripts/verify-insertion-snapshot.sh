#!/usr/bin/env bash
# Prove that a snapshot imports into isolated temporary services without touching live data.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [[ $# -ne 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: scripts/verify-insertion-snapshot.sh SNAPSHOT_DIRECTORY" >&2
  exit 2
fi
for command_name in docker jq shasum; do
  command -v "$command_name" >/dev/null || {
    echo "Required command is missing: $command_name" >&2
    exit 1
  }
done

snapshot_dir="$(cd "$1" && pwd)"
(
  cd "$snapshot_dir"
  shasum -a 256 -c SHA256SUMS
)

suffix="$(date '+%s')-$$"
prefix="ki-snapshot-verify-$suffix"
network="$prefix-network"
pg_container="$prefix-postgres"
hatchet_pg_container="$prefix-hatchet-postgres"
opensearch_container="$prefix-opensearch"
hatchet_container="$prefix-hatchet"
app_container="$prefix-app"
pg_volume="$prefix-pgdata"
hatchet_pg_volume="$prefix-hatchet-pgdata"
appdata_volume="$prefix-appdata"
opensearch_volume="$prefix-opensearch-data"
hatchet_config_volume="$prefix-hatchet-config"

cleanup() {
  docker rm -f "$app_container" "$hatchet_container" "$opensearch_container" "$hatchet_pg_container" "$pg_container" >/dev/null 2>&1 || true
  docker volume rm "$hatchet_config_volume" "$opensearch_volume" "$appdata_volume" "$hatchet_pg_volume" "$pg_volume" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT

postgres_image="$(jq -r '.images.postgres.name' "$snapshot_dir/manifest.json")"
hatchet_postgres_image="$(jq -r '.images.hatchet_postgres.name' "$snapshot_dir/manifest.json")"
opensearch_image="$(jq -r '.images.opensearch.name' "$snapshot_dir/manifest.json")"
hatchet_image="$(jq -r '.images.hatchet.name' "$snapshot_dir/manifest.json")"
app_image="$(jq -r '.images.app.name' "$snapshot_dir/manifest.json")"

docker network create "$network" >/dev/null
for volume_name in "$pg_volume" "$hatchet_pg_volume" "$appdata_volume" "$opensearch_volume" "$hatchet_config_volume"; do
  docker volume create "$volume_name" >/dev/null
done

echo "Restoring the Knowledge Index DB into an isolated PostgreSQL ..."
docker run -d --name "$pg_container" --network "$network" --network-alias postgres \
  -e POSTGRES_USER=ki -e POSTGRES_PASSWORD=verify-only -e POSTGRES_DB=ki \
  -v "$pg_volume:/var/lib/postgresql/data" -v "$snapshot_dir:/snapshot:ro" \
  "$postgres_image" >/dev/null
for _ in $(seq 1 60); do
  docker exec "$pg_container" pg_isready -U ki -d ki >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$pg_container" pg_isready -U ki -d ki >/dev/null
docker exec "$pg_container" pg_restore -U ki -d ki --no-owner --no-privileges /snapshot/ki.dump

echo "Restoring Hatchet DB/config into isolated services ..."
docker run -d --name "$hatchet_pg_container" --network "$network" --network-alias hatchet-postgres \
  -e POSTGRES_USER=hatchet -e POSTGRES_PASSWORD=verify-only -e POSTGRES_DB=hatchet \
  -v "$hatchet_pg_volume:/var/lib/postgresql/data" -v "$snapshot_dir:/snapshot:ro" \
  "$hatchet_postgres_image" >/dev/null
for _ in $(seq 1 60); do
  docker exec "$hatchet_pg_container" pg_isready -U hatchet -d hatchet >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$hatchet_pg_container" pg_isready -U hatchet -d hatchet >/dev/null
docker exec "$hatchet_pg_container" pg_restore -U hatchet -d hatchet --no-owner --no-privileges /snapshot/hatchet.dump

extract_archive() {
  local volume_name="$1" archive_name="$2"
  docker run --rm --user 0 --entrypoint tar \
    -v "$volume_name:/target" -v "$snapshot_dir:/snapshot:ro" "$app_image" \
    -xzf "/snapshot/$archive_name" -C /target
}
extract_archive "$appdata_volume" appdata.tar.gz
extract_archive "$opensearch_volume" opensearch.tar.gz
extract_archive "$hatchet_config_volume" hatchet-config.tar.gz

docker run -d --name "$opensearch_container" --network "$network" --network-alias opensearch \
  --ulimit memlock=-1:-1 \
  -e discovery.type=single-node -e DISABLE_INSTALL_DEMO_CONFIG=true \
  -e DISABLE_SECURITY_PLUGIN=true -e 'OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m' \
  -v "$opensearch_volume:/usr/share/opensearch/data" "$opensearch_image" >/dev/null
for _ in $(seq 1 120); do
  docker exec "$opensearch_container" curl -fsS http://localhost:9200/_cluster/health >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$opensearch_container" curl -fsS http://localhost:9200/_cluster/health >/dev/null

docker run -d --name "$hatchet_container" --network "$network" --network-alias hatchet \
  -e ADMIN_EMAIL=admin@example.com -e ADMIN_PASSWORD=verify-only \
  -e 'DATABASE_URL=postgresql://hatchet:verify-only@hatchet-postgres:5432/hatchet?sslmode=disable' \
  -e SERVER_GRPC_BIND_ADDRESS=0.0.0.0 -e SERVER_GRPC_INSECURE=t \
  -e SERVER_GRPC_BROADCAST_ADDRESS=hatchet:7077 -e SERVER_GRPC_PORT=7077 \
  -e SERVER_URL=http://hatchet:8888 -e SERVER_DEFAULT_ENGINE_VERSION=V1 \
  -v "$hatchet_config_volume:/config" "$hatchet_image" >/dev/null
for _ in $(seq 1 60); do
  if docker run --rm --network "$network" --entrypoint python "$app_image" -c \
    'import socket; socket.create_connection(("hatchet", 8888), 2).close()' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker run --rm --network "$network" --entrypoint python "$app_image" -c \
  'import socket; socket.create_connection(("hatchet", 8888), 2).close()'

docker run -d --name "$app_container" --network "$network" \
  -e 'KI_DATABASE_URL=postgresql+pg8000://ki:verify-only@postgres:5432/ki' \
  -e KI_ARTIFACT_DIR=/data/artifacts -e KI_CONFIG_PATH=/data/config.json \
  -e KI_SECURITY__AUTH_MODE=trusted_header \
  -v "$appdata_volume:/data" "$app_image" ki serve --host 0.0.0.0 --port 8000 >/dev/null
for _ in $(seq 1 60); do
  if docker run --rm --network "container:$app_container" --entrypoint python "$app_image" -c \
    'import urllib.request; urllib.request.urlopen("http://localhost:8000/healthz", timeout=2).read()' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker run --rm --network "container:$app_container" --entrypoint python "$app_image" -c \
  'import urllib.request; urllib.request.urlopen("http://localhost:8000/healthz", timeout=2).read()'

expected_source_objects="$(jq -r '.counts.source_objects' "$snapshot_dir/manifest.json")"
expected_indexed_objects="$(jq -r '.counts.indexed_objects' "$snapshot_dir/manifest.json")"
expected_managed_files="$(jq -r '.counts.managed_source_files' "$snapshot_dir/manifest.json")"
expected_artifacts="$(jq -r '.counts.artifact_files' "$snapshot_dir/manifest.json")"
expected_opensearch_documents="$(jq -r '.counts.opensearch_documents' "$snapshot_dir/manifest.json")"
expected_hatchet_tables="$(jq -r '.counts.hatchet_tables' "$snapshot_dir/manifest.json")"

actual_source_objects="$(docker exec "$pg_container" psql -U ki -d ki -Atc "SELECT count(*) FROM source_objects WHERE deleted_at IS NULL;")"
actual_indexed_objects="$(docker exec "$pg_container" psql -U ki -d ki -Atc "SELECT count(*) FROM processing_state WHERE stage='index' AND status='done';")"
actual_managed_files="$(docker exec "$app_container" sh -c "find /data/browser-sources -type f | wc -l | tr -d ' '")"
actual_artifacts="$(docker exec "$app_container" sh -c "find /data/artifacts -type f | wc -l | tr -d ' '")"
actual_opensearch_documents="$(docker exec "$opensearch_container" curl -fsS http://localhost:9200/knowledge-index-chunks-v1/_count | jq -r '.count')"
actual_hatchet_tables="$(docker exec "$hatchet_pg_container" psql -U hatchet -d hatchet -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")"

assert_equal() {
  local label="$1" expected="$2" actual="$3"
  [[ "$expected" == "$actual" ]] || {
    echo "Verification failed for $label: expected $expected, got $actual" >&2
    exit 1
  }
  printf '  %-28s %s\n' "$label" "$actual"
}

echo "Verifying restored state ..."
assert_equal source_objects "$expected_source_objects" "$actual_source_objects"
assert_equal indexed_objects "$expected_indexed_objects" "$actual_indexed_objects"
assert_equal managed_source_files "$expected_managed_files" "$actual_managed_files"
assert_equal artifact_files "$expected_artifacts" "$actual_artifacts"
assert_equal opensearch_documents "$expected_opensearch_documents" "$actual_opensearch_documents"
assert_equal hatchet_tables "$expected_hatchet_tables" "$actual_hatchet_tables"
echo "Snapshot import verified successfully; live containers were not modified."

