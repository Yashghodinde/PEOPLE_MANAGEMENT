"""IMP-08: the HR role. It ranks 2 (like a Sub-Admin, so only a Super Admin can be its manager) and can never be
anyone's manager. The role check constraint learns the new value."""

from django.db import migrations, models

RANK_FN = """
CREATE OR REPLACE FUNCTION ots_role_rank(r text) RETURNS int LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE r WHEN 'SUPER_ADMIN' THEN 3 WHEN 'SUB_ADMIN' THEN 2 WHEN 'HR' THEN 2 WHEN 'EMPLOYEE' THEN 1 ELSE 0 END
$$;
"""
RANK_FN_BEFORE = """
CREATE OR REPLACE FUNCTION ots_role_rank(r text) RETURNS int LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE r WHEN 'SUPER_ADMIN' THEN 3 WHEN 'SUB_ADMIN' THEN 2 WHEN 'EMPLOYEE' THEN 1 ELSE 0 END
$$;
"""

# 0004's function, plus: an HR account cannot be anyone's manager.
MANAGER_VALID_FN = """
CREATE OR REPLACE FUNCTION users_manager_valid_fn() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE m RECORD;
BEGIN
  IF NEW.reports_to_id IS NOT NULL AND (
       TG_OP = 'INSERT'
       OR NEW.reports_to_id IS DISTINCT FROM OLD.reports_to_id
       OR NEW.role <> OLD.role
       OR (NOT OLD.is_active AND NEW.is_active)) THEN
    SELECT role, is_active INTO m FROM users WHERE id = NEW.reports_to_id FOR SHARE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'users_manager_valid: manager does not exist' USING ERRCODE = '23514';
    END IF;
    IF NOT m.is_active THEN
      RAISE EXCEPTION 'users_manager_valid: manager is not active' USING ERRCODE = '23514';
    END IF;
    IF m.role = 'HR' THEN
      RAISE EXCEPTION 'users_manager_valid: an HR account cannot manage people' USING ERRCODE = '23514';
    END IF;
    IF ots_role_rank(m.role) <= ots_role_rank(NEW.role) THEN
      RAISE EXCEPTION 'users_manager_valid: manager must be of higher rank' USING ERRCODE = '23514';
    END IF;
  END IF;
  IF TG_OP = 'UPDATE' AND ((OLD.is_active AND NOT NEW.is_active) OR NEW.role <> OLD.role) THEN
    IF EXISTS (
      SELECT 1 FROM users r
      WHERE r.reports_to_id = NEW.id AND r.is_active
        AND (NOT NEW.is_active OR ots_role_rank(r.role) >= ots_role_rank(NEW.role) OR NEW.role = 'HR')
    ) THEN
      RAISE EXCEPTION 'users_manager_valid: user still has active reports' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_optional_shift")]

    operations = [
        migrations.RunSQL(RANK_FN, reverse_sql=RANK_FN_BEFORE),
        migrations.RunSQL(MANAGER_VALID_FN, reverse_sql=migrations.RunSQL.noop),
        migrations.RemoveConstraint(model_name="user", name="users_role_valid"),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(role__in=["SUPER_ADMIN", "SUB_ADMIN", "HR", "EMPLOYEE"]),
                name="users_role_valid",
            ),
        ),
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[
                    ("SUPER_ADMIN", "Super Admin"),
                    ("SUB_ADMIN", "Sub-Admin"),
                    ("HR", "HR"),
                    ("EMPLOYEE", "Employee"),
                ],
                default="EMPLOYEE",
                max_length=12,
            ),
        ),
    ]
