"""Topic and payload parsing for incoming telemetry.

Topic:   kaar/<tenant-slug>/<device-external-id>/telemetry
Payload: {"ts": "2026-11-03T20:00:00Z" | 1762200000, "metrics": {"temperature": -18.4, "battery": 97}}
         "ts" is optional (server time is used). Up to MAX_METRICS numeric metrics.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta

MAX_METRICS = 32
MAX_FUTURE = timedelta(minutes=5)
MAX_PAST = timedelta(days=7)  # gateways may buffer while offline
_SLUG = re.compile(r"^[a-z0-9-]{2,40}$")
_EXT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_METRIC = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class InvalidMessage(ValueError):
    pass


def parse_topic(topic: str, prefix: str = "kaar") -> tuple[str, str]:
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != prefix or parts[3] != "telemetry":
        raise InvalidMessage(f"unexpected topic {topic!r}")
    tenant, device = parts[1], parts[2]
    if not _SLUG.match(tenant) or not _EXT_ID.match(device):
        raise InvalidMessage(f"invalid ids in topic {topic!r}")
    return tenant, device


def _parse_ts(raw, now: datetime) -> datetime:
    if raw is None:
        return now
    if isinstance(raw, bool):
        raise InvalidMessage("bad ts")
    if isinstance(raw, int | float):
        if raw > 1e12:  # milliseconds
            raw = raw / 1000
        ts = datetime.fromtimestamp(raw, tz=UTC)
    elif isinstance(raw, str):
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as e:
            raise InvalidMessage("bad ts") from e
        if ts.tzinfo is None:
            raise InvalidMessage("ts must include a timezone")
    else:
        raise InvalidMessage("bad ts")
    if ts > now + MAX_FUTURE or ts < now - MAX_PAST:
        raise InvalidMessage(f"ts out of range: {ts.isoformat()}")
    return ts


def parse_payload(payload: bytes, now: datetime) -> tuple[datetime, dict[str, float]]:
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as e:
        raise InvalidMessage("payload is not JSON") from e
    if not isinstance(data, dict) or not isinstance(data.get("metrics"), dict):
        raise InvalidMessage("payload needs a 'metrics' object")
    raw_metrics = data["metrics"]
    if not raw_metrics or len(raw_metrics) > MAX_METRICS:
        raise InvalidMessage("metrics must contain 1..32 entries")

    metrics: dict[str, float] = {}
    for k, v in raw_metrics.items():
        if not isinstance(k, str) or not _METRIC.match(k):
            raise InvalidMessage(f"bad metric name {k!r}")
        if isinstance(v, bool):
            v = 1.0 if v else 0.0  # e.g. leak: true
        if not isinstance(v, int | float) or not math.isfinite(v):
            raise InvalidMessage(f"metric {k} is not a finite number")
        metrics[k] = float(v)
    return _parse_ts(data.get("ts"), now), metrics
