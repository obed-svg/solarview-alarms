from django.db import migrations


def seed_channel(apps, schema_editor):
    NotificationChannel = apps.get_model("notifications", "NotificationChannel")
    NotificationChannel.objects.get_or_create(
        name="ops-discord",
        defaults={
            "kind": "discord",
            "env_key": "webhook_discord",
            "min_severity": "medium",
            # nace deshabilitado: encenderlo en admin cuando el webhook esté listo
            "enabled": False,
        },
    )


def unseed_channel(apps, schema_editor):
    NotificationChannel = apps.get_model("notifications", "NotificationChannel")
    NotificationChannel.objects.filter(name="ops-discord").delete()


class Migration(migrations.Migration):
    dependencies = [("notifications", "0001_initial")]

    operations = [migrations.RunPython(seed_channel, unseed_channel)]
