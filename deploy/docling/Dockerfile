FROM quay.io/docling-project/docling-serve:v1.18.0
USER root
RUN /opt/app-root/bin/python -m pip install --no-cache-dir certifi==2026.10.19 \
    && /opt/app-root/bin/python -c "import certifi,shutil; shutil.copyfile(certifi.where(), '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem')" \
    && /opt/app-root/bin/python -c "import ssl,certifi; assert not any('emSign ECC Root CA - G3' in str(c['subject']) for c in ssl.create_default_context(cafile=certifi.where()).get_ca_certs())"
USER 1001
