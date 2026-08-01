FROM node:24-alpine AS ui

WORKDIR /build
COPY ui ./ui
COPY src/knowledge_index/web/static ./src/knowledge_index/web/static
RUN cd ui && npm ci && npm run build

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# pg_dump / pg_restore for the backup feature. Pinned to 17 from the PostgreSQL project's
# own repository rather than taken from Debian: pg_dump refuses to dump a server newer
# than itself, and this appliance talks to two servers — Postgres 16 for its own data and
# Postgres 17 for Hatchet's — so whichever Debian release the base image tracks must not
# decide whether backups work.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    # restic drives the incremental backup destination, where a night is stored as the
    # difference from the night before rather than as another full copy of the estate.
    # From Debian rather than pinned: any release carrying 0.14 or later has the
    # repository format that compresses what it deduplicates, and the destination refuses
    # to run against an older one rather than silently storing everything raw.
    && apt-get install -y --no-install-recommends restic \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

RUN addgroup --system knowledge && adduser --system --ingroup knowledge knowledge \
    && mkdir -p /data/artifacts /data/backup-staging /backups \
    && chown -R knowledge:knowledge /data /backups

COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src
COPY --from=ui /build/src/knowledge_index/web/static ./src/knowledge_index/web/static
RUN pip install .

USER knowledge
EXPOSE 8000
CMD ["ki", "serve", "--host", "0.0.0.0", "--port", "8000"]
