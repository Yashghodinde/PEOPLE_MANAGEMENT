"""The only source of 'now'. Server time is authoritative; never trust client timestamps."""

import datetime as dt
from zoneinfo import ZoneInfo

from django.utils import timezone

IST = ZoneInfo("Asia/Kolkata")


def now() -> dt.datetime:
    return timezone.now()


def today_local() -> dt.date:
    return now().astimezone(IST).date()
