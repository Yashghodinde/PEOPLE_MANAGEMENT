"""IMP-02: every version has its Indian upload date (0007 filled the existing rows), so the column is required."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("files", "0007_personal_storage")]

    operations = [
        migrations.AlterField(model_name="fileversion", name="uploaded_on", field=models.DateField()),
    ]
