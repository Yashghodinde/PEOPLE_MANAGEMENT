"""Server-shell-only recovery: set a temporary password for a Super Admin who is locked out."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ots.accounts.management.commands.bootstrap_admins import random_password
from ots.accounts.models import Role, User
from ots.audit import api as audit
from ots.core import alerts, events


class Command(BaseCommand):
    help = "Reset the password of a Super Admin (server shell only). Raises a CRITICAL alert."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--login", required=True)

    def handle(self, *args: Any, **o: Any) -> None:
        with transaction.atomic():
            user = User.objects.select_for_update().filter(login_id__iexact=o["login"]).first()
            if user is None or user.role != Role.SUPER_ADMIN:
                raise CommandError("No Super Admin with that login ID.")
            temp = random_password(16)
            user.set_password(temp)
            user.must_change_password = True
            user.failed_logins = 0
            user.locked_until = None
            user.is_active = True
            user.version += 1
            user.save()
            audit.record("breakglass.reset_master", None, user, {"login_id": user.login_id})
            events.publish("user.updated", user, {"fields": ["password"]})
        alerts.raise_alert(
            "auth.reset_master", "CRITICAL", "reset_master was run on the server", f"login {user.login_id}"
        )
        self.stdout.write(f"Temporary password for {user.login_id}: {temp}  (must be changed at next login)")
