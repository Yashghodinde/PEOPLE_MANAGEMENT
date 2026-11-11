"""A person may have no shift set (blank shift start/end)."""

import datetime

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_review_hardening")]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="shift_start",
            field=models.TimeField(blank=True, default=datetime.time(9, 30), null=True),
        ),
        migrations.AlterField(
            model_name="user",
            name="shift_end",
            field=models.TimeField(blank=True, default=datetime.time(18, 30), null=True),
        ),
    ]
