import uuid

from django.db import migrations
from uuid6 import uuid7


def create_default(apps, schema_editor):  # type: ignore[no-untyped-def]
    Company = apps.get_model("accounts", "Company")
    if not Company.objects.exists():
        Company.objects.create(id=uuid.UUID(bytes=uuid7().bytes), name="Default Company", code="DEFAULT")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_triggers")]
    operations = [migrations.RunPython(create_default, migrations.RunPython.noop)]
