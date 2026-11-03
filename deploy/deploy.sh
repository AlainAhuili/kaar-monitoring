#!/usr/bin/env bash
# Runs ON THE SERVER (called by GitHub Actions over SSH, or manually for a rollback):
#   IMAGE_TAG=<git sha> INGEST_IMAGE=ghcr.io/<owner>/kaar-monitoring-ingest ./deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
: "${IMAGE_TAG:?IMAGE_TAG required}" "${INGEST_IMAGE:?INGEST_IMAGE required}"
[ -f .env ] || { echo ".env missing – see README 'Deployment'"; exit 1; }
[ -f mosquitto/passwd ] || { echo "mosquitto/passwd missing – run tools/mqtt_user.sh first"; exit 1; }

# Persist the version so manual `docker compose` commands use the same images
sed -i '/^IMAGE_TAG=/d;/^INGEST_IMAGE=/d' .env
printf 'IMAGE_TAG=%s\nINGEST_IMAGE=%s\n' "$IMAGE_TAG" "$INGEST_IMAGE" >> .env

dc() { docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml --profile demo "$@"; }

echo "==> deploying $INGEST_IMAGE:$IMAGE_TAG"
dc pull --quiet
dc up -d --remove-orphans

./deploy/sync-mqtt-certs.sh || echo "WARN: MQTT certificate sync failed (TLS port may be down until it succeeds)"

echo "==> waiting for ingest to be stable"
sleep 15
cid=$(dc ps -q ingest)
status=$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$cid")
if [ "$status" != "running 0" ]; then
  echo "ERROR: ingest is '$status'"
  dc logs --tail 50 ingest
  exit 1
fi
dc logs --tail 5 ingest
dc ps
docker image prune -f > /dev/null
echo "==> deployed $IMAGE_TAG"
