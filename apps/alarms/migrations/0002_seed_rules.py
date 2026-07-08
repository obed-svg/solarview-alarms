from django.db import migrations

from apps.alarms.catalog import seed_rules, unseed_rules


class Migration(migrations.Migration):
    dependencies = [
        ("alarms", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_rules, unseed_rules),
    ]
