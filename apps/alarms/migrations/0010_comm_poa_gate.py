from django.db import migrations

POA_PARAMS = {"poa_min_wm2": 100, "persistence_minutes": 15}


def add_poa_gate_params(apps, schema_editor):
    """T40 (ola matinal 2026-07-09 06:23-06:43: 73 inverter_comm_lost + 88
    availability_inputs_missing falsas): los SUN2000 arrancan por IRRADIANCIA,
    no por reloj — a amanecer+45 muchos siguen legítimamente apagados. Las
    reglas 4 y 12 ahora exigen POA sostenida antes de exigir comunicación
    (mismo gate físico que la regla 2)."""
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    rule = AlarmRule.objects.filter(code="inverter_comm_lost").first()
    if rule:
        rule.default_params = {**rule.default_params, **POA_PARAMS}
        rule.save(update_fields=["default_params"])


def remove_poa_gate_params(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    rule = AlarmRule.objects.filter(code="inverter_comm_lost").first()
    if rule:
        for key in POA_PARAMS:
            rule.default_params.pop(key, None)
        rule.save(update_fields=["default_params"])


class Migration(migrations.Migration):
    dependencies = [("alarms", "0009_dawn_window_margins")]

    operations = [migrations.RunPython(add_poa_gate_params, remove_poa_gate_params)]
