#!/usr/bin/env bash
# One-time server setup: creates /opt/kaar/.env with random passwords and the MQTT users.
# Run on the server as the deploy user (before the first deploy):
#   curl -fsSLO https://raw.githubusercontent.com/AlainAhuili/kaar-monitoring/main/deploy/setup-env.sh
#   bash setup-env.sh kaarsmarttechs.com you@example.com
set -euo pipefail
base_domain="${1:?usage: setup-env.sh <base-domain> <acme-email>}"
acme_email="${2:?usage: setup-env.sh <base-domain> <acme-email>}"
dir=/opt/kaar
cd "$dir"

if [ -f .env ]; then
  echo ".env already exists – leaving it unchanged"
else
  pw() { openssl rand -hex "${1:-24}"; }
  umask 077
  cat > .env <<EOF
DB_PASSWORD=$(pw)
MQTT_INGEST_PASSWORD=$(pw)
MQTT_DEMO_GATEWAY_PASSWORD=$(pw)
GRAFANA_ADMIN_PASSWORD=$(pw 16)
GRAFANA_DEMO_DB_PASSWORD=$(pw)
DOMAIN=demo.${base_domain}
MQTT_DOMAIN=mqtt.${base_domain}
ACME_EMAIL=${acme_email}
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=alerts@${base_domain}
TELEGRAM_BOT_TOKEN=
EOF
  echo "created $dir/.env"
fi

set -a
# shellcheck source=/dev/null
. ./.env
set +a

mkdir -p mosquitto
touch mosquitto/passwd
docker run --rm -v "$dir/mosquitto:/m" eclipse-mosquitto:2 sh -c \
  "mosquitto_passwd -b /m/passwd ingest '$MQTT_INGEST_PASSWORD' &&
   mosquitto_passwd -b /m/passwd baeckerei-demo '$MQTT_DEMO_GATEWAY_PASSWORD' &&
   chown 1883:1883 /m/passwd && chmod 600 /m/passwd"
echo "MQTT users 'ingest' and 'baeckerei-demo' set"
echo
echo "Grafana admin login: admin / $GRAFANA_ADMIN_PASSWORD   (stored in $dir/.env)"
