"""Task-code sequences for 2026 to 2060 and the full-text / partial-match search columns.

* One sequence per year, created here because the application's database role cannot run DDL in production.
  (`ots.tasks.codes` creates a missing year on demand for the owner role and development.)
* `search_vector`: a generated tsvector over title, description and code, with a GIN index.
* A trigram index on `title` (pg_trgm) for partial matches such as typing the middle of a word.
"""

from django.db import migrations

YEARS = range(2026, 2061)
CREATE_SEQUENCES = "\n".join(f"CREATE SEQUENCE IF NOT EXISTS task_code_seq_{y};" for y in YEARS)
DROP_SEQUENCES = "\n".join(f"DROP SEQUENCE IF EXISTS task_code_seq_{y};" for y in YEARS)

SEARCH_FORWARD = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
ALTER TABLE tasks ADD COLUMN search_vector tsvector GENERATED ALWAYS AS (
  to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(description, '') || ' ' || coalesce(task_code, ''))
) STORED;
CREATE INDEX tasks_search_idx ON tasks USING GIN (search_vector);
CREATE INDEX tasks_title_trgm_idx ON tasks USING GIN (title gin_trgm_ops);
"""
SEARCH_BACKWARD = """
DROP INDEX IF EXISTS tasks_title_trgm_idx;
DROP INDEX IF EXISTS tasks_search_idx;
ALTER TABLE tasks DROP COLUMN IF EXISTS search_vector;
"""


class Migration(migrations.Migration):
    dependencies = [("tasks", "0001_initial")]

    operations = [
        migrations.RunSQL(CREATE_SEQUENCES, reverse_sql=DROP_SEQUENCES),
        migrations.RunSQL(SEARCH_FORWARD, reverse_sql=SEARCH_BACKWARD),
    ]
