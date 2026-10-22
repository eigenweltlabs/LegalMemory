ARG APP_BASE_IMAGE
FROM ${APP_BASE_IMAGE}
USER root
RUN python -m pip install --no-cache-dir certifi==2026.10.19 \
    && sed -i 's|^mozilla/emSign_ECC_Root_CA_-_G3.crt$|!mozilla/emSign_ECC_Root_CA_-_G3.crt|' /etc/ca-certificates.conf \
    && update-ca-certificates \
    && python -c "import ssl,certifi; assert not any('emSign ECC Root CA - G3' in str(c['subject']) for c in ssl.create_default_context(cafile=certifi.where()).get_ca_certs())"
USER knowledge
