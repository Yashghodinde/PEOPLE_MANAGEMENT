"""Background worker: job scheduler loop, heartbeat file, graceful SIGTERM."""

import logging
import signal
import threading
from pathlib import Path
from types import FrameType

from django.core.management.base import BaseCommand

from ots.attendance import jobs as attendance_jobs  # registers the attendance jobs
from ots.backup import jobs as backup_jobs  # noqa: F401  (registers the backup jobs)
from ots.core import builtin_jobs  # noqa: F401  (registers core jobs)
from ots.core.dispatcher import Listener
from ots.core.jobs import Scheduler
from ots.files import jobs as file_jobs  # noqa: F401 (registers file jobs)
from ots.live import jobs as live_jobs  # noqa: F401  (registers the live jobs)
from ots.notifications import jobs as notification_jobs  # noqa: F401  (registers the reminder job)
from ots.storage import jobs as storage_jobs  # registers the storage jobs

HEARTBEAT = Path("/tmp/worker-heartbeat")  # noqa: S108
LOOP_SECONDS = 5.0
SHUTDOWN_SECONDS = 30.0
log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the background worker (job scheduler)."

    def handle(self, *args: object, **options: object) -> None:
        stop = threading.Event()

        def _term(signum: int, frame: FrameType | None) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)

        sched = Scheduler()
        sched.sync_registry()
        aborted = sched.startup_sweep()
        storage_jobs.on_worker_start()
        attendance_jobs.on_worker_start()
        log.info("worker started", extra={"event": "worker.started", "aborted_runs": aborted})
        listener = Listener()  # wakes the loop immediately on outbox NOTIFY
        try:
            while not stop.is_set():
                HEARTBEAT.touch()
                sched.tick()
                listener.wait(timeout=LOOP_SECONDS)
        finally:
            listener.close()
            finished = sched.wait(SHUTDOWN_SECONDS)
            log.info("worker stopped", extra={"event": "worker.stopped", "clean": finished})
