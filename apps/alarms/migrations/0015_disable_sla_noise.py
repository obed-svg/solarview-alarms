from django.db import migrations
from django.utils import timezone


def disable_sla_noise(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    Alarm = apps.get_model("alarms", "Alarm")
    rule = AlarmRule.objects.filter(code="alarm_sla_breach").first()
    if rule is None:
        return
    rule.enabled = False
    rule.description = (
        "Capacidad interna deshabilitada para evitar repetir alarmas sin reconocer. "
        "No se muestra en WhatsApp ni en el resumen general."
    )
    rule.save(update_fields=["enabled", "description"])
    Alarm.objects.filter(rule=rule).exclude(status="resolved").update(
        status="resolved",
        resolved_at=timezone.now(),
        resolution_type="auto",
    )


def enable_sla_rule(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    AlarmRule.objects.filter(code="alarm_sla_breach").update(enabled=True)


class Migration(migrations.Migration):
    dependencies = [("alarms", "0014_alarm_maintenance_started_at_and_more")]
    operations = [migrations.RunPython(disable_sla_noise, enable_sla_rule)]
