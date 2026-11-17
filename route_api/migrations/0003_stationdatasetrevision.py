from django.db import migrations, models


def seed_revision(apps, schema_editor):
    apps.get_model("route_api", "StationDatasetRevision").objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("route_api", "0002_routeplan")]
    operations = [
        migrations.CreateModel(
            name="StationDatasetRevision",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("generation", models.PositiveBigIntegerField(default=1)),
            ],
        ),
        migrations.RunPython(seed_revision, migrations.RunPython.noop),
    ]
