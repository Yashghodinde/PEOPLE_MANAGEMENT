"""M02 review fixes: the manager trigger takes a share lock and re-checks on reactivation;
reporting_history cannot hold an empty or backwards interval."""

from django.db import migrations, models

# Same rules as 0002, plus:
#  * FOR SHARE on the manager row, so a manager cannot be deactivated or demoted by another session between
#    this check and our commit (their trigger then waits, and sees us as an active report);
#  * the manager is re-checked when a user is reactivated (inactive -> active), not only on manager/role change.
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
    IF ots_role_rank(m.role) <= ots_role_rank(NEW.role) THEN
      RAISE EXCEPTION 'users_manager_valid: manager must be of higher rank' USING ERRCODE = '23514';
    END IF;
  END IF;
  IF TG_OP = 'UPDATE' AND ((OLD.is_active AND NOT NEW.is_active) OR NEW.role <> OLD.role) THEN
    IF EXISTS (
      SELECT 1 FROM users r
      WHERE r.reports_to_id = NEW.id AND r.is_active
        AND (NOT NEW.is_active OR ots_role_rank(r.role) >= ots_role_rank(NEW.role))
    ) THEN
      RAISE EXCEPTION 'users_manager_valid: user still has active reports' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;
"""

# the previous definition (0002) is restored on reverse
PREVIOUS_FN = MANAGER_VALID_FN.replace(
    "\n       OR (NOT OLD.is_active AND NEW.is_active)) THEN", ") THEN"
).replace(" FOR SHARE;", ";")

# empty [D, D) rows from same-day changes made before this fix: they carry no information
CLEAN_HISTORY = (
    "DELETE FROM reporting_history WHERE effective_to IS NOT NULL AND effective_to <= effective_from"
)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_default_company")]

    operations = [
        migrations.RunSQL(MANAGER_VALID_FN, reverse_sql=PREVIOUS_FN),
        migrations.RunSQL(CLEAN_HISTORY, reverse_sql=migrations.RunSQL.noop),
        migrations.AddConstraint(
            model_name="reportinghistory",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("effective_to__isnull", True),
                    ("effective_to__gt", models.F("effective_from")),
                    _connector="OR",
                ),
                name="reporting_history_interval_valid",
            ),
        ),
    ]
