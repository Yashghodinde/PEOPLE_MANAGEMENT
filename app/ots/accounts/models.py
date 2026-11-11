"""Identity models (contract section 6). Business rules are enforced in services AND in the database."""

from __future__ import annotations

import datetime

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models.functions import Lower

from ots.core.ids import uuid7


class Role(models.TextChoices):
    SUPER_ADMIN = "SUPER_ADMIN", "Super Admin"
    SUB_ADMIN = "SUB_ADMIN", "Sub-Admin"
    EMPLOYEE = "EMPLOYEE", "Employee"


ROLE_RANK = {Role.SUPER_ADMIN: 3, Role.SUB_ADMIN: 2, Role.EMPLOYEE: 1}


class Qualification(models.TextChoices):
    TENTH = "TENTH", "10th"
    TWELFTH = "TWELFTH", "12th"
    DIPLOMA = "DIPLOMA", "Diploma"
    GRADUATE = "GRADUATE", "Graduate"
    POSTGRADUATE = "POSTGRADUATE", "Postgraduate"
    DOCTORATE = "DOCTORATE", "Doctorate"
    PROFESSIONAL = "PROFESSIONAL", "Professional"


class Company(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=20, unique=True)

    class Meta:
        db_table = "companies"

    def __str__(self) -> str:
        return self.name


class Department(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="departments")
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "departments"
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="departments_company_name_uniq")
        ]

    def __str__(self) -> str:
        return self.name


def _default_weekly_off() -> list[int]:
    return [6]  # Sunday; 0=Mon … 6=Sun


class UserQuerySet(models.QuerySet["User"]):
    def visible_to(self, viewer: User | None) -> UserQuerySet:
        """Rows `viewer` may see: Super Admin all; Sub-Admin own subtree + self; Employee self only."""
        from . import selectors

        if viewer is None or not viewer.is_active:
            return self.none()
        if viewer.role == Role.SUPER_ADMIN:
            return self.all()
        if viewer.role == Role.SUB_ADMIN:
            return self.filter(models.Q(pk=viewer.pk) | models.Q(pk__in=selectors.subtree_ids(viewer.pk)))
        return self.filter(pk=viewer.pk)


class UserManager(BaseUserManager["User"]):
    use_in_migrations = False

    def get_queryset(self) -> UserQuerySet:
        return UserQuerySet(self.model, using=self._db)

    def visible_to(self, viewer: User | None) -> UserQuerySet:
        return self.get_queryset().visible_to(viewer)


class User(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="users")
    employee_id = models.CharField(max_length=20, unique=True)
    login_id = models.CharField(max_length=60, unique=True)
    full_name = models.CharField(max_length=200)
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.EMPLOYEE)
    reports_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="direct_reports"
    )

    designation = models.CharField(max_length=120, blank=True, default="")
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.PROTECT, related_name="members"
    )
    date_of_joining = models.DateField(null=True, blank=True)

    highest_qualification = models.CharField(
        max_length=14, choices=Qualification.choices, blank=True, default=""
    )
    qualification_detail = models.CharField(max_length=200, blank=True, default="")
    institution = models.CharField(max_length=200, blank=True, default="")
    qualification_year = models.PositiveSmallIntegerField(null=True, blank=True)

    shift_start = models.TimeField(null=True, blank=True, default=datetime.time(9, 30))
    shift_end = models.TimeField(null=True, blank=True, default=datetime.time(18, 30))
    weekly_off = ArrayField(models.SmallIntegerField(), default=_default_weekly_off, blank=True)

    mobile = models.CharField(max_length=20, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    reference_photo_path = models.CharField(max_length=300, blank=True, default="")

    is_active = models.BooleanField(default=True)
    is_break_glass = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)
    failed_logins = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    version = models.PositiveIntegerField(default=1)

    last_login = None  # replaced by last_login_at

    objects = UserManager()

    USERNAME_FIELD = "login_id"
    REQUIRED_FIELDS = ["full_name", "employee_id"]

    class Meta:
        db_table = "users"
        constraints = [
            models.UniqueConstraint(Lower("login_id"), name="users_login_id_ci_uniq"),
            models.CheckConstraint(
                condition=models.Q(role="SUPER_ADMIN") | models.Q(reports_to__isnull=False),
                name="users_reports_to_required",
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=["SUPER_ADMIN", "SUB_ADMIN", "EMPLOYEE"]),
                name="users_role_valid",
            ),
        ]
        indexes = [models.Index(fields=["reports_to"], name="users_reports_to_idx")]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.employee_id})"

    def get_full_name(self) -> str:
        return self.full_name

    def get_short_name(self) -> str:
        return self.full_name.split(" ")[0] if self.full_name else self.login_id

    @property
    def rank(self) -> int:
        return ROLE_RANK[Role(self.role)]


class ReportingHistory(models.Model):
    """Who reported to whom, and when. effective_to is NULL for the current row."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="reporting_history")
    manager = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="reporting_history_as_manager"
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    changed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reporting_history"
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(effective_to__isnull=True),
                name="reporting_history_one_current",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="reporting_history_interval_valid",
            ),
        ]
        indexes = [models.Index(fields=["manager", "effective_from"], name="rephist_manager_idx")]

    def __str__(self) -> str:
        return f"{self.user_id} -> {self.manager_id} from {self.effective_from}"
