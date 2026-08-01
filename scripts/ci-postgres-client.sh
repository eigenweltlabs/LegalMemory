#!/usr/bin/env bash
# Install pg_dump / pg_restore 17 on a Debian-family CI runner.
#
# Same pin and same source as the Dockerfile, for the same reason: pg_dump refuses to dump
# a server newer than itself, and this appliance talks to two servers — Postgres 16 for its
# own data and Postgres 17 for Hatchet's. Ubuntu's own postgresql-client tracks whatever
# the release froze on (16 on 24.04), so taking it from the distribution would mean the
# runner image silently decides whether the Hatchet dump works.
#
# Kept out of the workflow file because two jobs need it and a duplicated apt key dance is
# a thing that drifts.
set -euo pipefail

sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
# shellcheck disable=SC1091
codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt ${codename}-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list > /dev/null
sudo apt-get update
sudo apt-get install -y --no-install-recommends postgresql-client-17

# Installing the package is not enough: /usr/bin/pg_restore is postgresql-common's
# pg_wrapper, and the runner image ships a PostgreSQL 16 server with a configured
# cluster, so the wrapper resolves an unqualified pg_restore to 16 — which cannot read
# the 1.16 archives the appliance's pg_dump 17 writes ("unsupported version (1.16) in
# file header", the error that broke the backup round-trip. The unit job never noticed,
# because there dump and restore both came from the same wrapper and agreed with each
# other). Put 17's real binaries first on PATH for every later step instead of trusting
# the wrapper.
echo "/usr/lib/postgresql/17/bin" >> "$GITHUB_PATH"

# restic drives the incremental backup destination. Installed rather than left out because
# tests/test_backup.py skips its restic cases when the binary is absent, and a test that
# skips on every machine that runs it is a test that does not exist.
sudo apt-get install -y --no-install-recommends restic

# Asserted, not just printed — the wrapper problem above surfaced as a test failure two
# jobs later; a wrong version should fail right here. GITHUB_PATH only takes effect in
# subsequent steps, so this checks the binaries the later steps will actually run.
/usr/lib/postgresql/17/bin/pg_dump --version | grep -F 'pg_dump (PostgreSQL) 17'
/usr/lib/postgresql/17/bin/pg_restore --version | grep -F 'pg_restore (PostgreSQL) 17'
restic version
