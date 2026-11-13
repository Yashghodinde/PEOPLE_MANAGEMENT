"""Substring search (M08.5): trigram indexes so `ILIKE '%term%'` stays fast at 100k versions."""

from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations

SQL = [
    ("fv_filename_trgm_idx", "file_versions", "original_filename"),
    ("fv_code_trgm_idx", "file_versions", "file_code"),
    ("users_name_trgm_idx", "users", "full_name"),
]


class Migration(migrations.Migration):
    dependencies = [("files", "0003_filefetchrequest_attempts_and_more"), ("accounts", "0001_initial")]

    operations = [
        TrigramExtension(),
        *[
            migrations.RunSQL(
                f"CREATE INDEX {name} ON {table} USING GIN ({col} gin_trgm_ops)",
                f"DROP INDEX IF EXISTS {name}",
            )
            for name, table, col in SQL
        ],
    ]
