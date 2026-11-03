"""Integration tests against a real database with db/init/*.sql loaded.

Skipped unless DATABASE_URL is set (CI provides a TimescaleDB service container).
TENANT_DATABASE_URL = connection as the demo client's read-only role.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

psycopg = pytest.importorskip("psycopg")

from app.db import Store  # noqa: E402
from app.rules import Rule, Transition  # noqa: E402

DB_URL = os.environ.get("DATABASE_URL")
TENANT_URL = os.environ.get("TENANT_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")

METRIC = "ci_metric"


@pytest.fixture
def store():
    s = Store(DB_URL)
    s.connect()
    with s.conn.cursor() as cur:  # start from a clean slate for the demo devices
        cur.execute(
            "DELETE FROM alert_events WHERE device_id IN "
            "(SELECT d.id FROM devices d JOIN tenants t ON t.id = d.tenant_id "
            "WHERE t.slug = 'baeckerei-demo')"
        )
    yield s
    with s.conn.cursor() as cur:
        cur.execute("DELETE FROM readings WHERE metric = %s", (METRIC,))
        cur.execute("DELETE FROM alert_events WHERE message LIKE 'ci-test%%'")
        cur.execute("DELETE FROM tenants WHERE slug = 'ci-other'")
    s.conn.close()


@pytest.fixture
def demo_device(store):
    devices, sites = store.load_devices()
    d = devices[("baeckerei-demo", "freezer-01")]
    assert sites[d.id] == "Filiale Hauptstraße"
    return d


def test_insert_readings_and_last_seen(store, demo_device):
    now = datetime.now(UTC).replace(microsecond=0)
    store.insert_readings([(now - timedelta(minutes=i), demo_device.id, METRIC, float(i)) for i in range(5)])
    store.touch_last_seen({demo_device.id: now})
    with store.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM readings WHERE metric = %s", (METRIC,))
        assert cur.fetchone()[0] == 5
        cur.execute("SELECT last_seen FROM devices WHERE id = %s", (demo_device.id,))
        assert cur.fetchone()[0] >= now
    # last_seen never moves backwards
    store.touch_last_seen({demo_device.id: now - timedelta(hours=1)})
    with store.conn.cursor() as cur:
        cur.execute("SELECT last_seen FROM devices WHERE id = %s", (demo_device.id,))
        assert cur.fetchone()[0] >= now


def test_alert_event_lifecycle_is_idempotent(store, demo_device):
    rule = next(r for r in store.load_rules() if r.device_id == demo_device.id)
    assert isinstance(rule, Rule)
    t0 = datetime.now(UTC)
    fire = Transition("fire", "threshold", demo_device, t0, rule, -10.0, -10.0)
    assert store.record_transition(fire, "ci-test fire") is True
    assert store.record_transition(fire, "ci-test fire") is False  # duplicate -> no 2nd notification
    resolve = Transition("resolve", "threshold", demo_device, t0 + timedelta(minutes=5), rule, -18.0, -8.5)
    assert store.record_transition(resolve, "ci-test resolve") is True
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT peak_value, resolved_at IS NOT NULL FROM alert_events WHERE message = 'ci-test fire'"
        )
        assert cur.fetchall() == [(-8.5, True)]


def test_offline_event_lifecycle(store, demo_device):
    t0 = datetime.now(UTC)
    fire = Transition("fire", "offline", demo_device, t0)
    assert store.record_transition(fire, "ci-test offline")
    assert not store.record_transition(fire, "ci-test offline")
    assert store.record_transition(Transition("resolve", "offline", demo_device, t0), "ci-test")


def test_channel_severity_filter(store, demo_device):
    with store.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (slug, name) VALUES ('ci-other', 'CI') RETURNING id",
        )
        tid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO notification_channels (tenant_id, kind, target, min_severity) VALUES "
            "(%s, 'email', 'warn@x', 'warning'), (%s, 'email', 'crit@x', 'critical')",
            (tid, tid),
        )
    assert store.channels_for(tid, "info") == []
    assert store.channels_for(tid, "warning") == [("email", "warn@x")]
    assert sorted(store.channels_for(tid, "critical")) == [("email", "crit@x"), ("email", "warn@x")]


@pytest.mark.skipif(not TENANT_URL, reason="TENANT_DATABASE_URL not set")
def test_tenant_role_is_isolated(store, demo_device):
    now = datetime.now(UTC)
    with store.conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (slug, name) VALUES ('ci-other', 'Other') RETURNING id")
        tid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO devices (tenant_id, external_id, name) VALUES (%s, 'x1', 'Other dev') RETURNING id",
            (tid,),
        )
        other_dev = cur.fetchone()[0]
    store.insert_readings([(now, demo_device.id, METRIC, 1.0), (now, other_dev, METRIC, 2.0)])

    with psycopg.connect(TENANT_URL, autocommit=True) as tconn, tconn.cursor() as cur:
        cur.execute("SELECT value FROM tenant_readings WHERE metric = %s", (METRIC,))
        assert cur.fetchall() == [(1.0,)]  # own data only
        cur.execute("SELECT count(*) FROM tenant_devices WHERE name = 'Other dev'")
        assert cur.fetchone()[0] == 0
        for table in ("readings", "devices", "tenants", "alert_rules"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(f"SELECT 1 FROM {table} LIMIT 1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM readings")


def test_hourly_rollup(store, demo_device):
    with store.conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if cur.fetchone() is None:
            pytest.skip("TimescaleDB not installed")
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    store.insert_readings(
        [(base + timedelta(minutes=10 * i), demo_device.id, METRIC, float(i)) for i in range(6)]
    )
    with store.conn.cursor() as cur:
        cur.execute("CALL refresh_continuous_aggregate('readings_hourly', NULL, NULL)")
        cur.execute(
            "SELECT min, max, n FROM readings_hourly WHERE device_id = %s AND metric = %s AND bucket = %s",
            (demo_device.id, METRIC, base),
        )
        assert cur.fetchone() == (0.0, 5.0, 6)
