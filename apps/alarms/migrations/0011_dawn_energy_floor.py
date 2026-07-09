from django.db import migrations

PARAMS = {
    "inverter_comm_lost": {"wake_grace_minutes": 45},
    "meter_no_increment": {"min_window_energy_kwh": 10},
    "meter_inverter_mismatch": {"min_window_energy_kwh": 10},
}


def add_params(apps, schema_editor):
    """T41 (mañana real 2026-07-09): (a) los dataloggers reportan el primer
    dato ~25 min después de que la POA cruza el umbral → gracia de arranque
    de 45 min en el gate de las reglas 4/12; (b) al alba la energía de la
    ventana es <1 kWh y la regla 10 daba mismatch de 75-100% con diferencias
    de centésimas → piso de energía mínima en 9/10."""
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    for code, extra in PARAMS.items():
        rule = AlarmRule.objects.filter(code=code).first()
        if rule:
            rule.default_params = {**rule.default_params, **extra}
            rule.save(update_fields=["default_params"])


def remove_params(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    for code, extra in PARAMS.items():
        rule = AlarmRule.objects.filter(code=code).first()
        if rule:
            for key in extra:
                rule.default_params.pop(key, None)
            rule.save(update_fields=["default_params"])


class Migration(migrations.Migration):
    dependencies = [("alarms", "0010_comm_poa_gate")]

    operations = [migrations.RunPython(add_params, remove_params)]
