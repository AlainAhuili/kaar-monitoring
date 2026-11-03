#!/usr/bin/env python3
"""Publish simulated sensor data for the demo tenant.

Replays the last 3 hours quickly (fake timestamps, 1 reading/min/device) including
a freezer door left open (warms to about -9 °C for ~25 min), then keeps sending in
real time. You should see the freezer alert FIRE and RESOLVE within seconds.

  pip install "paho-mqtt>=2.1"
  python tools/simulate.py --password <baeckerei-demo MQTT password>
"""

import argparse
import json
import math
import os
import random
import time
from datetime import UTC, datetime, timedelta

import paho.mqtt.client as mqtt

TENANT = "baeckerei-demo"


def freezer_temp(minute: int) -> float:
    base = -19.5 + 0.6 * math.sin(minute / 9)  # compressor cycling
    m = minute % 360  # an incident every 6 hours (keeps the demo alive)
    if 60 <= m < 95:  # door left open
        base += min(10.5, (m - 60) * 0.9)
    elif 95 <= m < 110:  # recovering
        base += max(0.0, 10.5 - (m - 95) * 1.2)
    return round(base + random.uniform(-0.2, 0.2), 2)


def coldroom_temp(minute: int) -> float:
    return round(4.0 + 0.4 * math.sin(minute / 12) + random.uniform(-0.15, 0.15), 2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--user", default=os.environ.get("MQTT_USER", TENANT))
    ap.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"))
    ap.add_argument("--hours", type=float, default=3)
    args = ap.parse_args()
    if not args.password:
        ap.error("--password or MQTT_PASSWORD is required")

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="kaar-sim")
    c.username_pw_set(args.user, args.password)
    c.reconnect_delay_set(1, 30)
    c.connect_async(args.host, args.port)
    c.loop_start()
    while not c.is_connected():  # broker may still be starting (container restart policy handles later drops)
        time.sleep(0.5)

    start = datetime.now(UTC) - timedelta(hours=args.hours)
    minute = 0
    while True:
        ts = start + timedelta(minutes=minute)
        now = datetime.now(UTC)
        if ts > now:
            time.sleep((ts - now).total_seconds())
        battery = max(5, 98 - minute // 120)
        for ext, temp in (("freezer-01", freezer_temp(minute)), ("coldroom-01", coldroom_temp(minute))):
            payload = {"ts": ts.isoformat(), "metrics": {"temperature": temp, "battery": battery}}
            c.publish(f"kaar/{TENANT}/{ext}/telemetry", json.dumps(payload), qos=1).wait_for_publish()
        if minute % 30 == 0:
            print(f"{ts:%H:%M} freezer={freezer_temp(minute):.1f}°C")
        minute += 1
        if ts < now:
            time.sleep(0.02)


if __name__ == "__main__":
    main()
