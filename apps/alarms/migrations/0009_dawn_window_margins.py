from django.db import migrations

MARGINS = {
    "data_frozen": 60,
    "poa_invalid": 60,
    "tmod_invalid": 60,
    "recloser_open": 30,
}


def add_dawn_margins(apps, schema_editor):
    """T39 (amanecer real 2026-07-09 05:40): las reglas de ventana evaluaban
    con `now` en horario solar pero la ventana de 45 min aún contenía
    oscuridad → 13 poa_invalid falsas (frozen 0.0 = noche real, offset -1.0
    del piranómetro, generación difusa cruzando 5 kW con POA aún en 0),
    1 data_frozen (temperatura constante nocturna) y 3 recloser_open de
    plantas que abren el reconectador de noche por operación y cierran al
    arrancar generación."""
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    for code, margin in MARGINS.items():
        rule = AlarmRule.objects.filter(code=code).first()
        if rule:
            rule.default_params = {**rule.default_params, "solar_margin_minutes": margin}
            rule.save(update_fields=["default_params"])


def remove_dawn_margins(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    for code in MARGINS:
        rule = AlarmRule.objects.filter(code=code).first()
        if rule:
            rule.default_params.pop("solar_margin_minutes", None)
            rule.save(update_fields=["default_params"])


class Migration(migrations.Migration):
    dependencies = [("alarms", "0008_meter_night_gate")]

    operations = [migrations.RunPython(add_dawn_margins, remove_dawn_margins)]
