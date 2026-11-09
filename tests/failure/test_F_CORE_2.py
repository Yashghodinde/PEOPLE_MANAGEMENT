"""F-CORE-2: worker killed mid-job. On restart the stale run is closed ABORTED and the job re-runs once."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from django.conf import settings
from django.db import connection
from ots.core import jobs
from ots.core.models import Job, JobRun

pytestmark = pytest.mark.django_db(transaction=True)


def test_killed_worker_run_is_aborted_and_rerun_succeeds_once(tmp_path):
    db = settings.DATABASES["default"]
    marker = tmp_path / "started"
    env = {**os.environ, "DATABASE_URL": f"postgres://localhost/{db['NAME']}"}
    victim = Path(__file__).with_name("_worker_victim.py")
    proc = subprocess.Popen([sys.executable, "-I", str(victim), str(marker)], env=env)  # noqa: S603
    try:
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists(), "victim job never started"
        assert JobRun.objects.filter(job_name="victim.job", status="RUNNING").count() == 1
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    # --- restart: fresh scheduler, same registry name now with a quick job ---
    time.sleep(0.5)  # let PostgreSQL notice the dead client and drop its advisory lock
    ran = []
    jobs._JOBS.pop("victim.job", None)
    jobs.job("victim.job", schedule="* * * * *")(lambda: ran.append(1))
    s = jobs.Scheduler()
    s.sync_registry()
    assert s.startup_sweep() == 1
    old = JobRun.objects.get(job_name="victim.job")
    assert old.status == "ABORTED" and old.finished_at is not None

    Job.objects.filter(name="victim.job").update(next_run_at="2000-01-01T00:00:00Z")
    s.tick()
    s.wait(10)
    assert ran == [1]
    statuses = sorted(JobRun.objects.filter(job_name="victim.job").values_list("status", flat=True))
    assert statuses == ["ABORTED", "SUCCESS"]
    assert (
        Job.objects.get(name="victim.job").next_run_at > Job.objects.get(name="victim.job").last_finished_at
    )
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_runs WHERE status = 'RUNNING'")
        assert cur.fetchone()[0] == 0
    jobs._JOBS.pop("victim.job", None)
