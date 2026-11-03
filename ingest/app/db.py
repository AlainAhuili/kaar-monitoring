"""Database access. Used from a single worker thread only."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import psycopg

from .rules import SEVERITY_ORDER, Device, Rule, Transition

log = logging.getLogger("db")


class Store:
    def __init__(self, url: str) -> None:
        self.url = url
        self.conn: psycopg.Connection | None = None

    def connect(self) -> None:
        delay = 1.0
        while True:
            try:
                self.conn = psycopg.connect(self.url, autocommit=True)
                log.info("connected to database")
                return
            except psycopg.OperationalError as e:
                log.warning("database not reachable (%s), retrying in %.0fs", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)

    def _cur(self):
        if self.conn is None or self.conn.closed:
            self.connect()
        return self.conn.cursor()

    # -- loading -------------------------------------------------------------
    def load_devices(self) -> tuple[dict[tuple[str, str], Device], dict[int, str]]:
        """Returns {(tenant_slug, external_id): Device} and {device_id: site name}."""
        with self._cur() as cur:
            cur.execute("""
                SELECT d.id, d.tenant_id, d.site_id, d.external_id, d.name, d.offline_after_s,
                       t.slug, s.name
                FROM devices d JOIN tenants t ON t.id = d.tenant_id
                LEFT JOIN sites s ON s.id = d.site_id""")
            devices, sites = {}, {}
            for did, tid, sid, ext, name, off, slug, site in cur.fetchall():
                devices[(slug, ext)] = Device(did, tid, sid, ext, name, off)
                sites[did] = site
            return devices, sites

    def load_last_seen(self) -> dict[int, datetime]:
        with self._cur() as cur:
            cur.execute("SELECT id, last_seen FROM devices WHERE last_seen IS NOT NULL")
            return dict(cur.fetchall())

    def load_rules(self) -> list[Rule]:
        with self._cur() as cur:
            cur.execute("""
                SELECT id, tenant_id, name, metric, operator, threshold, hysteresis, duration_s,
                       severity, device_id, site_id
                FROM alert_rules WHERE enabled""")
            return [Rule(*row) for row in cur.fetchall()]

    def load_open_events(self) -> list[tuple[str, int | None, int, float | None]]:
        with self._cur() as cur:
            cur.execute("""SELECT kind, rule_id, device_id, peak_value
                           FROM alert_events WHERE resolved_at IS NULL""")
            return cur.fetchall()

    # -- writing -------------------------------------------------------------
    def insert_readings(self, rows: list[tuple[datetime, int, str, float]]) -> None:
        with self._cur() as cur:
            with cur.copy("COPY readings (time, device_id, metric, value) FROM STDIN") as cp:
                for row in rows:
                    cp.write_row(row)

    def touch_last_seen(self, seen: dict[int, datetime]) -> None:
        with self._cur() as cur:
            cur.executemany(
                "UPDATE devices SET last_seen = GREATEST(COALESCE(last_seen, %s), %s) WHERE id = %s",
                [(ts, ts, did) for did, ts in seen.items()],
            )

    def record_transition(self, t: Transition, message: str) -> bool:
        """Persist an alert transition. Returns False if it was a duplicate."""
        with self._cur() as cur:
            if t.action == "fire":
                cur.execute(
                    """
                    INSERT INTO alert_events
                        (kind, rule_id, device_id, severity, message, started_at, peak_value)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING RETURNING id""",
                    (t.kind, t.rule.id if t.rule else None, t.device.id, t.severity, message, t.at, t.peak),
                )
                return cur.fetchone() is not None
            if t.kind == "threshold":
                cur.execute(
                    """
                    UPDATE alert_events SET resolved_at = %s, peak_value = %s
                    WHERE kind = 'threshold' AND rule_id = %s AND device_id = %s AND resolved_at IS NULL""",
                    (t.at, t.peak, t.rule.id, t.device.id),
                )
            else:
                cur.execute(
                    """
                    UPDATE alert_events SET resolved_at = %s
                    WHERE kind = 'offline' AND device_id = %s AND resolved_at IS NULL""",
                    (t.at, t.device.id),
                )
            return cur.rowcount > 0

    def channels_for(self, tenant_id: int, severity: str) -> list[tuple[str, str]]:
        with self._cur() as cur:
            cur.execute(
                """SELECT kind, target, min_severity FROM notification_channels
                           WHERE tenant_id = %s AND enabled""",
                (tenant_id,),
            )
            level = SEVERITY_ORDER.get(severity, 1)
            return [(k, tgt) for k, tgt, min_sev in cur.fetchall() if level >= SEVERITY_ORDER.get(min_sev, 1)]
