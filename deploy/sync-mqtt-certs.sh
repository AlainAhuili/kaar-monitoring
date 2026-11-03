#!/usr/bin/env bash
# Copy the Let's Encrypt certificate Caddy manages for $MQTT_DOMAIN to Mosquitto and
# restart the broker only if it changed. Run by deploy.sh and weekly by cron:
#   17 4 * * 1  cd /opt/kaar && ./deploy/sync-mqtt-certs.sh >> /var/log/kaar-certs.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck source=/dev/null
. ./.env
set +a
dc() { docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml --profile demo "$@"; }

src="/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/${MQTT_DOMAIN}"
mkdir -p mosquitto/certs
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# Caddy may need a minute to obtain the certificate on first start
for _ in $(seq 1 30); do
  if dc exec -T caddy test -f "$src/${MQTT_DOMAIN}.crt"; then break; fi
  sleep 4
done
dc exec -T caddy cat "$src/${MQTT_DOMAIN}.crt" > "$tmp/fullchain.pem"
dc exec -T caddy cat "$src/${MQTT_DOMAIN}.key" > "$tmp/privkey.pem"
if [ ! -s "$tmp/fullchain.pem" ] || [ ! -s "$tmp/privkey.pem" ]; then
  echo "certificate not available yet"
  exit 1
fi

if cmp -s "$tmp/fullchain.pem" mosquitto/certs/fullchain.pem 2>/dev/null &&
   cmp -s "$tmp/privkey.pem" mosquitto/certs/privkey.pem 2>/dev/null; then
  echo "MQTT certificate unchanged"
  exit 0
fi

docker run --rm -v "$PWD/mosquitto/certs:/c" -v "$tmp:/new:ro" alpine:3 sh -c \
  'cp /new/fullchain.pem /new/privkey.pem /c/ && chown 1883:1883 /c/*.pem && chmod 600 /c/privkey.pem && chmod 644 /c/fullchain.pem'
dc restart mosquitto
echo "MQTT certificate updated, broker restarted"
