"""F-AU-1: if the audit write fails, the business change is rolled back."""

import pytest
from django.db import DatabaseError, transaction
from ots.audit import api as audit
from ots.core.models import Alert

pytestmark = pytest.mark.django_db


def test_audit_failure_rolls_back_business_row():
    def service():
        with transaction.atomic():
            Alert.objects.create(
                key="biz",
                severity="INFO",
                title="t",
                first_seen_at="2026-11-11T00:00Z",
                last_seen_at="2026-11-11T00:00Z",
            )
            audit.record(
                "x.y", payload={"bad": "x"}, target=("t", "1" * 100)
            )  # target_id too long -> DB error

    with pytest.raises(DatabaseError):
        service()
    assert not Alert.objects.filter(key="biz").exists()
