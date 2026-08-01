#!/usr/bin/env bash
# Runs only on first initialization of the postgres volume: give the LiteLLM
# gateway its own database so gateway tables never mix with the ontology.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -c "CREATE DATABASE litellm OWNER $POSTGRES_USER"
