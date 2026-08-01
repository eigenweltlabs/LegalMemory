# Contributing to LegalMemory

Thanks for your interest in contributing.

## Development setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env          # fill in the required values
docker compose up -d --build
bash scripts/bootstrap-hatchet.sh
```

The admin UI lives in `ui/` (React + Vite):

```bash
cd ui && npm ci && npm run dev     # dev server on :5173, proxying the app on :8000
```

`npm run build` writes the production bundle into
`src/knowledge_index/web/static/`, which is committed; CI fails if the bundle
does not match the sources, so rebuild before committing UI changes.

The documentation site lives in `docs/` (Astro Starlight):

```bash
cd docs && npm install && npm run dev
```

## Tests and lint

Tests run against **real services** — there are no mocks for the stack. Start
the compose services first; tests that need Postgres expect it on
`localhost:5439`.

```bash
.venv/bin/pytest -q                    # the suite
.venv/bin/ruff check .                 # lint — CI pins the ruff version, see ci.yml
```

`.github/workflows/README.md` documents each CI job and how to run it locally.

## Ground rules

- **No silent fallbacks.** A failing dependency fails loudly, retries, and
  quarantines. Do not add demo stand-ins or mock code paths to the product.
- **Fail closed on permissions.** Anything touching ACLs, principals or
  retrieval scope must keep deny-wins and unknown-means-invisible semantics.
  Changes here need tests.
- **Read-only sources.** Connectors never write to a source system and never
  request write scopes.
- **Provenance on inference.** Model-produced fields carry the model, prompt
  version and confidence that produced them.
- **Documentation follows code.** If you change a screen, a connector, or a
  setting, update the matching page under `docs/src/content/docs/`.

## Adding a connector

Study `src/knowledge_index/connectors/registry.py` (the `CATALOG`) and an
existing source under `connectors/sources/`. A new OAuth provider is a
`providers.yaml` entry — endpoints, scopes and the registration guide the UI
renders. Every catalog entry needs a replay fixture
(`tests/test_connector_replay.py` fails without one) and a docs page under
`docs/src/content/docs/connectors/`.

## Pull requests

- Keep commits focused; explain *why* in the message body.
- New behaviour needs a test that fails without the change.
- CI must be green — it runs without repository secrets, so forks get
  identical results.
