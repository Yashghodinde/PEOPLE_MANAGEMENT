"""Database roles and default privileges (contract section 9). Run with migrator credentials.

Append-only revocations on specific tables are added by M00.6 (AppendOnly operation).
"""

import os

from django.db import migrations

APP_PW = os.environ.get("OTS_APP_DB_PASSWORD")
BACKUP_PW = os.environ.get("OTS_BACKUP_DB_PASSWORD")


def _role_sql(name: str, password: str | None, extra: str = "") -> str:
    login = "LOGIN PASSWORD %s" if password else "NOLOGIN"
    return f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{name}') THEN
    CREATE ROLE {name} {login} {extra};
  END IF;
END $$;
"""


def forwards(apps, schema_editor):  # type: ignore[no-untyped-def]
    cur = schema_editor.connection.cursor()
    for name, pw, extra in (("ots_app", APP_PW, ""), ("ots_backup", BACKUP_PW, "REPLICATION")):
        sql = _role_sql(name, None, extra)
        cur.execute(sql)
        if pw:
            cur.execute(f"ALTER ROLE {name} LOGIN PASSWORD %s", [pw])
    cur.execute("SELECT current_database()")
    db = cur.fetchone()[0]
    cur.execute(f'GRANT CONNECT ON DATABASE "{db}" TO ots_app, ots_backup')
    cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC, ots_app, ots_backup")
    cur.execute("GRANT USAGE ON SCHEMA public TO ots_app, ots_backup")
    cur.execute("GRANT pg_read_all_data TO ots_backup")
    # Existing tables, then default privileges for every future table the migrator creates.
    cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ots_app")
    cur.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ots_app")
    cur.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ots_app"
    )
    cur.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO ots_app")


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
