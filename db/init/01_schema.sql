-- Kaar Monitoring – core schema (PostgreSQL 16 + TimescaleDB)
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- Tenancy: tenant (client company) -> sites (buildings) -> devices (sensors)
-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
    id          serial PRIMARY KEY,
    slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{2,40}$'),  -- used in MQTT topics
    name        text NOT NULL,
    db_role     text UNIQUE,              -- read-only Postgres role used by this client's Grafana org
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sites (
    id          serial PRIMARY KEY,
    tenant_id   int  NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        text NOT NULL,
    address     text
);

CREATE TABLE devices (
    id              serial PRIMARY KEY,
    tenant_id       int  NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    site_id         int  REFERENCES sites(id) ON DELETE SET NULL,
    external_id     text NOT NULL CHECK (external_id ~ '^[A-Za-z0-9_-]{1,64}$'),  -- id in MQTT topic
    name            text NOT NULL,
    kind            text,                              -- e.g. freezer, cold-room, server-room, leak
    offline_after_s int  NOT NULL DEFAULT 1800 CHECK (offline_after_s >= 60),
    last_seen       timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, external_id)
);

-- ---------------------------------------------------------------------------
-- Time series: one row per (time, device, metric). Narrow table = any sensor type
-- ---------------------------------------------------------------------------
CREATE TABLE readings (
    time       timestamptz      NOT NULL,
    device_id  int              NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    metric     text             NOT NULL,
    value      double precision NOT NULL
);
SELECT create_hypertable('readings', 'time', chunk_time_interval => INTERVAL '7 days');
CREATE INDEX readings_device_metric_time ON readings (device_id, metric, time DESC);

ALTER TABLE readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id, metric'
);
SELECT add_compression_policy('readings', INTERVAL '14 days');
-- Keep raw data 2 years (adjust to what your clients must be able to show an inspector).
SELECT add_retention_policy('readings', INTERVAL '2 years');

-- Hourly rollup for dashboards and monthly reports
CREATE MATERIALIZED VIEW readings_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', time) AS bucket,
       device_id,
       metric,
       avg(value) AS avg,
       min(value) AS min,
       max(value) AS max,
       count(*)   AS n
FROM readings
GROUP BY 1, 2, 3
WITH NO DATA;

SELECT add_continuous_aggregate_policy('readings_hourly',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes');

-- ---------------------------------------------------------------------------
-- Alerting
-- ---------------------------------------------------------------------------
-- Scope: device_id set -> that device; else site_id set -> all devices of the site;
--        both NULL -> all devices of the tenant that report this metric.
CREATE TABLE alert_rules (
    id          serial PRIMARY KEY,
    tenant_id   int  NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_id   int  REFERENCES devices(id) ON DELETE CASCADE,
    site_id     int  REFERENCES sites(id) ON DELETE CASCADE,
    name        text NOT NULL,
    metric      text NOT NULL,
    operator    text NOT NULL CHECK (operator IN ('>', '<')),
    threshold   double precision NOT NULL,
    hysteresis  double precision NOT NULL DEFAULT 0 CHECK (hysteresis >= 0),
    duration_s  int  NOT NULL DEFAULT 0 CHECK (duration_s >= 0),   -- must be breached this long
    severity    text NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'critical')),
    enabled     boolean NOT NULL DEFAULT true
);

CREATE TABLE alert_events (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('threshold', 'offline')),
    rule_id      int  REFERENCES alert_rules(id) ON DELETE SET NULL,
    device_id    int  NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    severity     text NOT NULL,
    message      text NOT NULL,
    started_at   timestamptz NOT NULL,
    resolved_at  timestamptz,
    peak_value   double precision,
    acknowledged_at timestamptz,
    acknowledged_by text
);
-- At most one open event per rule+device, and one open offline event per device
CREATE UNIQUE INDEX alert_events_open_threshold ON alert_events (rule_id, device_id)
    WHERE resolved_at IS NULL AND kind = 'threshold';
CREATE UNIQUE INDEX alert_events_open_offline ON alert_events (device_id)
    WHERE resolved_at IS NULL AND kind = 'offline';
CREATE INDEX alert_events_device_time ON alert_events (device_id, started_at DESC);

CREATE TABLE notification_channels (
    id           serial PRIMARY KEY,
    tenant_id    int  NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('email', 'telegram', 'webhook')),
    target       text NOT NULL,      -- email address / telegram chat id / https URL
    min_severity text NOT NULL DEFAULT 'warning' CHECK (min_severity IN ('info', 'warning', 'critical')),
    enabled      boolean NOT NULL DEFAULT true
);

-- ---------------------------------------------------------------------------
-- Client-facing read access (Grafana org per client)
-- Each client gets a LOGIN role that is a member of grafana_tenant. That group may
-- only read the tenant_* views, which filter by current_user -> tenants.db_role.
-- ---------------------------------------------------------------------------
CREATE ROLE grafana_tenant NOLOGIN;

CREATE VIEW tenant_devices WITH (security_barrier) AS
SELECT d.id, d.external_id, d.name, d.kind, d.last_seen, s.name AS site
FROM devices d
JOIN tenants t ON t.id = d.tenant_id
LEFT JOIN sites s ON s.id = d.site_id
WHERE t.db_role = current_user;

CREATE VIEW tenant_readings WITH (security_barrier) AS
SELECT r.time, r.device_id, d.name AS device, r.metric, r.value
FROM readings r
JOIN devices d ON d.id = r.device_id
JOIN tenants t ON t.id = d.tenant_id
WHERE t.db_role = current_user;

CREATE VIEW tenant_readings_hourly WITH (security_barrier) AS
SELECT h.bucket, h.device_id, d.name AS device, h.metric, h.avg, h.min, h.max, h.n
FROM readings_hourly h
JOIN devices d ON d.id = h.device_id
JOIN tenants t ON t.id = d.tenant_id
WHERE t.db_role = current_user;

CREATE VIEW tenant_alert_events WITH (security_barrier) AS
SELECT e.id, e.kind, e.severity, e.message, e.started_at, e.resolved_at, e.peak_value,
       d.name AS device
FROM alert_events e
JOIN devices d ON d.id = e.device_id
JOIN tenants t ON t.id = d.tenant_id
WHERE t.db_role = current_user;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO grafana_tenant;
GRANT SELECT ON tenant_devices, tenant_readings, tenant_readings_hourly, tenant_alert_events
    TO grafana_tenant;
