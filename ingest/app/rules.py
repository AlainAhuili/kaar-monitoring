"""Pure alert-rule engine (no I/O) so it can be unit-tested in isolation.

Semantics
---------
* A threshold rule FIRES when the metric has been continuously breached for at
  least ``duration_s``. Duration is measured between received readings, so it
  fires on the first reading at/after the duration has elapsed.
* It RESOLVES when the value is back past ``threshold`` by at least
  ``hysteresis`` (stops flapping around the threshold).
* A device goes OFFLINE when nothing was received for ``offline_after_s`` and
  comes back online with the next reading. Offline also covers "sensor stopped
  sending while it was in breach".
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass(frozen=True)
class Device:
    id: int
    tenant_id: int
    site_id: int | None
    external_id: str
    name: str
    offline_after_s: int = 1800


@dataclass(frozen=True)
class Rule:
    id: int
    tenant_id: int
    name: str
    metric: str
    operator: Literal[">", "<"]
    threshold: float
    hysteresis: float = 0.0
    duration_s: int = 0
    severity: str = "warning"
    device_id: int | None = None
    site_id: int | None = None

    def applies_to(self, device: Device) -> bool:
        if self.tenant_id != device.tenant_id:
            return False
        if self.device_id is not None:
            return self.device_id == device.id
        if self.site_id is not None:
            return self.site_id == device.site_id
        return True

    def breached(self, value: float) -> bool:
        return value > self.threshold if self.operator == ">" else value < self.threshold

    def cleared(self, value: float) -> bool:
        if self.operator == ">":
            return value <= self.threshold - self.hysteresis
        return value >= self.threshold + self.hysteresis

    def worse(self, a: float | None, b: float) -> float:
        """Return the 'peak' of a breach (max for '>', min for '<')."""
        if a is None:
            return b
        return max(a, b) if self.operator == ">" else min(a, b)


@dataclass
class _State:
    breach_since: datetime | None = None
    active: bool = False
    peak: float | None = None


@dataclass(frozen=True)
class Transition:
    action: Literal["fire", "resolve"]
    kind: Literal["threshold", "offline"]
    device: Device
    at: datetime
    rule: Rule | None = None
    value: float | None = None
    peak: float | None = None

    @property
    def severity(self) -> str:
        return self.rule.severity if self.rule else "warning"


class Engine:
    def __init__(self) -> None:
        self._rules: dict[int, Rule] = {}
        self._by_metric: dict[str, list[Rule]] = {}
        self._states: dict[tuple[int, int], _State] = {}
        self._last_seen: dict[int, datetime] = {}
        self._offline: set[int] = set()

    # -- configuration -------------------------------------------------------
    def set_rules(self, rules: Iterable[Rule]) -> None:
        self._rules = {r.id: r for r in rules}
        self._by_metric = {}
        for r in self._rules.values():
            self._by_metric.setdefault(r.metric, []).append(r)
        # forget state of rules that were deleted/disabled
        self._states = {k: v for k, v in self._states.items() if k[0] in self._rules}

    def restore_open_threshold(self, rule_id: int, device_id: int, peak: float | None) -> None:
        """Rebuild state after a restart from open alert_events."""
        self._states[(rule_id, device_id)] = _State(active=True, peak=peak)

    def restore_offline(self, device_id: int) -> None:
        self._offline.add(device_id)

    def restore_last_seen(self, device_id: int, ts: datetime) -> None:
        self._last_seen[device_id] = ts

    # -- evaluation ----------------------------------------------------------
    def observe(
        self, device: Device, metric: str, value: float, ts: datetime, received: datetime | None = None
    ) -> list[Transition]:
        """``ts`` = measurement time (drives rule durations); ``received`` = wall-clock
        arrival time (drives offline detection, so replayed/buffered data from a
        gateway doesn't make a device look offline)."""
        if not math.isfinite(value):
            return []
        out: list[Transition] = []
        received = received or ts

        prev = self._last_seen.get(device.id)
        if prev is None or received > prev:
            self._last_seen[device.id] = received
        if device.id in self._offline:
            self._offline.discard(device.id)
            out.append(Transition("resolve", "offline", device, received))

        for rule in self._by_metric.get(metric, ()):
            if not rule.applies_to(device):
                continue
            st = self._states.setdefault((rule.id, device.id), _State())
            if st.active:
                st.peak = rule.worse(st.peak, value)
                if rule.cleared(value):
                    out.append(Transition("resolve", "threshold", device, ts, rule, value, st.peak))
                    self._states[(rule.id, device.id)] = _State()
            elif rule.breached(value):
                if st.breach_since is None or ts < st.breach_since:
                    st.breach_since = ts
                st.peak = rule.worse(st.peak, value)
                if (ts - st.breach_since).total_seconds() >= rule.duration_s:
                    st.active = True
                    out.append(Transition("fire", "threshold", device, ts, rule, value, st.peak))
            else:
                st.breach_since = None
                st.peak = None
        return out

    def check_offline(self, devices: Iterable[Device], now: datetime) -> list[Transition]:
        out: list[Transition] = []
        for d in devices:
            last = self._last_seen.get(d.id)
            if last is None or d.id in self._offline:
                continue  # never reported yet, or already flagged
            if (now - last).total_seconds() > d.offline_after_s:
                self._offline.add(d.id)
                out.append(Transition("fire", "offline", d, now))
        return out
