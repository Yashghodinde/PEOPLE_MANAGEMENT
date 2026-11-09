import shutil
from collections import namedtuple
from datetime import timedelta

import pytest
from django.test import Client
from ots.core.clock import now
from ots.core.models import Job

U = namedtuple("U", "total used free")


@pytest.fixture
def disk_ok(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(100, 50, 50))
    settings.OTS_DATA_DIR = tmp_path


def heartbeat(seconds_ago):
    Job.objects.update_or_create(
        name="jobs.watchdog",
        defaults={"schedule": "* * * * *", "last_finished_at": now() - timedelta(seconds=seconds_ago)},
    )


@pytest.mark.django_db
def test_healthz_ok_on_fresh_install_without_worker_history(disk_ok):
    r = Client().get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"db": "ok", "disk": "ok", "worker_heartbeat_age_s": None}


@pytest.mark.django_db
def test_healthz_reports_fresh_worker_heartbeat(disk_ok):
    heartbeat(30)
    body = Client().get("/healthz").json()
    assert 29 <= body["worker_heartbeat_age_s"] <= 40


@pytest.mark.django_db
def test_healthz_503_when_worker_stale(disk_ok):
    heartbeat(900)
    r = Client().get("/healthz")
    assert r.status_code == 503 and r.json()["worker_heartbeat_age_s"] >= 900


@pytest.mark.django_db
def test_healthz_503_when_disk_low(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(100, 95, 5))
    settings.OTS_DATA_DIR = tmp_path
    r = Client().get("/healthz")
    assert r.status_code == 503 and r.json()["disk"] == "critical"


@pytest.mark.django_db
def test_healthz_503_when_db_down(disk_ok, monkeypatch):
    from ots.core import health

    monkeypatch.setattr(health, "check_db", lambda: "error")
    r = Client().get("/healthz")
    assert r.status_code == 503 and r.json()["db"] == "error"
