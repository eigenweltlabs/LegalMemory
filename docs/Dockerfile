# The documentation, built for a sub-path and served by the edge.
#
# The output is static, so this image exists only to carry `dist` into the
# deployment — Caddy mounts it and serves it under /docs. Building it here
# rather than on the server means the deploy step moves a finished artifact and
# a broken docs build fails in CI instead of halfway through a release.

FROM node:24-slim AS build
WORKDIR /docs

# Baked at build time, not runtime: Astro writes the prefix into every generated
# link and every asset path, so the same image cannot serve two base paths.
ARG DOCS_BASE_PATH=/docs
ARG DOCS_SITE_URL=https://legalmemory.eigenweltlabs.com
ENV DOCS_BASE_PATH=${DOCS_BASE_PATH} DOCS_SITE_URL=${DOCS_SITE_URL}

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# A scratch-ish carrier. The deploy copies /dist out of this image into the
# volume Caddy serves; nothing in here ever runs.
FROM alpine:3 AS dist
COPY --from=build /docs/dist /dist
