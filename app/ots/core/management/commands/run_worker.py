"""Placeholder worker loop: touches the heartbeat file. Replaced by the real scheduler in M00.7."""

import time
from pathlib import Path

from django.core.management.base import BaseCommand

HEARTBEAT = Path("/tmp/worker-heartbeat")  # noqa: S108


class Command(BaseCommand):
    help = "Run the background worker (stub until M00.7)."

    def handle(self, *args: object, **options: object) -> None:
        while True:
            HEARTBEAT.touch()
            time.sleep(10)
