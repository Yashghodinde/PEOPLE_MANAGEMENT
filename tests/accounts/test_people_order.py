"""People directory is ordered by designation rank, then name, then employee ID (one central table)."""

import re

import pytest
from django.test import Client
from ots.accounts.designations import DESIGNATION_PRIORITY, priority_of
from ots.accounts.models import Department, Role

from tests.accounts.factories import company, make_user

pytestmark = pytest.mark.django_db
PW = "correct horse battery"
LADDER = [
    "Director",
    "Manager",
    "Operations Head",
    "Senior Accountant",
    "Accounts Executive",
    "Employee",
    "Dispatcher",
    "Clerk",
]


def names(resp):
    return re.findall(r'href="/app/people/[0-9a-f-]+">([^<]+)</a>', resp.content.decode())


@pytest.fixture
def boss_client():
    boss = make_user(Role.SUPER_ADMIN, login_id="boss", full_name="Zed Boss", designation="Director")
    c = Client()
    assert c.post("/login", {"login_id": "boss", "password": PW}).status_code == 302
    return boss, c


def test_the_ladder_is_exactly_the_one_the_owner_gave():
    ranks = [DESIGNATION_PRIORITY[d] for d in LADDER]
    assert ranks == sorted(ranks, reverse=True) and len(set(ranks)) == len(ranks)


def test_every_designation_lists_in_ladder_order_and_clerk_is_last(boss_client):
    boss, c = boss_client
    for i, d in enumerate(reversed(LADDER[1:])):  # created in the wrong order on purpose
        make_user(Role.EMPLOYEE, reports_to=boss, full_name=f"P{i} {d}", designation=d)
    got = names(c.get("/app/people"))
    assert got[0] == "Zed Boss"
    assert [n.split(" ", 1)[1] for n in got[1:]] == LADDER[1:]


def test_same_designation_is_alphabetical_then_by_employee_id(boss_client):
    boss, c = boss_client
    make_user(Role.EMPLOYEE, reports_to=boss, full_name="bravo", designation="Clerk", employee_id="E9")
    make_user(Role.EMPLOYEE, reports_to=boss, full_name="Alpha", designation="Clerk", employee_id="E5")
    make_user(Role.EMPLOYEE, reports_to=boss, full_name="Alpha", designation="Clerk", employee_id="E2")
    assert names(c.get("/app/people"))[1:] == ["Alpha", "Alpha", "bravo"]
    ids = re.findall(r"E[259]\b", c.get("/app/people").content.decode())
    assert ids[:3] == ["E2", "E5", "E9"]


def test_blank_and_unknown_designations_come_after_all_configured_ones_and_stay_visible(boss_client):
    boss, c = boss_client
    make_user(Role.EMPLOYEE, reports_to=boss, full_name="A Blank", designation="")
    make_user(Role.EMPLOYEE, reports_to=boss, full_name="B Unknown", designation="Chief Dreamer")
    make_user(
        Role.EMPLOYEE, reports_to=boss, full_name="C Clerk", designation="  clerk "
    )  # case and spaces ignored
    got = names(c.get("/app/people"))
    assert got[:2] == ["Zed Boss", "C Clerk"] and set(got[2:]) == {"A Blank", "B Unknown"}
    assert priority_of("CLERK") == 30 and priority_of("") == 0 and priority_of("Chief Dreamer") == 0


def test_filters_keep_the_same_order(boss_client):
    boss, c = boss_client
    dept = Department.objects.create(name="Operations", company=company())
    for n, d in (("Low", "Clerk"), ("High", "Manager"), ("Mid", "Dispatcher")):
        make_user(Role.EMPLOYEE, reports_to=boss, full_name=n, designation=d, department=dept)
    assert names(c.get(f"/app/people?department={dept.pk}")) == ["High", "Mid", "Low"]


def test_pagination_keeps_one_continuous_order(boss_client):
    boss, c = boss_client
    for i in range(60):
        make_user(Role.EMPLOYEE, reports_to=boss, full_name=f"N{i:02d}", designation=LADDER[1 + i % 7])
    one, two = names(c.get("/app/people")), names(c.get("/app/people?page=2"))
    assert len(one) == 50 and len(two) == 11 and not set(one) & set(two)
    ladder_rank = {d: i for i, d in enumerate(LADDER)}
    allnames = one + two
    ranks = [ladder_rank[LADDER[1 + int(n[1:]) % 7]] if n.startswith("N") else 0 for n in allnames]
    assert ranks == sorted(ranks)
