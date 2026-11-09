"""Helper process for F-CORE-2: starts a job, announces it, then sleeps until it is SIGKILLed."""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"

import django  # noqa: E402

django.setup()

from ots.core.jobs import Scheduler, job  # noqa: E402

marker = Path(sys.argv[1])


@job("victim.job", schedule="* * * * *")
def victim() -> None:
    marker.write_text("running")
    time.sleep(120)


s = Scheduler()
s.sync_registry()
from ots.core.models import Job  # noqa: E402

Job.objects.filter(name="victim.job").update(next_run_at="2000-01-01T00:00:00Z")
s.tick()
time.sleep(120)
