from datetime import UTC, datetime, timedelta

from app.rules import Device, Engine, Rule

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
FREEZER = Device(id=1, tenant_id=1, site_id=10, external_id="freezer-01", name="Freezer", offline_after_s=900)
OTHER_TENANT = Device(id=2, tenant_id=2, site_id=20, external_id="x", name="X")
SAME_SITE = Device(id=3, tenant_id=1, site_id=10, external_id="fridge", name="Fridge")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def warm_rule(**kw) -> Rule:
    base = dict(
        id=1,
        tenant_id=1,
        name="too warm",
        metric="temperature",
        operator=">",
        threshold=-15,
        hysteresis=1,
        duration_s=600,
        device_id=1,
    )
    base.update(kw)
    return Rule(**base)


def feed(engine, device, values, metric="temperature", step=1):
    out = []
    for i, v in enumerate(values):
        out += [(i * step, t) for t in engine.observe(device, metric, v, at(i * step))]
    return out


def test_fires_only_after_duration():
    e = Engine()
    e.set_rules([warm_rule()])
    events = feed(e, FREEZER, [-14] * 12)  # 1 reading/min, breached from minute 0
    fires = [(m, t) for m, t in events if t.action == "fire"]
    assert len(fires) == 1 and fires[0][0] == 10  # 600 s after first breach


def test_short_breach_resets():
    e = Engine()
    e.set_rules([warm_rule()])
    events = feed(e, FREEZER, [-14] * 5 + [-18] + [-14] * 5)
    assert events == []


def test_hysteresis_prevents_flapping_and_peak_tracked():
    e = Engine()
    e.set_rules([warm_rule(duration_s=0)])
    events = feed(e, FREEZER, [-14, -9, -15.5, -14.5, -16.5])
    actions = [t.action for _, t in events]
    assert actions == ["fire", "resolve"]  # -15.5 is within hysteresis, -16.5 clears
    assert events[1][1].peak == -9


def test_less_than_operator():
    e = Engine()
    e.set_rules(
        [warm_rule(metric="battery", operator="<", threshold=15, hysteresis=5, duration_s=0, device_id=None)]
    )
    events = feed(e, FREEZER, [30, 14, 17, 21], metric="battery")
    assert [t.action for _, t in events] == ["fire", "resolve"]


def test_scoping_by_tenant_and_site():
    rule = warm_rule(device_id=None, site_id=10, duration_s=0)
    e = Engine()
    e.set_rules([rule])
    assert feed(e, OTHER_TENANT, [0]) == []
    assert len(feed(e, SAME_SITE, [0])) == 1


def test_restore_open_alert_does_not_refire():
    e = Engine()
    e.set_rules([warm_rule(duration_s=0)])
    e.restore_open_threshold(rule_id=1, device_id=1, peak=-10)
    assert feed(e, FREEZER, [-12]) == []  # still breached, already open
    events = feed(e, FREEZER, [-20])
    assert events[0][1].action == "resolve" and events[0][1].peak == -10


def test_offline_and_back_online():
    e = Engine()
    e.set_rules([])
    e.observe(FREEZER, "temperature", -18, at(0))
    assert e.check_offline([FREEZER], at(10)) == []
    off = e.check_offline([FREEZER], at(16))
    assert len(off) == 1 and off[0].kind == "offline" and off[0].action == "fire"
    assert e.check_offline([FREEZER], at(30)) == []  # no duplicate
    back = e.observe(FREEZER, "temperature", -18, at(31))
    assert [(t.kind, t.action) for t in back] == [("offline", "resolve")]


def test_backfilled_data_does_not_trigger_offline():
    e = Engine()
    e.set_rules([])
    # gateway uploads 2 h old buffered readings right now
    e.observe(FREEZER, "temperature", -18, at(-120), received=at(0))
    assert e.check_offline([FREEZER], at(5)) == []


def test_deleted_rule_state_is_dropped():
    e = Engine()
    e.set_rules([warm_rule(duration_s=0)])
    feed(e, FREEZER, [-10])
    e.set_rules([])
    assert feed(e, FREEZER, [-10]) == []
