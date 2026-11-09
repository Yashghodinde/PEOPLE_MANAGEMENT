import json
import logging

import pytest
from django.contrib.auth.hashers import make_password
from django.db import ProgrammingError, connection
from django.db.utils import InternalError
from ots.core.logging import request_id_var, user_id_var


def test_hasher_parameters():
    h = make_password("correct horse battery")
    assert h.startswith("argon2$argon2id$")
    assert "m=19456,t=2,p=1" in h


def test_db_settings(settings):
    db = settings.DATABASES["default"]
    assert db["CONN_MAX_AGE"] == 60 and db["CONN_HEALTH_CHECKS"] is True
    assert settings.USE_TZ and settings.TIME_ZONE == "Asia/Kolkata"


def test_json_log_fields():
    from ots.core.logging import ContextFilter, OtsJsonFormatter

    request_id_var.set("req-1")
    user_id_var.set("u-1")
    rec = logging.LogRecord("ots.test", logging.INFO, __file__, 1, "hello", None, None)
    rec.event = "x.y"
    ContextFilter().filter(rec)
    data = json.loads(OtsJsonFormatter().format(rec))
    assert {"ts", "level", "logger", "request_id", "user_id", "event", "msg"} <= set(data)
    assert data["request_id"] == "req-1" and data["user_id"] == "u-1"
    assert data["event"] == "x.y" and data["msg"] == "hello" and data["level"] == "INFO"


def test_logging_configured_for_stdout(settings):
    assert settings.LOGGING["handlers"]["stdout"]["stream"] == "ext://sys.stdout"


@pytest.mark.django_db
def test_roles_exist():
    with connection.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('ots_app','ots_backup')")
        assert {r[0] for r in cur.fetchall()} == {"ots_app", "ots_backup"}


@pytest.mark.django_db
def test_ots_app_cannot_create_table():
    with connection.cursor() as cur, pytest.raises((ProgrammingError, InternalError)):
        cur.execute("SET ROLE ots_app")
        cur.execute("CREATE TABLE should_fail (id int)")


@pytest.mark.django_db
def test_ots_app_can_use_normal_tables():
    with connection.cursor() as cur:
        cur.execute("SET ROLE ots_app")
        cur.execute("SELECT count(*) FROM django_migrations")
        cur.execute("RESET ROLE")
