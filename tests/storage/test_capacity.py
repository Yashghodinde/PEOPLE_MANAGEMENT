"""M05.7: capacity thresholds, the archive-only block at 95%, history rows, and certificate expiry alerts."""

import datetime

import pytest
from freezegun import freeze_time
from ots.core import alerts
from ots.core.jobs import registered_jobs
from ots.storage import capacity, queue
from ots.storage import classify as c
from ots.storage import jobs as storage_jobs  # noqa: F401  (registers the jobs)
from ots.storage.errors import GraphError
from ots.storage.gate import ARCHIVE_KIND, QueueGate
from ots.storage.models import StorageCapacity
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library

from tests.storage.queue_helpers import make_op

pytestmark = pytest.mark.django_db
K, S = Op.Kind, Op.State


@pytest.fixture
def gate():
    return QueueGate(rng=lambda: 1.0)


def fill(fake, percent, total=10_000):
    """Make the fake provider report `percent`% of `total` bytes used."""
    fake.wipe()
    fake.set_quota(total)
    fake.plant(Library.BUSINESS, "Files/ballast", b"x" * int(total * percent / 100))


def capacity_alert():
    return alerts.open_alerts().filter(key=c.ALERT_CAPACITY).first()


@pytest.mark.parametrize(
    "percent,severity",
    [(0, None), (69.9, None), (70, "WARNING"), (79.9, "WARNING"), (80, "WARNING"), (89.9, "WARNING"),
     (90, "CRITICAL"), (94.9, "CRITICAL"), (95, "CRITICAL"), (100, "CRITICAL")],
)  # fmt: skip
def test_thresholds(fake, gate, percent, severity):
    fill(fake, percent)
    reading = capacity.check_capacity(provider=fake, gate=gate)
    assert reading.severity == severity
    alert = capacity_alert()
    assert (alert.severity if alert else None) == severity
    assert (reading.archive_blocked) is (percent >= 95)


def test_the_alert_names_the_percentage_and_closes_when_space_returns(fake, gate):
    fill(fake, 83)
    capacity.check_capacity(provider=fake, gate=gate)
    assert "83%" in capacity_alert().title
    fill(fake, 40)
    capacity.check_capacity(provider=fake, gate=gate)
    assert capacity_alert() is None


def test_a_reading_is_kept_every_time_for_the_growth_chart(fake, gate):
    fill(fake, 10)
    capacity.check_capacity(provider=fake, gate=gate)
    fill(fake, 20)
    capacity.check_capacity(provider=fake, gate=gate)
    rows = list(StorageCapacity.objects.order_by("at", "id"))
    assert [(r.used_bytes, r.total_bytes) for r in rows] == [(1000, 10_000), (2000, 10_000)]


def test_at_96_percent_wal_ships_but_new_files_queue(fake, gate, tmp_path):
    fill(fake, 96)
    capacity.check_capacity(provider=fake, gate=gate)
    file_op = make_op(tmp_path, b"new file", name="file", kind=K.ARCHIVE_FILE)
    wal_op = make_op(tmp_path, b"wal", name="wal", kind=K.SHIP_WAL, library=Library.DR, remote="WAL/1")
    assert queue.process_one(provider=fake, gate=gate) == "done"  # the WAL goes first and goes through
    assert queue.process_one(provider=fake, gate=gate) == "idle"  # the file is held back, not lost
    file_op.refresh_from_db()
    wal_op.refresh_from_db()
    assert (wal_op.state, file_op.state) == (S.DONE, S.PENDING)
    assert gate.permission(ARCHIVE_KIND).permission.value == "WAIT"


def test_when_space_is_freed_the_queued_files_go_through(fake, gate, tmp_path):
    fill(fake, 96)
    capacity.check_capacity(provider=fake, gate=gate)
    op = make_op(tmp_path, b"new file", name="file")
    assert queue.process_one(provider=fake, gate=gate) == "idle"
    fill(fake, 60)
    reading = capacity.check_capacity(provider=fake, gate=gate)
    assert not reading.archive_blocked and gate.block() is None
    assert queue.process_one(provider=fake, gate=gate) == "done"
    op.refresh_from_db()
    assert op.state == S.DONE


def test_lifting_the_archive_block_never_lifts_an_authorisation_block(fake, gate):
    with freeze_time("2030-01-01 06:00:00"):
        gate.failure(GraphError(403))
        fill(fake, 10)
        assert capacity.check_capacity(provider=fake, gate=gate) is None  # blocked: no extra calls
        gate.clear_archive_block()
        assert gate.block().state == "BLOCKED_AUTH"


def test_an_unknown_quota_is_ignored_not_a_crash(fake, gate):
    fake.set_quota(0)
    assert capacity.check_capacity(provider=fake, gate=gate) is None
    assert not StorageCapacity.objects.exists()


def test_a_failing_usage_call_is_reported_to_the_gate_and_raised(fake, gate):
    fake.inject([502])
    with pytest.raises(GraphError):
        capacity.check_capacity(provider=fake, gate=gate)


# --- the certificate -----------------------------------------------------------------------------------------------


class WithCert:
    def __init__(self, not_after):
        self._not_after = not_after

    def certificate_not_after(self):
        return self._not_after


NOW = "2030-01-01 06:00:00"


@pytest.mark.parametrize(
    "days_left,severity",
    # days_left + 1 hour remain, so "30" means just over 30 days: not yet "within 30 days"
    [(400, None), (31, None), (30, None), (29, "WARNING"), (8, "WARNING"), (7, "WARNING"), (6, "CRITICAL"), (1, "CRITICAL")],
)  # fmt: skip
def test_certificate_alert_levels(days_left, severity):
    with freeze_time(NOW):
        now = datetime.datetime.now(datetime.UTC)
        got = capacity.check_certificate(provider=WithCert(now + datetime.timedelta(days=days_left, hours=1)))
    assert got == days_left
    alert = alerts.open_alerts().filter(key=c.ALERT_CERT).first()
    assert (alert.severity if alert else None) == severity


def test_an_expired_certificate_is_critical_and_says_so():
    with freeze_time(NOW):
        now = datetime.datetime.now(datetime.UTC)
        capacity.check_certificate(provider=WithCert(now - datetime.timedelta(days=2)))
    alert = alerts.open_alerts().get(key=c.ALERT_CERT)
    assert alert.severity == "CRITICAL" and "EXPIRED" in alert.title


def test_renewing_the_certificate_closes_the_alert():
    with freeze_time(NOW):
        now = datetime.datetime.now(datetime.UTC)
        capacity.check_certificate(provider=WithCert(now + datetime.timedelta(days=5)))
        assert alerts.open_alerts().filter(key=c.ALERT_CERT).exists()
        capacity.check_certificate(provider=WithCert(now + datetime.timedelta(days=730)))
    assert not alerts.open_alerts().filter(key=c.ALERT_CERT).exists()


def test_a_provider_without_a_certificate_is_skipped(fake):
    assert capacity.check_certificate(provider=fake) is None


def test_the_jobs_are_registered():
    jobs = registered_jobs()
    assert jobs["storage.capacity"].schedule == "0 * * * *"
    assert jobs["storage.cert_check"].schedule == "15 3 * * *"
