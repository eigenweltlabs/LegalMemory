# CI

One workflow, `ci.yml`, on every push and pull request. Four jobs, all independent, none
reading a repository secret — so a fork's pull request gets the same result a branch does.

| job | what it proves | roughly |
| --- | --- | --- |
| `lint` | `ruff check` over the whole repository | under a minute |
| `unit` | the fast suite against pgvector Postgres 16 on port 5439 | 6-9 minutes |
| `ui` | the committed bundle is what this source builds | 2-3 minutes |
| `backup-roundtrip` | a real backup of the whole estate, captured inside the appliance | 25-45 minutes |

## Running the jobs on your own machine

```sh
ruff check .

# unit — needs postgres on 5439, pg_dump >= the server, and a credential key
KI_CONNECTOR_CREDENTIAL_KEY=Y2ktZHVtbXkta2V5LW5vdC1hLXJlYWwtc2VjcmV0LS0= pytest -q -ra

# ui
cd ui && npm ci && npm run build && cd ..
git status --porcelain -- src/knowledge_index/web/static      # must print nothing

# backup-roundtrip
scripts/ci-env.sh .env.ci
scripts/ci-stack.sh up
eval "$(scripts/ci-stack.sh env | sed 's/^/export /')"
pytest -m integration tests/test_backup_roundtrip.py -q -ra   # on Linux: sudo -E env "PATH=$PATH" pytest ...
scripts/ci-stack.sh down
```

`-ra`, not `-rs`. A bare `-rs` *replaces* pytest's default report characters rather than
adding to them, so the short summary lists the skips and drops the `FAILED` lines — which
is how you get a red run that does not say what went red.

The `sudo` on Linux is not optional and is not about the stack. The appliance writes every
component at the destination through `tempfile.mkstemp`, which creates `0600` whatever the
umask, as the unprivileged user the image runs (uid 100). A bind mount on Linux carries
that ownership through untouched, so an ordinary account cannot open the backup it has just
asked the appliance to make — `stage_backup` raises `PermissionError`, every test errors,
and the fixture's `rmtree(..., ignore_errors=True)` teardown fails as quietly and leaves an
encrypted copy of the estate in the working directory. On a Mac none of it happens: Docker
Desktop reports every bind-mounted file as owned by the host user whatever the container
wrote it as. Nothing is weakened by running as root — `stage_backup` opens with an explicit
`0600`, so the assertion that a staged component is unreadable by others still holds.

On a machine that already runs the appliance, skip `ci-stack.sh` entirely: the round-trip
is written to run against a normal `docker compose up -d`, finds the stack by its compose
project label, cleans up the backup, the scratch stores and the seeded index it created,
and touches neither the appliance's database nor its blob store nor its search index.
`scripts/ci-stack.sh` exists because a runner has no stack to find.

`ci-stack.sh` always uses the compose project `knowledge-index-ci`, so `down -v` can never
delete a development stack's volumes. It does still want the same host ports — 5439, 9200,
4000, 5001, 8083 — and `tests/conftest.py` hard-codes the first two, so the two stacks
cannot run at the same time. Stop the development one first.

## Why there is no committed `.env.ci`

`.gitignore` excludes `.env.*`. A file by that name would be untracked, CI would run on
whatever the runner happened to have, and the values nobody could see would be the ones
that mattered. `scripts/ci-env.sh` generates it instead, so the dummy values live in
version control where they can be read and reviewed. It refuses to write to `.env`.

Every value it writes is obvious rubbish. `docker-compose.yml` marks twelve variables
required with `${VAR:?}` and refuses to start anything without them; none of the twelve
has to be real, because nothing in CI calls a model.

## Why the round-trip job is so large

`tests/test_backup_roundtrip.py` does not talk to the services itself. It runs the
product's own `perform_backup` **inside the app container**, over `docker exec`, as the
unprivileged user, through the read-only mounts `docker-compose.yml` gives it — because
three of the five failures it exists for are invisible anywhere else. So the job builds
the appliance image and the Keycloak image and starts nine of the eleven services:

