#!/usr/bin/env bash
# The stack tests/test_backup_roundtrip.py needs, brought up and proven fit to back up.
#
#   scripts/ci-stack.sh up     build, start, wait, and refuse to continue if any store the
#                              backup is meant to capture is missing or unreadable
#   scripts/ci-stack.sh env    print the variables that point pytest at this stack
#   scripts/ci-stack.sh logs   dump every container's log (for a failed job)
#   scripts/ci-stack.sh down   stop it and delete its volumes
#
# The test does not talk to these services itself. It runs the product's own
# perform_backup inside the app container, over `docker exec`, because three of the five
# failures it exists for are invisible anywhere else — the snapshot repository's
# ownership, Hatchet's 0600 config, and the credentials of two databases derived from the
# primary on a server only the compose network can reach. So this is not a convenient
# subset: it is nine of the eleven services, and each one is load-bearing.
#
#   app                 where the capture runs, as the unprivileged user, through the
#                       read-only mounts docker-compose.yml gives it
#   postgres            the appliance's database and — through deploy/postgres/init — the
#                       litellm and langfuse databases derived from its URL
#   litellm, langfuse   not for their models but for their tables. The round-trip asserts
#                       each derived dump holds TABLE DATA, and a database nothing has
#                       migrated dumps clean and empty, which is exactly the green-but-
#                       hollow backup the whole file is about
#   hatchet-postgres    a Postgres 17 server behind a pg_dump 17 client
#   hatchet             the only source of genuinely Hatchet-generated config: root-owned,
#                       mode 0600, unreadable by the backup until the init container acts
#   keycloak            the only source of a real embedded H2 database in keycloak_data
#   opensearch          its snapshot API, writing into the shared repository volume
#   docling             not used by any backup component, and 8.6 GB of the images this
#                       job pulls. It is here for exactly one reason: docker-compose.yml
#                       gives app `depends_on: docling: condition: service_healthy`, so
#                       the container the capture runs in does not start without it. Not
#                       for conftest's live_stack fixture — test_backup_roundtrip.py
#                       deliberately does not use it, because a backup calls no model and
#                       converts no document
#
# Not started: worker, watcher, restore-agent and oauth2-proxy. Nothing in the round-trip
# reaches any of them.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd -P)"

# A distinct project name, always. The same compose file describes the stack a developer
# already has running on this machine; without this, `up` would recreate their containers
# with CI's dummy provider keys and `down -v` would delete their index. The round-trip
# test finds the stack by this name, which is why `env` below exports it.
PROJECT="${KI_CI_PROJECT:-knowledge-index-ci}"
ENV_FILE="${KI_CI_ENV_FILE:-.env.ci}"
CI_DIR="$ROOT/runtime/ci"

OPENSEARCH_HOST_PORT="${OPENSEARCH_PORT:-9200}"
LITELLM_HOST_PORT="${LITELLM_PORT:-4000}"
DOCLING_HOST_PORT="${DOCLING_PORT:-5001}"
KEYCLOAK_HOST_PORT="${KEYCLOAK_PORT:-8083}"

compose() {
  docker compose -p "$PROJECT" --env-file "$ENV_FILE" \
    -f docker-compose.yml -f docker-compose.ci.yml "$@"
}

wait_for() {
  local what="$1" seconds="$2"
  shift 2
  local deadline=$((SECONDS + seconds))
  until "$@" >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
      echo "TIMED OUT after ${seconds}s waiting for: ${what}" >&2
      return 1
    fi
    sleep 5
  done
  echo "ready: ${what}"
}

fail() {
  echo "FATAL: $*" >&2
  exit 1
}

cmd_up() {
  [ -f "$ENV_FILE" ] || fail "$ENV_FILE does not exist — run scripts/ci-env.sh first"

  # Created here, by this user, before Docker sees them. A bind-mount source Docker has to
  # create is created by the daemon as root, and the round-trip test then finds a /backups
  # it cannot write and skips the entire suite with a message about the mount.
  mkdir -p "$CI_DIR/backups" "$CI_DIR/local"
  chmod 0777 "$CI_DIR/backups"

  # Keycloak refuses to start without the certificate files docker-compose.yml points it
  # at, and deploy/keycloak/tls is gitignored — it is generated per machine and never
  # shipped. The openssl fallback in this script is enough: nothing in CI performs an OAuth
  # flow, it only has to let Keycloak boot far enough to initialise its embedded database.
  #
  # The chmod is what makes that work on a Linux runner, and it is not cosmetic. The
  # generator ends with `chmod 600` on the key; the runner owns it as uid 1001, the
  # Keycloak container runs as uid 1000, and the mount is a plain bind — so Keycloak
  # cannot read the key it was pointed at and exits 1 with
  #   ERROR: Failed to load 'https-*' material: AccessDeniedException .../keycloak.key
  # over and over, until `wait_for "Keycloak realm"` gives up ten minutes later and the
  # job fails somewhere that says nothing about certificates. None of this shows on a
  # developer's Mac, where Docker Desktop reports every bind-mounted file as owned by the
  # container's own user, which is why a 0600 key works there and only there.
  #
  # Widening it is safe only because this branch runs when the material did not exist: a
  # throwaway self-signed key openssl made seconds ago, for one run, on a checkout that is
  # discarded. A developer's own key — mkcert-issued and possibly trusted by their machine
  # — is left at 0600 because this branch does not run at all when it is already there.
  if [ ! -f deploy/keycloak/tls/keycloak.crt ]; then
    scripts/gen-keycloak-tls.sh
    chmod 0644 deploy/keycloak/tls/keycloak.key
  fi

  # Explicit, so the log shows the build as its own step rather than as a silent seven
  # minutes inside `up`.
  compose build app keycloak

  compose up -d --wait postgres hatchet-postgres opensearch
  compose up -d hatchet keycloak langfuse
  # --wait on app covers litellm and docling too: it starts them, and app's own healthcheck
  # cannot pass until both are up, because docker-compose.yml makes it wait for them.
  compose up -d --wait app

  # Probed from the host rather than trusted from `up --wait`, because a published port
  # that nothing has bound yet is the difference between a stack that is running and a
  # stack a test on this runner can reach.
  wait_for "LiteLLM gateway on localhost" 600 \
    curl -fsS "http://127.0.0.1:${LITELLM_HOST_PORT}/health/liveliness"
  wait_for "Docling Serve on localhost" 600 \
    curl -fsS "http://127.0.0.1:${DOCLING_HOST_PORT}/health"
  wait_for "OpenSearch on localhost" 600 \
    curl -fsS "http://127.0.0.1:${OPENSEARCH_HOST_PORT}/_cluster/health"

  # Keycloak has to have finished importing the realm, not merely be running: the volume it
  # is backed up from holds nothing until it has created its embedded database, and a
  # backup of an empty keycloak_data would pass every check except the one that matters.
  wait_for "Keycloak realm" 600 \
    curl -fsS "http://127.0.0.1:${KEYCLOAK_HOST_PORT}/realms/knowledge-index"

  wait_for "Hatchet-generated config" 600 \
    compose run --rm --no-deps backup-permissions sh -c '[ -s /hatchet-config/server.yaml ]'

  # And now, once those files exist, hand them over. This is not a CI workaround: on a
  # first-ever `docker compose up` the init container runs before Hatchet has written
  # anything, so its chown lands on an empty directory and the config Hatchet then writes
  # is root-owned and 0600. A brand-new appliance's first backup skips
  # volumes/hatchet-config for that reason and starts capturing it after the next restart.
  compose run --rm backup-permissions

  cmd_verify
}

