"""M06.6: a whole working day writes at most 10 times to the attendance tables."""

import datetime
import re
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from freezegun import freeze_time
from ots.attendance import sessions
from ots.attendance.models import WorkSession

from tests.attendance.conftest import jpeg

pytestmark = pytest.mark.django_db
PW = "correct horse battery"
TABLES = ("work_sessions", "attendance_days", "attendance_corrections", "holidays")
WRITE = re.compile(rf'^\s*(INSERT INTO|UPDATE)\s+"({"|".join(TABLES)})"', re.I)
T0 = datetime.datetime(2026, 11, 12, 4, 0, tzinfo=datetime.UTC)


def minutes(n):
    return datetime.timedelta(minutes=n)


def test_a_full_day_stays_within_ten_writes(worker):
    writes = []

    def counter(execute, sql, params, many, context):
        if WRITE.match(sql):
            writes.append(sql[:40])
        return execute(sql, params, many, context)

    def selfie_login(c):
        assert c.post("/login", {"login_id": "asha", "password": PW}).status_code == 302
        data = {"selfie": SimpleUploadedFile("s.jpg", jpeg(), "image/jpeg"), "device_id": "d1"}
        assert c.post("/login/selfie", data, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())).status_code == 200

    with connection.execute_wrapper(counter):
        c = Client()
        with freeze_time(T0):
            selfie_login(c)  # 1 insert
        key = c.session.session_key
        with freeze_time(T0 + minutes(60)):
            sessions.report_state(key, "idle")  # 1 update
        with freeze_time(T0 + minutes(70)):
            sessions.report_state(key, "active")  # 1 update
        with freeze_time(T0 + minutes(120)):
            sessions.report_state(key, "idle")  # 1 update
        with freeze_time(T0 + minutes(170)):
            sessions.sweep()  # 1 update: closed as IDLE_45
        c2 = Client()
        with freeze_time(T0 + minutes(180)):
            selfie_login(c2)  # 1 insert
        with freeze_time(T0 + minutes(480)):
            sessions.end_current(c2.session.session_key, "END_OF_DAY")  # 1 update
    assert WorkSession.objects.count() == 2
    assert len(writes) <= 10, writes
