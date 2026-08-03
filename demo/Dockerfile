# The demo application: a document browser and a chat, in front of the appliance.
#
# Nothing here reaches a model or a document store directly. Every request the
# browser makes lands on this server, which calls the appliance over its own
# network under the demo's identity. That is why the image needs no provider
# key and no database driver.

# Debian rather than Alpine: on musl, npm resolves sharp to its wasm32 fallback,
# which drags in optional packages a lockfile generated on any other platform
# does not carry — and `npm ci` refuses a lockfile that does not match. glibc
# has native prebuilds for every architecture this is likely to run on.
FROM node:24-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# `npm ci` first, because a demo that resolved a different dependency tree than
# the one tested is not the thing that was tested. It falls back because npm
# omits the transitive dependencies of platform-specific optional packages it
# did not install: a lockfile written on macOS has no entry for the deps of
# Tailwind's wasm32 oxide fallback, and `npm ci` on linux refuses the lockfile
# rather than skipping a package it will not use. `npm install` resolves them.
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

FROM node:24-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:24-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Not root. The demo holds the header that authenticates it to the appliance,
# and a process that does not need to be root should not be.
RUN groupadd -g 1001 nodejs && useradd -u 1001 -g nodejs -m nextjs

# `output: "standalone"` traces exactly the files the server needs; static and
# public are copied beside it because the standalone server does not embed them.
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=build --chown=nextjs:nodejs /app/public ./public

USER nextjs
EXPOSE 3000

# /healthz, not /: behind a login the index answers a redirect to a Clerk-hosted
# page, and a probe that follows it is testing Clerk's uptime rather than ours.
HEALTHCHECK --interval=15s --timeout=4s --retries=8 --start-period=15s \
  CMD node -e "fetch('http://127.0.0.1:3000/healthz',{redirect:'manual'}).then(r=>process.exit(r.status===200?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
