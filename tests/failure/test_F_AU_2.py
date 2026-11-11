"""F-AU-2: the application role cannot UPDATE or DELETE audit rows; the trigger blocks it for anyone."""

import pytest
from django.db import DatabaseError, connection, transaction
from ots.audit import api as audit

pytestmark = pytest.mark.django_db


def test_update_and_delete_rejected():
    row = audit.record("test.tamper")
    with connection.cursor() as cur:
        cur.execute("SET ROLE ots_app")
        try:
            for sql in ("UPDATE audit_log SET action='x' WHERE id=%s", "DELETE FROM audit_log WHERE id=%s"):
                with pytest.raises(DatabaseError), transaction.atomic():
                    cur.execute(sql, [row.id])
            cur.execute("SELECT has_table_privilege('ots_app','audit_log','UPDATE')")
            assert cur.fetchone()[0] is False
        finally:
            cur.execute("RESET ROLE")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute("UPDATE audit_log SET action='x' WHERE id=%s", [row.id])  # trigger blocks even the owner
