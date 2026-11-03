from datetime import UTC, datetime, timedelta

import pytest
from app.parsing import InvalidMessage, parse_payload, parse_topic

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def test_topic_ok():
    assert parse_topic("kaar/baeckerei-demo/freezer-01/telemetry") == ("baeckerei-demo", "freezer-01")


@pytest.mark.parametrize(
    "topic",
    [
        "kaar/baeckerei-demo/freezer-01",
        "other/a1/b/telemetry",
        "kaar/UPPER/x/telemetry",
        "kaar/ok/../telemetry",
        "kaar/ok/x/y/telemetry",
    ],
)
def test_topic_rejected(topic):
    with pytest.raises(InvalidMessage):
        parse_topic(topic)


def test_payload_iso_and_bool():
    ts, m = parse_payload(b'{"ts":"2026-11-03T11:59:00Z","metrics":{"temperature":-18.2,"leak":true}}', NOW)
    assert ts == NOW - timedelta(minutes=1)
    assert m == {"temperature": -18.2, "leak": 1.0}


def test_payload_epoch_ms_and_default_ts():
    ms = int((NOW - timedelta(seconds=30)).timestamp() * 1000)
    ts, _ = parse_payload(f'{{"ts":{ms},"metrics":{{"t":1}}}}'.encode(), NOW)
    assert ts == NOW - timedelta(seconds=30)
    ts, _ = parse_payload(b'{"metrics":{"t":1}}', NOW)
    assert ts == NOW


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"metrics":{}}',
        b'{"metrics":{"t":"hot"}}',
        b'{"metrics":{"Bad-Name":1}}',
        b'{"metrics":{"t":1},"ts":"2026-11-03T12:00:00"}',
        b'{"metrics":{"t":1},"ts":"2030-01-01T00:00:00Z"}',
        b'{"metrics":{"t":NaN}}',
    ],
)
def test_payload_rejected(raw):
    with pytest.raises(InvalidMessage):
        parse_payload(raw, NOW)
