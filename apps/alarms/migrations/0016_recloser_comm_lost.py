from django.db import migrations

RULE = {
    "code": "recloser_comm_lost",
    "name": "Reconectador sin señal",
    "description": (
        "El reconectador no actualiza su medición durante al menos 5 minutos. "
        "Se vigila las 24 horas y no aplica a proyectos sin reconectador asociado."
    ),
    "category": "grid",
    "component_type": "relay",
    "default_severity": "high",
    "rule_group": "fast",
    "default_params": {"stale_minutes": 5},
    "enabled": True,
    "auto_resolve": True,
}


def add_rule(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    values = dict(RULE)
    code = values.pop("code")
    AlarmRule.objects.update_or_create(code=code, defaults=values)


def remove_rule(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    AlarmRule.objects.filter(code=RULE["code"]).delete()


class Migration(migrations.Migration):
    dependencies = [("alarms", "0015_disable_sla_noise")]
    operations = [migrations.RunPython(add_rule, remove_rule)]
