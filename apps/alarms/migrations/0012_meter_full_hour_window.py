from django.db import migrations

DROP = {
    "meter_no_increment": ["window_minutes", "poa_min_wm2"],
    "meter_inverter_mismatch": ["window_minutes"],
}


def drop_rolling_window_params(apps, schema_editor):
    """T45: las reglas 9/10 comparan la última hora COMPLETA (quoia alineado
    al bucket horario cerrado de /generation/). La ventana rodante comparaba
    60 min de medidor contra el bucket PARCIAL de la hora en curso —
    'mismatch 520%' fabricado en plantas sanas (caso real El Olimpo: 280 vs
    274 kWh/h). window_minutes ya no aplica; el POA gate de la 9 tampoco (el
    propio bucket de generación es la prueba de producción)."""
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    for code, keys in DROP.items():
        rule = AlarmRule.objects.filter(code=code).first()
        if rule:
            for key in keys:
                rule.default_params.pop(key, None)
            rule.save(update_fields=["default_params"])


def restore_rolling_window_params(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    restore = {
        "meter_no_increment": {"window_minutes": 60, "poa_min_wm2": 100},
        "meter_inverter_mismatch": {"window_minutes": 60},
    }
    for code, extra in restore.items():
        rule = AlarmRule.objects.filter(code=code).first()
        if rule:
            rule.default_params = {**rule.default_params, **extra}
            rule.save(update_fields=["default_params"])


class Migration(migrations.Migration):
    dependencies = [("alarms", "0011_dawn_energy_floor")]

    operations = [
        migrations.RunPython(drop_rolling_window_params, restore_rolling_window_params)
    ]
