#!/usr/bin/env bash
# Runs only on first initialization of the postgres volume: Langfuse keeps its own
# database, for the same reason LiteLLM does — trace storage never mixes with the
# ontology, and either can be dropped without touching the knowledge layer.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -c "CREATE DATABASE langfuse OWNER $POSTGRES_USER"
