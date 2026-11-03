# Kaar Monitoring – sensor monitoring as a service

[![CI/CD](https://github.com/AlainAhuili/kaar-monitoring/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/AlainAhuili/kaar-monitoring/actions/workflows/ci-cd.yml)
**Live demo:** https://demo.kaarsmarttechs.com (read-only dashboard, simulated bakery freezer with an incident every 6 h)

Starter platform for Kaar Smart Techs: business sensors (cold storage, server rooms,
leaks, energy) → MQTT → TimescaleDB → alerts + Grafana dashboards, one system for many clients.

## Architecture

```mermaid
flowchart LR
  S[Sensors<br/>LoRaWAN / Zigbee / ESP32] --> G[Gateway at client site]
  G -- "MQTT (TLS)<br/>kaar/&lt;tenant&gt;/&lt;device&gt;/telemetry" --> B[Mosquitto<br/>ACL per tenant]
  B --> I[Ingest service<br/>parse · batch · rules]
  I -- COPY --> DB[(TimescaleDB<br/>readings hypertable<br/>hourly rollup)]
  I -- alert events --> DB
  I --> N[Email / Telegram / Webhook]
  DB --> GF[Grafana<br/>1 org per client]
```

**Why these choices**
- **MQTT + one topic per device**: what every gateway (Milesight, Dragino, ChirpStack, ESP32) can speak. With LoRaWAN, ChirpStack's MQTT integration is reshaped into this topic format by a small bridge or a gateway-side script.
- **Narrow readings table** `(time, device_id, metric, value)`: any new sensor type works with no schema change. TimescaleDB compresses it heavily after 14 days (about 10–20×); retention defaults to 2 years.
- **A single worker thread in ingest**: ordered rule evaluation and no DB locking. A small VPS handles a few thousand messages per second, far beyond what a few hundred battery sensors send (about 1 msg/min each).
- **Rules in the database**: change thresholds without redeploying. They are reloaded every 60 s.

## Data model

```
tenants (client company, slug used in MQTT, db_role for Grafana)
 └─ sites (buildings)
     └─ devices (external_id from topic, offline_after_s, last_seen)
          └─ readings (hypertable)          readings_hourly (continuous aggregate)
alert_rules (scope: device | site | whole tenant; metric, > or <, threshold, hysteresis, duration, severity)
alert_events (threshold/offline, started_at, resolved_at, peak_value, acknowledged_*)
notification_channels (per tenant: email | telegram | webhook, min_severity)
```

## Alert semantics

| Concept | Behaviour |
|---|---|
| **Duration** | Fires only after the value has been breached continuously for `duration_s` (e.g. door opened for 2 min ≠ alarm). |
| **Hysteresis** | Resolves only when back past the threshold by `hysteresis`, which prevents flapping. |
| **Peak** | Worst value during the incident is stored. Useful for reports ("max −8.3 °C for 26 min"). |
| **Offline** | No message *received* for `offline_after_s` → offline alert. Based on arrival time, so a gateway uploading buffered data doesn't trigger it. This also covers "sensor died while in breach". |
| **Restart-safe** | Open events are reloaded at startup; unique indexes prevent duplicate events and duplicate notifications. MQTT uses a persistent session (QoS 1) so messages are queued while ingest restarts. |

## Multi-tenancy and security

- **MQTT**: each client's gateway gets a user named after the tenant slug. The ACL (`pattern write kaar/%u/+/telemetry`) means it can only publish into its own tenant. Unknown devices are dropped and logged, so devices must be provisioned first.
- **Client dashboards**: each client gets a Postgres LOGIN role (member of `grafana_tenant`) that can only read the `tenant_*` views, filtered by `current_user`. In Grafana: create an org per client, add a datasource with that client's role, and build dashboards on `tenant_readings` / `tenant_readings_hourly` / `tenant_alert_events`. Even a hand-written SQL query can't see another client's data.
- The provisioned Grafana datasource deliberately uses the demo client's **read-only role**, because Grafana viewers (including anonymous visitors of the public demo) can send arbitrary queries to a datasource. Only the ingest service uses the `kaar` owner role; admin work goes through `docker compose exec db psql` or an SSH tunnel.

## Run it (dev)

```bash
cp .env.example .env              # set passwords
set -a; . ./.env; set +a
tools/mqtt_user.sh ingest "$MQTT_INGEST_PASSWORD"
tools/mqtt_user.sh baeckerei-demo "$MQTT_DEMO_GATEWAY_PASSWORD"
docker compose --profile demo up -d --build    # 'demo' profile = simulator container
docker compose logs -f ingest                  # see [CRITICAL] ... then [OK] ...
```
Grafana: http://localhost:3000 → *Kaar – Sensor Overview* (admin login with `GRAFANA_ADMIN_PASSWORD`).
The simulator replays the last 3 h (including a freezer incident) and then keeps sending in real time.

Payload format the gateways must send:
```json
topic: kaar/baeckerei-demo/freezer-01/telemetry
{"ts": "2026-11-03T20:00:00Z", "metrics": {"temperature": -18.4, "humidity": 61, "battery": 97, "door_open": false}}
```
`ts` is optional (ISO 8601 with timezone, or epoch s/ms; up to 7 days back is accepted for buffered uploads).

Tests: `pip install -r ingest/requirements.txt -r ingest/requirements-dev.txt && cd ingest && pytest`
(DB integration tests run when `DATABASE_URL` / `TENANT_DATABASE_URL` are set, as in CI).

## CI/CD

`.github/workflows/ci-cd.yml`:

```mermaid
flowchart LR
  PR[push / PR] --> T[Lint & test<br/>ruff · pytest · shellcheck<br/>schema + integration tests<br/>on real TimescaleDB<br/>compose validation]
  T -- main only --> B[Build & push image<br/>GHCR, tagged with git SHA<br/>buildx + GHA cache]
  B --> D[Deploy<br/>rsync config · SSH deploy.sh<br/>pull · up -d · stability check]
  D --> S[Smoke test<br/>HTTPS /api/health + dashboard]
```

- Every image is tagged with the commit SHA. **Rollback** = *Actions → CI/CD → Run workflow* with `image_tag` = an older SHA (skips the build, redeploys that image).
- The `production` GitHub environment can require manual approval before deploys (Settings → Environments).
- `deploy.sh` fails the pipeline if the ingest container isn't stable 15 s after the rollout.

## Deployment (Oracle Cloud Always Free, €0)

Runs on Oracle's Always Free Arm server (Ampere A1). CI builds the image for `linux/amd64` and `linux/arm64`.
Hetzner (about €5/month, x86) also works: use `deploy/cloud-init-hetzner.yml` instead and skip the Oracle firewall steps.

One-time setup, about 45 minutes:

1. **Keys (laptop):** `ssh-keygen -t ed25519 -f ~/.ssh/kaar-gh-deploy -N "" -C github-actions`. You need the public key of this deploy key plus your own `~/.ssh/id_ed25519.pub`.
2. **Oracle account:** sign up at oracle.com/cloud/free, home region **Germany Central (Frankfurt)**. The home region can't be changed later. A card is required for identity verification. Recommended: upgrade to *Pay As You Go* (Always Free resources stay free) and set a budget alert of €1. PAYG accounts aren't affected by Oracle's reclaiming of idle Always Free instances, which this low-load demo might otherwise trigger.
3. **Create instance:** Compute → Instances → *Create instance*
   - Image: **Canonical Ubuntu 24.04** · Shape: **Ampere → VM.Standard.A1.Flex, 2 OCPU, 12 GB** (the Always Free maximum, stay at or below it)
   - Networking: create a new VCN with a public subnet, *Assign a public IPv4 address*
   - SSH keys: paste your laptop public key
   - *Show advanced options → Management → Paste cloud-init script*: `deploy/cloud-init-oracle.yml` with both public keys filled in
   - "Out of capacity"? Try another availability domain (AD-1/2/3) or retry later.
4. **Open the ports in the Security List** (Oracle's network firewall): Instance → subnet → *Security Lists* → Default → *Add Ingress Rules*, source `0.0.0.0/0`, TCP, destination ports **80**, **443**, **8883** (one rule each, or `80,443,8883`). Port 22 is already open. The server's own iptables rules are opened by cloud-init.
5. **DNS at IONOS:** A records `demo` and `mqtt` → the instance's public IP.
6. **Server (once):** wait about 5 minutes, then `ssh deploy@<ip>`:
   ```bash
   cloud-init status --wait
   cd /opt/kaar
   curl -fsSLO https://raw.githubusercontent.com/AlainAhuili/kaar-monitoring/main/deploy/setup-env.sh
   bash setup-env.sh kaarsmarttechs.com you@example.com    # creates .env + MQTT users, prints Grafana admin password
   ```
7. **GitHub → Settings → Secrets and variables → Actions:**
   - Secrets: `DEPLOY_HOST` (IP), `DEPLOY_USER` = `deploy`, `DEPLOY_SSH_KEY` (contents of `~/.ssh/kaar-gh-deploy`), `DEPLOY_KNOWN_HOSTS` (`ssh-keyscan -t ed25519 <ip>`)
   - **Repository** variable: `DOMAIN` = `demo.kaarsmarttechs.com` (deploys are skipped while it's unset)
8. **Deploy:** Actions → CI/CD → *Run workflow*. Caddy gets the HTTPS certificates as soon as DNS resolves.

The container image on GHCR must be public, or log the server in with `docker login ghcr.io` using a read-only token.

A real gateway or ESP32 then connects to `mqtts://mqtt.kaarsmarttechs.com:8883` with its tenant credentials.

**Schema changes:** `db/init` only runs when the database is first created. For changes after go-live, add a migration tool (e.g. dbmate) and a migration step in `deploy.sh`.

## Production checklist

- [x] MQTT over TLS on 8883 (Let's Encrypt via Caddy), internal 1883 not published; Grafana only behind Caddy with HTTPS
- [x] Postgres never published; public dashboard uses a read-only tenant role
- [x] SSH keys only, firewall (Oracle Security List + iptables / ufw on Hetzner), fail2ban, unattended upgrades (cloud-init)
- [ ] Strong, unique passwords per gateway
- [ ] Nightly `pg_dump` (or pgBackRest) off-site, and test a restore once
- [ ] Monitor the monitor: an external uptime check (e.g. UptimeRobot on `https://DOMAIN/api/health`) + an alert if ingest logs stop (the `stats` line comes every 5 min)
- [ ] Choose the retention period per client contract (what they must be able to show an inspector or auditor)
- [ ] Hosted in an EU data centre (Oracle Frankfurt or Hetzner) and covered by a data processing agreement (AV-Vertrag) with each client

## Next steps

1. **Monthly PDF report per client** (min/max/avg per device from `readings_hourly`, list of incidents with peak and duration). This is the deliverable restaurants and care homes pay for.
2. **Alert acknowledgement** link in emails (`acknowledged_at/by`) plus escalation if not acknowledged within X minutes.
3. **Small admin UI** for onboarding (tenant, site, devices, rules, channels) instead of SQL.
4. **ChirpStack bridge** for LoRaWAN sensors → this topic format.
