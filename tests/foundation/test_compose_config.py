from pathlib import Path

import yaml

COMPOSE = yaml.safe_load((Path(__file__).parents[2] / "deploy" / "compose.yaml").read_text())
SERVICES = COMPOSE["services"]


def test_four_services():
    assert set(SERVICES) == {"caddy", "app", "worker", "db"}


def test_memory_limits_match_contract():
    limits = {name: svc["mem_limit"] for name, svc in SERVICES.items()}
    assert limits == {"caddy": "64m", "app": "600m", "worker": "350m", "db": "1g"}


def test_restart_policy():
    assert all(s["restart"] == "unless-stopped" for s in SERVICES.values())


def test_only_caddy_publishes_ports():
    assert SERVICES["caddy"]["ports"] == ["80:80", "443:443"]
    assert all("ports" not in s for n, s in SERVICES.items() if n != "caddy")


def test_healthchecks():
    app, db, worker = (SERVICES[n]["healthcheck"] for n in ("app", "db", "worker"))
    assert (app["interval"], app["timeout"], app["retries"]) == ("30s", "5s", 3)
    assert db["interval"] == "10s" and "pg_isready" in db["test"][-1]
    assert worker["interval"] == "10s" and "worker-heartbeat" in worker["test"][-1]


def test_logging_rotation():
    for svc in SERVICES.values():
        assert svc["logging"]["options"] == {"max-size": "50m", "max-file": "10"}
