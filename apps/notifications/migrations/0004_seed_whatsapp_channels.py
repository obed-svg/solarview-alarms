from django.db import migrations


def seed_whatsapp_channels(apps, schema_editor):
    NotificationChannel = apps.get_model("notifications", "NotificationChannel")
    NotificationChannel.objects.filter(kind="discord").update(enabled=False)
    common = {
        "kind": "whatsapp",
        "env_key": "WHATSAPP_ACCESS_TOKEN",
        "min_severity": "medium",
        "enabled": False,
    }
    NotificationChannel.objects.get_or_create(
        name="ops-whatsapp",
        defaults={**common, "purpose": "realtime"},
    )
    NotificationChannel.objects.get_or_create(
        name="resumen-whatsapp",
        defaults={**common, "purpose": "general_summary"},
    )


def unseed_whatsapp_channels(apps, schema_editor):
    NotificationChannel = apps.get_model("notifications", "NotificationChannel")
    NotificationChannel.objects.filter(
        name__in=["ops-whatsapp", "resumen-whatsapp"]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("notifications", "0003_whatsappinboundmessage_and_more")]
    operations = [migrations.RunPython(seed_whatsapp_channels, unseed_whatsapp_channels)]
