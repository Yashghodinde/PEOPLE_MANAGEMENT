import django.db.models.functions.datetime
from django.db import migrations, models

import ots.core.db_ops

FUNCTIONS = """
CREATE OR REPLACE FUNCTION ots_mark_published(p_id bigint) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  UPDATE outbox_events SET published_at = now() WHERE id = p_id AND published_at IS NULL
$$;
CREATE OR REPLACE FUNCTION ots_mark_dead_letter(p_id bigint, p_reason text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  UPDATE outbox_events
     SET published_at = now(),
         payload = payload || jsonb_build_object('dead_letter', true, 'dead_letter_reason', p_reason)
   WHERE id = p_id AND published_at IS NULL
$$;
REVOKE ALL ON FUNCTION ots_mark_published(bigint), ots_mark_dead_letter(bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ots_mark_published(bigint), ots_mark_dead_letter(bigint, text) TO ots_app;
"""


class Migration(migrations.Migration):
    dependencies = [("ots_core", "0002_idempotency")]
    operations = [
        migrations.CreateModel(
            name="OutboxEvent",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                ("type", models.CharField(max_length=100)),
                ("aggregate_type", models.CharField(max_length=60)),
                ("aggregate_id", models.CharField(max_length=64)),
                ("payload", models.JSONField(default=dict)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"db_table": "outbox_events"},
        ),
        migrations.AddIndex(
            model_name="outboxevent",
            index=models.Index(
                condition=models.Q(("published_at__isnull", True)),
                fields=["id"],
                name="outbox_unpublished_idx",
            ),
        ),
        ots.core.db_ops.AppendOnly("outbox_events", allow_update_columns=("published_at", "payload")),
        migrations.RunSQL(FUNCTIONS, migrations.RunSQL.noop),
    ]
