# Python installer trust

All image and CI dependency installs bootstrap pip 26.2.2 from the official PyPI wheel pinned by SHA-256. The bootstrap uses the verified wheel directly with `--no-index`; an older pip never downloads its own replacement. Docker builds use the repository root as their context.

Run `python -I deploy/python/bootstrap.py` in a writable Python 3.10+ environment to apply the same bootstrap. CI needs curl. Docker supplies the wheel through `ADD --checksum`. The script verifies the installed pip version and its own vendored CA trust after installation.

The bootstrap also refreshes ensurepip and virtualenv seeds. Docling contains Python 3.8/3.9 compatibility wheels that cannot use pip 26.2.2. Only an affected vendored CA bundle in those legacy wheels is replaced; their wheel RECORD and virtualenv SHA-256 records are regenerated. Distro wheels using the patched system trust store are preserved. Build logs record seed paths and hashes.

The separate certifi package and operating-system CA store are still patched by each image Dockerfile. Updating standalone certifi alone does not repair pip's private CA bundle.
