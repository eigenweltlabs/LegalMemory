#!/usr/bin/env bash
# Create the Hatchet API token for the default tenant and hand it to app + worker.
# Idempotent: reuses an existing token in .env unless --rotate is passed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "${1:-}" != "--rotate" ]] && grep -q '^HATCHET_CLIENT_TOKEN=.\+' .env 2>/dev/null; then
  echo "HATCHET_CLIENT_TOKEN already present in .env (use --rotate to replace)"
  exit 0
fi

echo "Waiting for the Hatchet engine ..."
for _ in $(seq 1 60); do
  # default token lifetime is 90 days — an appliance would lose its orchestrator
  # mid-engagement; issue a 10-year token and document rotation instead
  if docker compose exec -T hatchet /hatchet-admin token create \
      --config /config --tenant-id 707d0855-80ab-4e1f-a156-f1c4546cbf52 \
      --expiresIn 87600h \
      > /tmp/hatchet-token.$$ 2>/dev/null; then
    break
  fi
  sleep 2
done
token="$(tr -d '[:space:]' < /tmp/hatchet-token.$$)"
rm -f /tmp/hatchet-token.$$
if [[ -z "$token" ]]; then
  echo "Failed to create a Hatchet token — is the hatchet service healthy?" >&2
  exit 1
fi

touch .env
grep -v '^HATCHET_CLIENT_TOKEN=' .env > .env.tmp || true
printf 'HATCHET_CLIENT_TOKEN=%s\n' "$token" >> .env.tmp
mv .env.tmp .env

echo "Token written to .env — restarting app and worker with credentials"
docker compose up -d app worker
