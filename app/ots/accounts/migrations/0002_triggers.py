"""DB-level hierarchy rules (M02.1): no cycles, valid manager, last Super Admin, no user deletes."""

from django.db import migrations

RANK_FN = """
CREATE OR REPLACE FUNCTION ots_role_rank(r text) RETURNS int LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE r WHEN 'SUPER_ADMIN' THEN 3 WHEN 'SUB_ADMIN' THEN 2 WHEN 'EMPLOYEE' THEN 1 ELSE 0 END
$$;
"""

NO_CYCLE_FN = """
CREATE OR REPLACE FUNCTION users_no_cycle_fn() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.reports_to_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.reports_to_id = NEW.id THEN
        RAISE EXCEPTION 'users_no_cycle: a user cannot report to themselves' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        WITH RECURSIVE chain(id, reports_to_id, depth) AS (
            SELECT u.id, u.reports_to_id, 1 FROM users u WHERE u.id = NEW.reports_to_id
            UNION ALL
            SELECT u.id, u.reports_to_id, c.depth + 1
            FROM users u JOIN chain c ON u.id = c.reports_to_id
            WHERE c.depth < 100
        )
        SELECT 1 FROM chain WHERE id = NEW.id OR reports_to_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'users_no_cycle: reporting loop detected' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
"""

# Manager must be active and of higher rank; a manager with active reports cannot be
# deactivated or demoted to a rank that no longer outranks them.
MANAGER_VALID_FN = """
CREATE OR REPLACE FUNCTION users_manager_valid_fn() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    m RECORD;
BEGIN
    IF NEW.reports_to_id IS NOT NULL
       AND (TG_OP = 'INSERT'
            OR NEW.reports_to_id IS DISTINCT FROM OLD.reports_to_id
            OR NEW.role <> OLD.role) THEN
        SELECT role, is_active INTO m FROM users WHERE id = NEW.reports_to_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'users_manager_valid: manager does not exist' USING ERRCODE = '23514';
        END IF;
        IF NOT m.is_active THEN
            RAISE EXCEPTION 'users_manager_valid: manager is not active' USING ERRCODE = '23514';
        END IF;
        IF ots_role_rank(m.role) <= ots_role_rank(NEW.role) THEN
            RAISE EXCEPTION 'users_manager_valid: manager must have a higher rank' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE'
       AND ((OLD.is_active AND NOT NEW.is_active) OR NEW.role <> OLD.role) THEN
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

LAST_SA_FN = """
CREATE OR REPLACE FUNCTION users_last_super_admin_fn() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    others int;
BEGIN
    IF NOT (OLD.role = 'SUPER_ADMIN' AND OLD.is_active AND NOT OLD.is_break_glass) THEN
        RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.role = 'SUPER_ADMIN' AND NEW.is_active AND NOT NEW.is_break_glass THEN
        RETURN NEW;
    END IF;
    -- serialise concurrent demotions so two admins cannot remove each other
    PERFORM pg_advisory_xact_lock(hashtext('ots_last_super_admin'));
    SELECT count(*) INTO others FROM users
    WHERE id <> OLD.id AND role = 'SUPER_ADMIN' AND is_active AND NOT is_break_glass;
    IF others = 0 THEN
        RAISE EXCEPTION 'users_last_super_admin: the last active Super Admin cannot be removed'
            USING ERRCODE = '23514';
    END IF;
    RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
END $$;
"""

NO_DELETE_FN = """
CREATE OR REPLACE FUNCTION users_no_delete_fn() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'users_no_delete: users are deactivated, never deleted' USING ERRCODE = '23514';
END $$;
"""

FORWARD = [
    RANK_FN,
    NO_CYCLE_FN,
    MANAGER_VALID_FN,
    LAST_SA_FN,
    NO_DELETE_FN,
    "DROP TRIGGER IF EXISTS users_no_cycle ON users",
    "CREATE TRIGGER users_no_cycle BEFORE INSERT OR UPDATE OF reports_to_id ON users "
    "FOR EACH ROW EXECUTE FUNCTION users_no_cycle_fn()",
    "DROP TRIGGER IF EXISTS users_manager_valid ON users",
    "CREATE TRIGGER users_manager_valid BEFORE INSERT OR UPDATE ON users "
    "FOR EACH ROW EXECUTE FUNCTION users_manager_valid_fn()",
    "DROP TRIGGER IF EXISTS users_last_super_admin ON users",
    "CREATE TRIGGER users_last_super_admin BEFORE UPDATE OR DELETE ON users "
    "FOR EACH ROW EXECUTE FUNCTION users_last_super_admin_fn()",
    "DROP TRIGGER IF EXISTS users_no_delete ON users",
    "CREATE TRIGGER users_no_delete BEFORE DELETE ON users "
    "FOR EACH ROW EXECUTE FUNCTION users_no_delete_fn()",
]

BACKWARD = [
    "DROP TRIGGER IF EXISTS users_no_delete ON users",
    "DROP TRIGGER IF EXISTS users_last_super_admin ON users",
    "DROP TRIGGER IF EXISTS users_manager_valid ON users",
    "DROP TRIGGER IF EXISTS users_no_cycle ON users",
    "DROP FUNCTION IF EXISTS users_no_delete_fn()",
    "DROP FUNCTION IF EXISTS users_last_super_admin_fn()",
    "DROP FUNCTION IF EXISTS users_manager_valid_fn()",
    "DROP FUNCTION IF EXISTS users_no_cycle_fn()",
    "DROP FUNCTION IF EXISTS ots_role_rank(text)",
]


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]
    operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=BACKWARD)]
