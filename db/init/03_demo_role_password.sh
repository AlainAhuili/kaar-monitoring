#!/bin/bash
# Runs once at DB initialisation: replace the seed's placeholder password of the
# read-only demo role with the one from the environment.
set -euo pipefail
if [ -n "${GRAFANA_DEMO_DB_PASSWORD:-}" ]; then
  psql -v ON_ERROR_STOP=1 -v pw="$GRAFANA_DEMO_DB_PASSWORD" \
       --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
       -c "ALTER ROLE grafana_baeckerei_demo PASSWORD :'pw'"
fi
