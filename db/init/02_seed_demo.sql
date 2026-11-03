-- Demo data: one bakery client with a freezer and a cold room.
-- DEV ONLY – change the role password before using anything like this in production.

INSERT INTO tenants (slug, name, db_role) VALUES ('baeckerei-demo', 'Bäckerei Demo GmbH', 'grafana_baeckerei_demo');
CREATE ROLE grafana_baeckerei_demo LOGIN PASSWORD 'change-me' IN ROLE grafana_tenant;

INSERT INTO sites (tenant_id, name, address)
VALUES (1, 'Filiale Hauptstraße', 'Hauptstraße 1, 12345 Musterstadt');

INSERT INTO devices (tenant_id, site_id, external_id, name, kind, offline_after_s) VALUES
    (1, 1, 'freezer-01',  'Tiefkühlschrank Backstube', 'freezer',   900),
    (1, 1, 'coldroom-01', 'Kühlraum',                  'cold-room', 900);

INSERT INTO alert_rules (tenant_id, device_id, name, metric, operator, threshold, hysteresis, duration_s, severity) VALUES
    (1, 1, 'Tiefkühler zu warm', 'temperature', '>', -15, 1.0, 600, 'critical'),
    (1, 2, 'Kühlraum zu warm',   'temperature', '>',   7, 0.5, 900, 'warning'),
    -- tenant-wide: any device reporting low battery
    (1, NULL, 'Batterie schwach', 'battery',    '<',  15, 5.0,   0, 'info');

-- Webhook to a local echo endpoint is disabled by default; set a real target to try it.
INSERT INTO notification_channels (tenant_id, kind, target, min_severity, enabled) VALUES
    (1, 'email',   'owner@example.com',                 'warning', false),
    (1, 'webhook', 'https://example.com/kaar-webhook',  'info',    false);
