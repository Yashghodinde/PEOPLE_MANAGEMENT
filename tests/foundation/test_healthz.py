import pytest
from django.test import Client


@pytest.mark.django_db
def test_healthz_ok(settings, tmp_path, monkeypatch):
    import shutil
    from collections import namedtuple

    U = namedtuple("U", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(100, 50, 50))
    settings.OTS_DATA_DIR = tmp_path
    r = Client().get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"db": "ok", "disk": "ok"}


@pytest.mark.django_db
def test_healthz_503_when_disk_low(settings, tmp_path, monkeypatch):
    import shutil
    from collections import namedtuple

    U = namedtuple("U", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(100, 95, 5))
    settings.OTS_DATA_DIR = tmp_path
    r = Client().get("/healthz")
    assert r.status_code == 503 and r.json()["disk"] == "critical"
