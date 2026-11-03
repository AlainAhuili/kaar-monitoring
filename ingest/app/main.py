"""Ingest + alert service.

MQTT callbacks only enqueue messages. A single worker thread parses, batches
readings into TimescaleDB (COPY), evaluates alert rules, and dispatches
notifications. One thread = no DB locking concerns and ordered evaluation.
"""

from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
import psycopg

from . import config as config_mod
from .db import Store
from .notify import Notifier, format_message
from .parsing import InvalidMessage, parse_payload, parse_topic
from .rules import Device, Engine, Transition

log = logging.getLogger("ingest")


class Service:
    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        self.store = Store(cfg.database_url)
        self.engine = Engine()
        self.notifier = Notifier(cfg)
        self.inbox: queue.Queue = queue.Queue(maxsize=100_000)
        self.stop = threading.Event()
        self.devices: dict[tuple[str, str], Device] = {}
        self.sites: dict[int, str] = {}
        self._last_device_refresh = 0.0
        self.buffer: list = []
        self.seen: dict[int, datetime] = {}
        self.stats = {"ok": 0, "invalid": 0, "unknown_device": 0, "dropped": 0}

    # -- MQTT ------------------------------------------------------------------
    def _on_connect(self, client, _userdata, _flags, reason_code, _props):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        topic = f"{self.cfg.mqtt_prefix}/+/+/telemetry"
        client.subscribe(topic, qos=1)
        log.info("MQTT connected, subscribed to %s", topic)

    def _on_message(self, _client, _userdata, msg):
        try:
            self.inbox.put_nowait((msg.topic, msg.payload, datetime.now(UTC)))
        except queue.Full:
            self.stats["dropped"] += 1

    def _mqtt(self) -> mqtt.Client:
        c = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="kaar-ingest", clean_session=False
        )  # persistent session: broker queues QoS1 while we restart
        c.username_pw_set(self.cfg.mqtt_user, self.cfg.mqtt_password)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.reconnect_delay_set(1, 30)
        c.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=30)
        return c

    # -- state -----------------------------------------------------------------
    def _refresh_devices(self) -> None:
        self.devices, self.sites = self.store.load_devices()
        self._last_device_refresh = time.monotonic()

    def _device(self, tenant: str, ext: str) -> Device | None:
        d = self.devices.get((tenant, ext))
        # newly provisioned device? refresh at most every 30 s
        if d is None and time.monotonic() - self._last_device_refresh > 30:
            self._refresh_devices()
            d = self.devices.get((tenant, ext))
        return d

    def _bootstrap(self) -> None:
        self.store.connect()
        self._refresh_devices()
        self.engine.set_rules(self.store.load_rules())
        for did, ts in self.store.load_last_seen().items():
            self.engine.restore_last_seen(did, ts)
        for kind, rule_id, device_id, peak in self.store.load_open_events():
            if kind == "threshold" and rule_id is not None:
                self.engine.restore_open_threshold(rule_id, device_id, peak)
            elif kind == "offline":
                self.engine.restore_offline(device_id)
        log.info("loaded %d devices", len(self.devices))

    # -- processing ------------------------------------------------------------
    def _handle(self, topic: str, payload: bytes, received: datetime) -> None:
        try:
            tenant, ext = parse_topic(topic, self.cfg.mqtt_prefix)
            ts, metrics = parse_payload(payload, received)
        except InvalidMessage as e:
            self.stats["invalid"] += 1
            log.debug("invalid message on %s: %s", topic, e)
            return
        device = self._device(tenant, ext)
        if device is None:
            self.stats["unknown_device"] += 1
            log.warning("unknown device %s/%s – provision it in the devices table", tenant, ext)
            return
        self.stats["ok"] += 1
        prev = self.seen.get(device.id)
        if prev is None or ts > prev:
            self.seen[device.id] = ts
        for metric, value in metrics.items():
            self.buffer.append((ts, device.id, metric, value))
            for t in self.engine.observe(device, metric, value, ts, received):
                self._dispatch(t)

    def _dispatch(self, t: Transition) -> None:
        subject, body = format_message(t, self.sites.get(t.device.id))
        if not self.store.record_transition(t, body):
            return  # duplicate (e.g. after restart) – don't notify twice
        log.info("%s", subject)
        payload = {
            "action": t.action,
            "kind": t.kind,
            "severity": t.severity,
            "device": t.device.external_id,
            "device_name": t.device.name,
            "rule": t.rule.name if t.rule else None,
            "value": t.value,
            "peak": t.peak,
            "at": t.at.isoformat(),
            "subject": subject,
            "message": body,
        }
        for kind, target in self.store.channels_for(t.device.tenant_id, t.severity):
            self.notifier.send(kind, target, subject, body, payload)

    def _flush(self) -> None:
        if not self.buffer and not self.seen:
            return
        try:
            if self.buffer:
                self.store.insert_readings(self.buffer)
            if self.seen:
                self.store.touch_last_seen(self.seen)
            self.buffer, self.seen = [], {}
        except psycopg.Error:
            log.exception("flush failed, will retry (%d rows buffered)", len(self.buffer))
            if len(self.buffer) > 200_000:  # don't grow forever while DB is down
                self.stats["dropped"] += len(self.buffer)
                self.buffer = []
            self.store.conn = None

    def run(self) -> None:
        self._bootstrap()
        client = self._mqtt()
        client.loop_start()
        last_flush = last_rules = last_offline = last_stats = time.monotonic()
        try:
            while not self.stop.is_set():
                try:
                    self._handle(*self.inbox.get(timeout=0.5))
                except queue.Empty:
                    pass
                now = time.monotonic()
                if len(self.buffer) >= self.cfg.batch_size or now - last_flush >= self.cfg.flush_interval_s:
                    self._flush()
                    last_flush = now
                try:
                    if now - last_rules >= self.cfg.rules_reload_s:
                        self.engine.set_rules(self.store.load_rules())
                        self._refresh_devices()
                        last_rules = now
                    if now - last_offline >= self.cfg.offline_check_s:
                        all_devices = list(self.devices.values())
                        for t in self.engine.check_offline(all_devices, datetime.now(UTC)):
                            self._dispatch(t)
                        last_offline = now
                except psycopg.Error:
                    log.exception("periodic task failed")
                    self.store.conn = None
                if now - last_stats >= 300:
                    log.info("stats %s, queue=%d", self.stats, self.inbox.qsize())
                    last_stats = now
        finally:
            client.loop_stop()
            client.disconnect()
            while not self.inbox.empty():
                self._handle(*self.inbox.get_nowait())
            self._flush()
            self.notifier.shutdown()
            log.info("stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    svc = Service(config_mod.load())
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: svc.stop.set())
    svc.run()


if __name__ == "__main__":
    main()
