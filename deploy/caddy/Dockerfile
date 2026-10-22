FROM golang:1.27.3-alpine AS builder
RUN apk add --no-cache git ca-certificates
RUN go install github.com/caddyserver/xcaddy/cmd/xcaddy@v0.4.7
RUN xcaddy build v2.11.7 --output /usr/bin/caddy

FROM caddy:2.11.7-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
# Refresh the system bundle, then explicitly distrust the compromised CA chain.
RUN apk upgrade --no-cache ca-certificates \
    && sed -i 's|^mozilla/emSign_ECC_Root_CA_-_G3.crt$|!mozilla/emSign_ECC_Root_CA_-_G3.crt|' /etc/ca-certificates.conf \
    && update-ca-certificates
