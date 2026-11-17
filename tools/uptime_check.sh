#!/usr/bin/env bash
# External uptime check for the public demo. Run by .github/workflows/uptime.yml,
# or by hand:  DEMO_HOST=demo.kaarsmarttechs.com tools/uptime_check.sh
#
# Checks (each one runs even if an earlier one failed):
#   1. Dashboard: https://$DEMO_HOST/api/health answers and Grafana's database is "ok"
#   2. MQTT TLS:  $MQTT_HOST:8883 accepts a TLS handshake with a valid certificate
#   3. Certs:     HTTPS certificates of both hosts are valid for at least $CERT_MIN_DAYS days
# Exit code: 0 all good, 1 at least one check failed.
set -uo pipefail

DEMO_HOST="${DEMO_HOST:?set DEMO_HOST, e.g. demo.kaarsmarttechs.com}"
MQTT_HOST="${MQTT_HOST:-mqtt.${DEMO_HOST#*.}}"
MQTT_PORT="${MQTT_PORT:-8883}"
CERT_MIN_DAYS="${CERT_MIN_DAYS:-14}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"

failures=0
report() {  # report <ok|fail> <check> <detail>
  local icon="✅" detail
  if [ "$1" = fail ]; then icon="❌"; failures=$((failures + 1)); fi
  # One line, no table-breaking pipes, bounded length (curl retries repeat their error).
  detail=$(printf '%s' "$3" | tr '\n' ' ' | tr -s ' ' | tr '|' '/' | cut -c1-200)
  printf '%s %s: %s\n' "$icon" "$2" "$detail"
  printf '| %s | %s | %s |\n' "$icon" "$2" "$detail" >> "$SUMMARY"
}

printf '| | Check | Result |\n|---|---|---|\n' >> "$SUMMARY"

# 1. Dashboard health (retries ride out a container restart during a deploy)
if body=$(curl -fsS --max-time 20 --retry 3 --retry-all-errors --retry-delay 10 \
    "https://${DEMO_HOST}/api/health" 2>&1); then
  db=$(printf '%s' "$body" | jq -r '.database // "missing"' 2>/dev/null || echo "not JSON")
  if [ "$db" = ok ]; then
    report ok "Dashboard" "https://${DEMO_HOST}/api/health, database ok"
  else
    report fail "Dashboard" "health answered but database is '${db}'"
  fi
else
  report fail "Dashboard" "https://${DEMO_HOST}/api/health unreachable: $(printf '%s' "$body" | tail -n1)"
fi

# 2. MQTT over TLS (what gateways and ESP32 devices connect to)
tls_out=$(echo | timeout 20 openssl s_client -connect "${MQTT_HOST}:${MQTT_PORT}" \
  -servername "${MQTT_HOST}" -verify_return_error 2>&1)
if printf '%s' "$tls_out" | grep -q 'Verify return code: 0 (ok)'; then
  report ok "MQTT TLS" "${MQTT_HOST}:${MQTT_PORT} handshake ok, certificate valid"
else
  reason=$(printf '%s' "$tls_out" | grep -m1 -iE 'error|refused|timed out|verify return' || echo "no TLS handshake")
  report fail "MQTT TLS" "${MQTT_HOST}:${MQTT_PORT}: ${reason}"
fi

# 3. Certificate lifetime (Caddy renews automatically; this catches a renewal that stopped)
for host in "$DEMO_HOST" "$MQTT_HOST"; do
  cert=$(echo | timeout 20 openssl s_client -connect "${host}:443" -servername "$host" 2>/dev/null \
    | openssl x509 2>/dev/null)
  if [ -z "$cert" ]; then
    report fail "Certificate ${host}" "could not read the HTTPS certificate"
  elif printf '%s\n' "$cert" | openssl x509 -noout -checkend $((CERT_MIN_DAYS * 86400)) >/dev/null; then
    expiry=$(printf '%s\n' "$cert" | openssl x509 -noout -enddate | cut -d= -f2)
    report ok "Certificate ${host}" "valid until ${expiry}"
  else
    report fail "Certificate ${host}" "expires within ${CERT_MIN_DAYS} days"
  fi
done

if [ "$failures" -gt 0 ]; then
  echo "${failures} check(s) failed"
  exit 1
fi
echo "All checks passed"
