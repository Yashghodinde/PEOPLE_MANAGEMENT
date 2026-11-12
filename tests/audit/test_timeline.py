"""M03.2: the activity timeline merges providers, in order, one query each, with stable cursors."""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from freezegun import freeze_time
from ots.accounts.models import Role
from ots.audit import api as audit
from ots.audit import timeline
from ots.audit.timeline import Cursor, Event
from ots.core.errors import ValidationFailed

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
UTC = datetime.UTC


@pytest.fixture
def person():
    sa = make_user(Role.SUPER_ADMIN)
    return make_user(Role.EMPLOYEE, reports_to=make_user(Role.SUB_ADMIN, reports_to=sa))


def _login_row(user, at, action="auth.login_ok"):
    from django.db import transaction

    with freeze_time(at), transaction.atomic():
        audit.record(action, user, user, {})


@pytest.fixture
def clean_providers(monkeypatch):
    """Tests add fake providers without leaking them into other tests."""
    monkeypatch.setattr(timeline, "_PROVIDERS", dict(timeline._PROVIDERS))
    return timeline._PROVIDERS


def _fake(name, stamps, counter=None):
    """A provider that makes one query and returns events at the given instants."""

    def provider(user_id, start, end, cursor, limit):
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        events = [Event(at=t, source=name, key=i, title=f"{name} {i}") for i, t in enumerate(stamps)]
        events = [e for e in events if (start is None or e.at >= start) and (end is None or e.at < end)]
        if cursor is not None:
            events = [
                e
                for e in events
                if e.sort_key() < (cursor.at, cursor.source, int(cursor.key))
                or (e.source != cursor.source and (e.at, e.source) < (cursor.at, cursor.source))
            ]
        return sorted(events, key=Event.sort_key, reverse=True)[:limit]

    return provider


def test_audit_provider_shows_only_that_persons_auth_rows(person):
    other = make_user(Role.EMPLOYEE, reports_to=person.reports_to)
    _login_row(person, "2026-11-12 04:00:00")
    _login_row(person, "2026-11-12 05:00:00", "auth.login_failed")
    _login_row(other, "2026-11-12 04:30:00")
    from django.db import transaction

    with transaction.atomic():
        audit.record("user.updated", person, person, {})  # not an auth.* row
    page = timeline.activity_timeline(person.pk)
    assert [e.title for e in page.events] == ["Wrong password", "Signed in"]
    assert page.next_cursor is None


def test_events_from_every_provider_are_merged_newest_first(person, clean_providers):
    base = datetime.datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
    _login_row(person, base + datetime.timedelta(minutes=10))
    clean_providers["task"] = _fake("task", [base + datetime.timedelta(minutes=m) for m in (5, 20)])
    clean_providers["session"] = _fake("session", [base + datetime.timedelta(minutes=m) for m in (0, 15)])
    page = timeline.activity_timeline(person.pk)
    assert [e.title for e in page.events] == ["task 1", "session 1", "Signed in", "task 0", "session 0"]


def test_one_query_per_provider(person, clean_providers):
    t = datetime.datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
    clean_providers["task"] = _fake("task", [t])
    clean_providers["session"] = _fake("session", [t])
    with CaptureQueriesContext(connection) as ctx:
        timeline.activity_timeline(person.pk)
    assert len(ctx) == len(timeline.providers()) == 3  # audit + task + session


def test_date_range_is_inclusive_in_local_time(person):
    # 2026-11-12 20:00 UTC is 01:30 on the 13th in Kolkata
    _login_row(person, "2026-11-12 20:00:00")
    _login_row(person, "2026-11-12 04:00:00")  # 09:30 on the 12th
    on_12 = timeline.activity_timeline(person.pk, datetime.date(2026, 11, 12), datetime.date(2026, 11, 12))
    on_13 = timeline.activity_timeline(person.pk, datetime.date(2026, 11, 13), datetime.date(2026, 11, 13))
    assert len(on_12.events) == 1 and len(on_13.events) == 1
    assert on_13.events[0].at.astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).day == 13


def test_cursor_paging_never_skips_or_repeats_even_on_the_same_instant(person, clean_providers):
    same = datetime.datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
    for _ in range(3):
        _login_row(person, same)  # three audit rows at the exact same instant
    clean_providers["task"] = _fake("task", [same, same, same - datetime.timedelta(minutes=1)])
    seen: list[tuple[str, object]] = []
    cursor = None
    pages = 0
    while True:
        page = timeline.activity_timeline(person.pk, cursor=cursor, limit=2)
        seen += [(e.source, e.key) for e in page.events]
        pages += 1
        if page.next_cursor is None:
            break
        cursor = Cursor.decode(page.next_cursor.encode())  # round-trips through text, as in a URL
        assert pages < 10
    assert len(seen) == len(set(seen)) == 6  # 3 audit rows + 3 task events: every one once
    assert {src for src, _ in seen} == {"audit", "task"}


def test_malformed_cursor_is_a_validation_error():
    for bad in ("garbage", "2026-11-12T06:00:00|audit|1", "2026-11-12T06:00:00+00:00||", "x|y|z"):
        with pytest.raises(ValidationFailed):
            Cursor.decode(bad)
    assert Cursor.decode("") is None and Cursor.decode(None) is None


def test_a_provider_cannot_be_registered_twice(clean_providers):
    with pytest.raises(RuntimeError, match="already registered"):
        timeline.register_provider("audit", timeline.audit_provider)