# The difference between "the stack came up" and "the stack can be backed up". A backup
# that reports success with two thirds of the estate missing is the failure this job
# exists to prevent, so a store that is present but unreadable has to stop the job here
# rather than turn into a skipped component in a green run.
#
# Every check is behavioural and runs where the appliance runs — can this user open this
# file, can that user write that directory — because the metadata reads fine in all the
# cases that shipped broken.
cmd_verify() {
  local missing
  missing="$(compose exec -T postgres psql -U ki -d postgres -tAc \
    "SELECT string_agg(d, ',') FROM unnest(ARRAY['litellm','langfuse']) AS d
      WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = d)")"
  [ -z "$missing" ] || fail "databases missing from the primary server: $missing — \
deploy/postgres/init only runs on a first initialisation, so a reused volume has none"

  # Tables, not just databases. An empty database dumps and restores perfectly and proves
  # nothing, and the round-trip asserts TABLE DATA in both derived dumps.
  local database tables
  for database in litellm langfuse; do
    tables="$(compose exec -T postgres psql -U ki -d "$database" -tAc \
      "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
    [ "${tables//[[:space:]]/}" != "0" ] || fail "the $database database has no tables — \
its service has not migrated it yet, and a dump of it would be an empty archive"
  done

  # As OpenSearch, which is the user that fails when the shared repository is root-owned.
  compose exec -T opensearch sh -c 'touch /mnt/snapshots/.ci-probe && rm /mnt/snapshots/.ci-probe' \
    || fail "OpenSearch cannot write its snapshot repository — it would fail to create a \
snapshot and opensearch/snapshot would be reported as a skipped component"

  # And as the appliance, which reads the finished snapshot back out and deletes it.
  compose exec -T app sh -c 'touch /backup-sources/opensearch-snapshots/.ci-probe \
    && rm /backup-sources/opensearch-snapshots/.ci-probe' \
    || fail "the app container cannot write the snapshot repository"

  compose exec -T app sh -c '
    set -e
    [ -n "$(ls -A /backup-sources/hatchet-config)" ] || { echo "hatchet-config is empty"; exit 1; }
    for f in /backup-sources/hatchet-config/*; do
      [ -r "$f" ] || { echo "unreadable: $f"; exit 1; }
    done
    [ -s /backup-sources/keycloak/h2/keycloakdb.mv.db ] || {
      echo "keycloak volume holds no embedded database"; exit 1; }
  ' || fail "the app container cannot read the two container-owned volumes — the backup \
runs unprivileged there and would skip both"

  [ -w "$CI_DIR/backups" ] || fail "$CI_DIR/backups is not writable by $(id -un); the \
round-trip skips when the appliance's /backups mount is not a directory it can write"

  echo "verified: litellm and langfuse have tables; the snapshot repository is writable by \
OpenSearch and by the appliance; both container-owned volumes are readable there; \
/backups is writable here"
}

cmd_env() {
  cat <<ENV
KI_TEST_COMPOSE_PROJECT=${PROJECT}
ENV
}

cmd_logs() {
  compose logs --no-color --tail=300
}

cmd_down() {
  compose down -v --remove-orphans || true
  # The appliance writes into /backups as its own user, so a plain rm from a developer's
  # account fails on the backups a run left behind. Hand them back before deleting them.
  if [ -d "$CI_DIR" ]; then
    docker run --rm -v "$CI_DIR:/ci" alpine:3 \
      chown -R "$(id -u):$(id -g)" /ci >/dev/null 2>&1 || true
    rm -rf "$CI_DIR"
  fi
}

case "${1:-}" in
  up) cmd_up ;;
  verify) cmd_verify ;;
  env) cmd_env ;;
  logs) cmd_logs ;;
  down) cmd_down ;;
  *)
    echo "usage: $0 {up|verify|env|logs|down}" >&2
    exit 2
    ;;
esac
