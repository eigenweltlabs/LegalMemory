#!/usr/bin/env bash
# Reset the orchestrator completely: queue, database, token, worker.
#
# Why this exists: Hatchet keeps its own Postgres, separate from KI's. Truncating KI's
# tables leaves every queued task alive in Hatchet, so the moment a worker reconnects it
# drains a backlog belonging to data that no longer exists. And wiping Hatchet's volume
# invalidates HATCHET_CLIENT_TOKEN, so a worker started before the token is re-minted
# never registers and the engine sits there with no worker. Both failure modes have bitten
# repeatedly; the order below is the part that matters.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 1/5 stopping everything that talks to the orchestrator"
docker compose stop worker watcher hatchet hatchet-postgres >/dev/null 2>&1 || true
docker compose rm -f hatchet hatchet-postgres >/dev/null 2>&1 || true

echo "==> 2/5 destroying the orchestrator's own database and config"
docker volume rm knowledge-index_hatchet_pgdata knowledge-index_hatchet_config >/dev/null 2>&1 || true

echo "==> 3/5 bringing the engine back up on empty storage"
docker compose up -d hatchet-postgres hatchet >/dev/null
for _ in $(seq 1 60); do
  state=$(docker compose ps -a --format json 2>/dev/null \
    | python3 -c "import sys,json;print(next((d['State'] for d in json.load(sys.stdin) if d['Service']=='hatchet'),''))" 2>/dev/null || echo "")
  [ "$state" = "running" ] && break
  sleep 2
done
[ "$state" = "running" ] || { echo "hatchet did not start"; exit 1; }
sleep 15   # the engine seeds its schema after the container reports running

echo "==> 3.5 failing KI runs that the wipe just orphaned"
# A queued/running row in KI points at a Hatchet workflow that no longer exists. Left
# alone it sits at "queued" forever and the UI reports work that nothing will ever do.
# The app's run sweeper reaches the same rows on its own once the engine answers 404 for
# them, but this reset already knows they are dead, so it says so immediately.
#
# last_error is JSONB: the bare string this used to write was invalid JSON, so the whole
# statement errored and the redirect hid it. Failures are reported now — a reset that
# leaves runs claiming to be in flight is exactly what this step exists to prevent.
if ! docker compose exec -T postgres psql -U ki -d ki -v ON_ERROR_STOP=1 -tAc \
  "update pipeline_runs set status='failed', finished_at=now(),
     last_error='{\"class\": \"StrandedRun\",
                  \"message\": \"the orchestrator was reset; this run was abandoned\",
                  \"detected_by\": \"reset-hatchet\"}'::jsonb
   where status in ('queued','running');" >/dev/null; then
  echo "WARNING: could not fail orphaned runs; the admin UI may still report them as running" >&2
fi

echo "==> 4/5 minting a fresh worker token (the old one died with the volume)"
bash scripts/bootstrap-hatchet.sh --rotate >/dev/null

echo "==> 5/5 starting the worker and waiting for it to register"
docker compose up -d worker watcher >/dev/null
for _ in $(seq 1 45); do
  if docker compose logs worker 2>&1 | grep -q "waiting for tasks"; then
    echo
    echo "orchestrator reset: queue empty, token rotated, worker registered."
    docker compose logs worker 2>&1 | grep "waiting for tasks" | tail -1
    exit 0
  fi
  sleep 2
done

echo
echo "WORKER DID NOT REGISTER. Its last lines:" >&2
docker compose logs worker --tail 20 >&2
exit 1
