"""F-ST-8: the Graph certificate is about to expire. WARNING `storage.cert_expiring` when 30 days remain, CRITICAL
when 7 remain (and when it has expired); renewing the certificate closes the alert. Runbook OPS-5."""

import datetime

import pytest
from freezegun import freeze_time
from ots.core import alerts
from ots.storage import capacity
from ots.storage import classify as c

pytestmark = pytest.mark.django_db
START = datetime.datetime(2030, 1, 1, 6, 0, tzinfo=datetime.UTC)


class Cert:
    def __init__(self, not_after):
        self.not_after = not_after

    def certificate_not_after(self):
        return self.not_after


def alert():
    return alerts.open_alerts().filter(key=c.ALERT_CERT).first()


def test_a_certificate_counting_down_to_its_expiry_warns_then_goes_critical_then_renewal_clears_it():
    expires = START + datetime.timedelta(days=60)
    provider = Cert(expires)
    seen = {}
    with freeze_time(START) as clock:
        for day in range(0, 61, 1):
            capacity.check_certificate(provider=provider)
            a = alert()
            seen[60 - day] = a.severity if a else None  # days remaining -> severity that day
            clock.tick(datetime.timedelta(days=1))
    assert seen[60] is None and seen[31] is None  # more than 30 days: nothing
    assert seen[30] == "WARNING" and seen[29] == "WARNING" and seen[8] == "WARNING"
    assert seen[7] == "CRITICAL" and seen[1] == "CRITICAL"

    with freeze_time(START + datetime.timedelta(days=65)):  # it expired five days ago and nobody noticed
        capacity.check_certificate(provider=provider)
        a = alert()
        assert a.severity == "CRITICAL" and "EXPIRED" in a.title
        provider.not_after = START + datetime.timedelta(days=65 + 730)  # the new certificate (runbook OPS-5)
        capacity.check_certificate(provider=provider)
        assert alert() is None
