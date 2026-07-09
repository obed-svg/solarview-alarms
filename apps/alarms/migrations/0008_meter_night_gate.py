from django.db import migrations


def add_meter_solar_margin(apps, schema_editor):
    """T38: la regla 8 solo evalúa en horario solar. Observado 2026-07-09
    ~03:30: de noche el régimen de escritura de quoia cambia por sistema —
    medidores que paran a las 20:30 exactas y otros que pasan a cadencia
    horaria (age 61 vs umbral 60 = flap nocturno garantizado)."""
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    rule = AlarmRule.objects.filter(code="meter_comm_lost").first()
    if rule:
        rule.default_params = {**rule.default_params, "solar_margin_minutes": 30}
        rule.save(update_fields=["default_params"])


def remove_meter_solar_margin(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    rule = AlarmRule.objects.filter(code="meter_comm_lost").first()
    if rule:
        rule.default_params.pop("solar_margin_minutes", None)
        rule.save(update_fields=["default_params"])


class Migration(migrations.Migration):
    dependencies = [("alarms", "0007_weather_night_gate")]

    operations = [migrations.RunPython(add_meter_solar_margin, remove_meter_solar_margin)]
