"""F-ID-1: login IDs differing only in case collide; employee_id unique."""

import pytest
from django.db import IntegrityError, transaction
from ots.accounts.models import Role

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db


def test_login_id_case_insensitive_unique():
    make_user(Role.SUPER_ADMIN, login_id="Priya")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_user(Role.SUPER_ADMIN, login_id="priya")


def test_employee_id_unique():
    make_user(Role.SUPER_ADMIN, employee_id="EMP-X")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_user(Role.SUPER_ADMIN, employee_id="EMP-X")
