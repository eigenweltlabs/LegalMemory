FROM quay.io/docling-project/docling-serve:v1.18.0
USER root
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /tmp/pip-26.2.2-py3-none-any.whl
COPY deploy/python/bootstrap.py /tmp/bootstrap-python.py
RUN /usr/bin/python3.12 -I /tmp/bootstrap-python.py /tmp/pip-26.2.2-py3-none-any.whl --replace-system-pip \
    && /opt/app-root/bin/python -I /tmp/bootstrap-python.py /tmp/pip-26.2.2-py3-none-any.whl \
    && rm /tmp/bootstrap-python.py /tmp/pip-26.2.2-py3-none-any.whl
RUN /opt/app-root/bin/python -m pip install --no-cache-dir certifi==2026.10.19 \
    && /opt/app-root/bin/python -c "import certifi,shutil; shutil.copyfile(certifi.where(), '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem')" \
    && /opt/app-root/bin/python -c "import ssl,certifi; assert not any('emSign ECC Root CA - G3' in str(c['subject']) for c in ssl.create_default_context(cafile=certifi.where()).get_ca_certs())"
USER 1001
