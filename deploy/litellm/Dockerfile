FROM node:26.12.1-bookworm-slim AS node
FROM ghcr.io/berriai/litellm:v1.85.1
USER root
# Keep the application version unchanged while replacing its bundled runtimes.
COPY --from=node /usr/local/bin/node /usr/bin/node
RUN python -m ensurepip --upgrade \
    && python -m pip install --no-cache-dir certifi==2026.10.19 \
    && apk add --no-cache libatomic \
    && apk upgrade --no-cache ca-certificates-bundle
# Wolfi ships a bundle directly, without update-ca-certificates. Use the fixed
# Mozilla bundle for both Python and system clients, then verify trust.
RUN python -c "import certifi,shutil; shutil.copyfile(certifi.where(), '/etc/ssl/certs/ca-certificates.crt')" \
    && python -c "import ssl,certifi; assert not any('emSign ECC Root CA - G3' in str(c['subject']) for c in ssl.create_default_context(cafile=certifi.where()).get_ca_certs())" \
    && node --version
