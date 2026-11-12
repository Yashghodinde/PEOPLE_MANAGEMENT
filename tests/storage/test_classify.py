"""M05.3: classify() covers every row of the plan's error table (section 3), as one pure function."""

import datetime

import pytest
from ots.storage import classify as c
from ots.storage.classify import Kind, classify
from ots.storage.errors import (
    ConfigurationError,
    GraphError,
    TransportFailure,
    VerificationFailed,
    WrongLibrary,
)

H = datetime.timedelta


def err(status, code="", retry_after=None, url_kind="", body=""):
    return GraphError(status, code=code, body=body, retry_after=retry_after, url_kind=url_kind)


# (error, expected kind, expected alert key, expected severity)
TABLE = [
    (err(401), Kind.BLOCK_AUTH, c.ALERT_AUTH, "CRITICAL"),
    (err(403), Kind.BLOCK_AUTH, c.ALERT_AUTH, "CRITICAL"),
    (err(404, url_kind="upload"), Kind.SESSION_EXPIRED, None, None),
    (err(404), Kind.FAILED, c.ALERT_DEAD_LETTER, "CRITICAL"),
    (err(409, "nameAlreadyExists"), Kind.CONFLICT, None, None),
    (err(410, "resyncRequired"), Kind.RESYNC, None, None),
    (err(416), Kind.RESUME, None, None),
    (err(429, retry_after=120), Kind.PAUSE_QUEUE, c.ALERT_THROTTLED, "INFO"),
    (err(429), Kind.PAUSE_QUEUE, c.ALERT_THROTTLED, "INFO"),  # no header: a default pause
    (err(503, retry_after=30), Kind.PAUSE_QUEUE, c.ALERT_THROTTLED, "INFO"),
    (err(503), Kind.RETRY, None, None),  # 503 without Retry-After is an ordinary outage
    (err(500), Kind.RETRY, None, None),
    (err(502), Kind.RETRY, None, None),
    (err(504), Kind.RETRY, None, None),
    (TransportFailure("timed out"), Kind.RETRY, None, None),
    (TransportFailure("connection refused"), Kind.RETRY, None, None),
    (err(507), Kind.BLOCK_QUOTA, c.ALERT_QUOTA, "CRITICAL"),
    (err(400), Kind.FAILED, c.ALERT_DEAD_LETTER, "CRITICAL"),
    (err(405), Kind.FAILED, c.ALERT_DEAD_LETTER, "CRITICAL"),
    (err(413), Kind.FAILED, c.ALERT_DEAD_LETTER, "CRITICAL"),
    (err(422), Kind.FAILED, c.ALERT_DEAD_LETTER, "CRITICAL"),
    (ConfigurationError("no certificate"), Kind.BLOCK_AUTH, c.ALERT_AUTH, "CRITICAL"),
    (WrongLibrary("no business delete"), Kind.FAILED, c.ALERT_DEAD_LETTER, "CRITICAL"),
    (RuntimeError("something odd"), Kind.RETRY, None, None),
]


@pytest.mark.parametrize(
    "error,kind,key,severity", TABLE, ids=lambda x: getattr(x, "status", None) or str(x)[:30]
)
def test_every_row_of_the_error_table(error, kind, key, severity):
    a = classify(error)
    assert (a.kind, a.alert_key, a.severity) == (kind, key, severity)


def test_throttle_uses_the_servers_retry_after_and_a_default_otherwise():
    assert classify(err(429, retry_after=120)).delay == 120
    assert classify(err(503, retry_after=7)).delay == 7
    assert classify(err(429)).delay == c.DEFAULT_THROTTLE_SECONDS


def test_backoff_is_30_seconds_doubling_capped_at_an_hour():
    delays = [classify(err(500), attempts=n).delay for n in range(9)]
    assert delays == [30, 60, 120, 240, 480, 960, 1920, 3600, 3600]


def test_jitter_scales_the_backoff_within_twenty_percent():
    assert classify(err(500), attempts=2, jitter=0.8).delay == pytest.approx(96)
    assert classify(err(500), attempts=2, jitter=1.2).delay == pytest.approx(144)


def test_after_24_hours_of_trying_a_server_error_becomes_a_dead_letter():
    assert classify(err(502), attempts=40, age=H(hours=23, minutes=59)).kind == Kind.RETRY
    a = classify(err(502), attempts=40, age=H(hours=24))
    assert (a.kind, a.alert_key, a.severity) == (Kind.FAILED, c.ALERT_DEAD_LETTER, "CRITICAL")
    assert classify(TransportFailure("down"), attempts=99, age=H(days=2)).kind == Kind.FAILED


def test_the_error_body_in_a_dead_letter_message_is_cut_to_2kb():
    a = classify(err(400, body="x" * 9000))
    assert len(a.message) < 2100 and "graph 400" in a.message


def test_verification_failures_get_a_few_tries_then_a_dead_letter():
    e = VerificationFailed("hash differs")
    assert classify(e, attempts=0).kind == Kind.RETRY and classify(e, attempts=1).kind == Kind.RETRY
    a = classify(e, attempts=2)
    assert (a.kind, a.alert_key) == (Kind.FAILED, c.ALERT_VERIFICATION)


def test_classify_is_pure_the_same_input_gives_the_same_action():
    e = err(500)
    assert classify(e, attempts=3) == classify(e, attempts=3)
