"""Reusable migration operations."""

import re
from collections.abc import Sequence

from django.db import migrations

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION ots_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE allowed text[] := COALESCE(TG_ARGV, ARRAY[]::text[]);
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF (to_jsonb(NEW) - allowed) IS DISTINCT FROM (to_jsonb(OLD) - allowed) THEN
      RAISE EXCEPTION 'table % is append-only: only % may change', TG_TABLE_NAME, allowed
        USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'table % is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'restrict_violation';
END $$;
"""


class AppendOnly(migrations.operations.base.Operation):
    """Make a table append-only.

    A trigger rejects DELETE and UPDATE of non-allowed columns, and the app role loses
    UPDATE/DELETE/TRUNCATE. Allowed columns change only through SECURITY DEFINER functions
    (the app role has no UPDATE privilege at all). TRUNCATE has no trigger so test DB flushes work.
    """

    reduces_to_sql = True
    reversible = True

    def __init__(self, table: str, allow_update_columns: Sequence[str] = ()) -> None:
        for name in (table, *allow_update_columns):
            if not _IDENT.match(name):
                raise ValueError(f"unsafe identifier: {name}")
        self.table = table
        self.allow_update_columns = tuple(allow_update_columns)

    def state_forwards(self, app_label, state):  # type: ignore[no-untyped-def]
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):  # type: ignore[no-untyped-def]
        t = self.table
        args = ", ".join(f"'{c}'" for c in self.allow_update_columns)
        schema_editor.execute(APPEND_ONLY_FN, params=None)
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {t}_append_only ON {t}")
        schema_editor.execute(
            f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION ots_append_only({args})"
        )
        schema_editor.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {t} FROM ots_app")

    def database_backwards(self, app_label, schema_editor, from_state, to_state):  # type: ignore[no-untyped-def]
        t = self.table
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {t}_append_only ON {t}")
        # restore what database_forwards took away, so the reverse really undoes it.
        # (the shared ots_append_only function stays: other tables use it.)
        schema_editor.execute(f"GRANT UPDATE, DELETE, TRUNCATE ON {t} TO ots_app")

    def describe(self) -> str:
        return f"Make {self.table} append-only"
