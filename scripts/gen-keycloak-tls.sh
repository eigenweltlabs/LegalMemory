#!/usr/bin/env bash
# TLS for the development Keycloak.
#
# Google refuses a plain-http redirect URI for a Web OAuth client — its localhost
# exception covers Desktop clients only — so a real Google sign-in needs https even on a
# laptop. And a self-signed certificate is not enough: an MCP client doing OAuth verifies
# the chain and aborts where a browser would offer a click-through. mkcert issues from a
# CA the machine trusts, which satisfies both.
#
# Production replaces all of this with the firm's own certificate.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p deploy/keycloak/tls

if command -v mkcert >/dev/null 2>&1; then
  mkcert -cert-file deploy/keycloak/tls/keycloak.crt \
         -key-file  deploy/keycloak/tls/keycloak.key \
         localhost 127.0.0.1 keycloak ::1
  echo
  echo "Trust it:  mkcert -install            # once, asks for your password"
  echo "For Node:  export NODE_EXTRA_CA_CERTS=\"$(mkcert -CAROOT)/rootCA.pem\""
  echo "           Node ignores the system keychain, so a client that speaks OAuth"
  echo "           needs this even after mkcert -install."
else
  echo "mkcert not found (brew install mkcert) — falling back to self-signed." >&2
  echo "A browser will warn; an OAuth client will refuse outright." >&2
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout deploy/keycloak/tls/keycloak.key \
    -out deploy/keycloak/tls/keycloak.crt \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,DNS:keycloak,IP:127.0.0.1" 2>/dev/null
fi

chmod 600 deploy/keycloak/tls/keycloak.key
echo "restart: docker compose up -d --force-recreate keycloak"