| service | why it cannot be left out |
| --- | --- |
| `app` | the capture runs there |
| `postgres` | the appliance's database, plus the `litellm` and `langfuse` databases its init scripts create |
| `litellm`, `langfuse` | for their **tables**. The test asserts each derived dump holds `TABLE DATA`, and an unmigrated database dumps clean and empty |
| `hatchet-postgres` | a Postgres 17 server behind a pg_dump 17 client |
| `hatchet` | the only source of genuinely Hatchet-generated config: root-owned, mode 0600 |
| `keycloak` | the only source of a real embedded H2 database in `keycloak_data` |
| `opensearch` | its snapshot API, writing the repository volume it shares with the appliance |
| `docling` | no backup component touches it. It is here only because `app` has `depends_on: docling: condition: service_healthy`, so the container the capture runs in does not start without it. Not for `conftest.live_stack` — `test_backup_roundtrip.py` deliberately does not use that fixture |

`worker`, `watcher`, `restore-agent` and `oauth2-proxy` are not started; nothing in the
round-trip reaches them.

Two consequences worth knowing before you read a red run:

- **Disk.** The images come to roughly 15 GB, Docling alone 8.6 GB, against about 21 GB
  free on a hosted runner. The job deletes the preinstalled .NET, Android and GHC
  toolchains first. If that stops being enough, the failure is a `no space left on device`
  in the middle of a pull.
- **Keycloak's TLS key.** `scripts/gen-keycloak-tls.sh` ends with `chmod 600` on the key.
  On a Linux runner that key is owned by uid 1001 and the Keycloak container runs as uid
  1000, so Keycloak cannot read the file `docker-compose.yml` points it at and exits with
  `Failed to load 'https-*' material: AccessDeniedException`. `ci-stack.sh` widens the mode
  to 0644 — but only for material it generated itself in this run, never for a key that was
  already on the machine. None of this reproduces on a Mac, where Docker Desktop reports
  every bind-mounted file as owned by the container's own user.
- **The second `backup-permissions` pass.** On a first-ever `docker compose up` the init
  container runs before Hatchet has written anything, so its `chown` lands on an empty
  directory and the config Hatchet then writes is root-owned and 0600. `ci-stack.sh` waits
  for the config and runs the init container again. This is not a CI workaround — it means
  a brand-new appliance's very first backup skips `volumes/hatchet-config` and only starts
  capturing it after the stack is restarted.

## What it covers, and what it does not

The fast suite has 75 backup tests and passed through four separate production failures,
because its fixture switches the gateway databases, the orchestrator database and the
search index off and mounts neither container-owned volume — it exercises roughly five of
the ten components. This job exists for the other five.

Covered: the derived `litellm` and `langfuse` dumps against a live server with a real
password; `hatchet` on a Postgres 17 server; a real OpenSearch snapshot into the shared
repository, written as uid 1000 and read back as the appliance's user; and both
container-owned volumes captured byte for byte through the read-only mounts.

Not covered, and a green tick must not be read as covering it:

- **Applying either container-owned volume.** Replacing them means stopping Keycloak and
  Hatchet, which are the containers the suite is talking to. The test asserts the restore
  plan says `stop`; the replacement itself is covered against a stub agent in
  `tests/test_backup.py`. A firm rehearsing the real thing runs `scripts/restore-backup.sh`.
- **Restoring the search index.** `apply_search_index` empties the repository and restores
  over the live cluster.
- **Anything that calls a model.** LiteLLM boots on dummy provider keys and would fail any
  real completion.

Because so many of the skips above are legitimate, the job asserts on the JUnit report
that no integration test skipped and at least one ran. A skipped backup test is a green
exit code over an estate nothing checked, which is the same failure as a backup reporting
success with six of ten components.

## `ruff check` is red on main today

`ruff check .` reports 23 findings on the tree this workflow was added to — 2 `F541` in
`src/knowledge_index/cli.py`, 12 `F401`, 6 `E402` and 3 `F811`, concentrated in
`src/knowledge_index/connectors/configs.py` (which carries a duplicated import block from
line 435) plus `src/knowledge_index/retrieval.py` and a vendored script under
`src/knowledge_index/benchmark/harvey/skills/`. 17 are fixable with `ruff check --fix .`.

They are not excluded here, and no path carve-out was added for them. A linter configured
around the code that fails it reports nothing worth knowing. Fix them in the same change
that merges this workflow, or the `lint` job is red from its first run.
