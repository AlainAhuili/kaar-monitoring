#!/bin/bash
# Runs once at DB initialisation: replace the seed's placeholder password of the
# read-only demo role with the one from the environment.
set -euo pipefail
if [ -n "${GRAFANA_DEMO_DB_PASSWORD:-}" ]; then
  # The SQL goes via stdin: psql does not substitute :'pw' in -c commands,
  # which left the role on the seed password and broke Grafana's datasource.
  psql -v ON_ERROR_STOP=1 -v pw="$GRAFANA_DEMO_DB_PASSWORD" \
       --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
ALTER ROLE grafana_baeckerei_demo PASSWORD :'pw';
SQL
fi
