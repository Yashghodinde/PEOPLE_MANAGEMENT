"""IMP-13.0 (D-095 D3): a Sub-Admin may report to another Sub-Admin. Equal rank is allowed for that pair only,
and because rank no longer rules out a loop, the trigger now refuses one itself."""

from django.db import migrations

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
    IF ots_role_rank(m.role) <= ots_role_rank(NEW.role)
       AND NOT (m.role = 'SUB_ADMIN' AND NEW.role = 'SUB_ADMIN') THEN
      RAISE EXCEPTION 'users_manager_valid: manager must be of higher rank' USING ERRCODE = '23514';
    END IF;
    IF NEW.role = 'SUB_ADMIN' AND m.role = 'SUB_ADMIN' THEN  -- equal rank is the only way a loop can form
      IF NEW.reports_to_id = NEW.id OR EXISTS (
        WITH RECURSIVE up(id, depth) AS (
          SELECT u.reports_to_id, 1 FROM users u WHERE u.id = NEW.reports_to_id
          UNION ALL
          SELECT u.reports_to_id, up.depth + 1 FROM users u JOIN up ON u.id = up.id
          WHERE up.id IS NOT NULL AND up.depth < 100
        ) SELECT 1 FROM up WHERE up.id = NEW.id
      ) THEN
        RAISE EXCEPTION 'users_manager_valid: reporting loop' USING ERRCODE = '23514';
      END IF;
    END IF;
  END IF;
  IF TG_OP = 'UPDATE' AND ((OLD.is_active AND NOT NEW.is_active) OR NEW.role <> OLD.role) THEN
    IF EXISTS (
      SELECT 1 FROM users r
      WHERE r.reports_to_id = NEW.id AND r.is_active
        AND (NOT NEW.is_active OR (ots_role_rank(r.role) >= ots_role_rank(NEW.role) AND NOT (r.role = 'SUB_ADMIN' AND NEW.role = 'SUB_ADMIN')) OR NEW.role = 'HR')
    ) THEN
      RAISE EXCEPTION 'users_manager_valid: user still has active reports' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_hr_role")]
    operations = [migrations.RunSQL(MANAGER_VALID_FN, reverse_sql=migrations.RunSQL.noop)]
