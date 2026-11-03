#!/usr/bin/env bash
# Add/update an MQTT user in mosquitto/passwd with the right file ownership for the
# eclipse-mosquitto image (uid/gid 1883). Usage:
#   tools/mqtt_user.sh ingest '<password>'           # the ingest service
#   tools/mqtt_user.sh baeckerei-demo '<password>'   # a gateway; username == tenant slug
set -euo pipefail
cd "$(dirname "$0")/.."
user="$1"; pass="$2"
touch mosquitto/passwd
docker run --rm -v "$PWD/mosquitto:/m" eclipse-mosquitto:2 sh -c \
  "mosquitto_passwd -b /m/passwd '$user' '$pass' && chown 1883:1883 /m/passwd && chmod 600 /m/passwd"
echo "user '$user' set. Restart broker: docker compose restart mosquitto"
