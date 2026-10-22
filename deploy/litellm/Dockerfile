FROM node:26.12.1-bookworm-slim AS node
FROM ghcr.io/berriai/litellm:v1.85.1
USER root
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /tmp/pip-26.2.2-py3-none-any.whl
COPY deploy/python/bootstrap.py /tmp/bootstrap-python.py
RUN python -I /tmp/bootstrap-python.py /tmp/pip-26.2.2-py3-none-any.whl \
    && rm /tmp/bootstrap-python.py /tmp/pip-26.2.2-py3-none-any.whl
# Keep the application version unchanged while replacing its bundled runtimes.
COPY --from=node /usr/local/bin/node /usr/bin/node
RUN python -m pip install --no-cache-dir certifi==2026.10.19 \
    && apk add --no-cache libatomic \
    && apk upgrade --no-cache ca-certificates-bundle
# Wolfi ships a bundle directly, without update-ca-certificates. Use the fixed
# Mozilla bundle for both Python and system clients, then verify trust.
RUN python -c "import certifi,shutil; shutil.copyfile(certifi.where(), '/etc/ssl/certs/ca-certificates.crt')" \
    && python -c "import ssl,certifi; assert not any('emSign ECC Root CA - G3' in str(c['subject']) for c in ssl.create_default_context(cafile=certifi.where()).get_ca_certs())" \
    && node --version
