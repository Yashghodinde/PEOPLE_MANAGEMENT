"""Create the primary Super Admin and the sealed break-glass account (server shell, once)."""

from __future__ import annotations

import secrets
import string
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ots.accounts import services
from ots.accounts.models import Role, User
from ots.audit import api as audit

ALPHABET = string.ascii_letters + string.digits


def random_password(n: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


class Command(BaseCommand):
    help = "Create the primary Super Admin and the break-glass account. Prints both passwords once."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--login", required=True, help="login ID of the primary Super Admin")
        parser.add_argument("--name", required=True, help="full name of the primary Super Admin")
        parser.add_argument("--employee-id", default="EMP001")

    def handle(self, *args: Any, **o: Any) -> None:
        if User.objects.filter(role=Role.SUPER_ADMIN).exists():
            raise CommandError("A Super Admin already exists; bootstrap_admins runs only on an empty system.")
        main_pw, glass_pw = random_password(16), random_password(24)
        with transaction.atomic():
            main = services.create_user(
                None, login_id=o["login"], employee_id=o["employee_id"], full_name=o["name"],
                role=Role.SUPER_ADMIN, reports_to=None, password=main_pw, must_change_password=True,
            )  # fmt: skip
            glass = services.create_user(
                None, login_id="breakglass", employee_id="BREAKGLASS", full_name="Break-glass account",
                role=Role.SUPER_ADMIN, reports_to=None, password=glass_pw, must_change_password=False,
                is_break_glass=True,
            )  # fmt: skip
            audit.record("breakglass.created", None, glass, {"by": "bootstrap_admins"})
        self.stdout.write(
            f"Primary Super Admin  login: {main.login_id}   password: {main_pw}  (change at first login)"
        )
        self.stdout.write(f"Break-glass          login: {glass.login_id}   password: {glass_pw}")
        self.stdout.write("Print the break-glass password, seal it in the envelope, and clear this terminal.")
