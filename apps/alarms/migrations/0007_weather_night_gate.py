from django.db import migrations


def add_weather_solar_margin(apps, schema_editor):
    """T36: la regla 14 solo evalúa en horario solar. Observado 2026-07-08 de
    noche: las 8 estaciones de la flota "stale" con el MISMO last_data_at
    (20:58:10 idéntico al segundo) — el escritor de weather del backend se
    detiene de noche por sistema; no son fallas de estación."""
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    rule = AlarmRule.objects.filter(code="weather_comm_lost").first()
    if rule:
        rule.default_params = {**rule.default_params, "solar_margin_minutes": 30}
        rule.save(update_fields=["default_params"])


def remove_weather_solar_margin(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    rule = AlarmRule.objects.filter(code="weather_comm_lost").first()
    if rule:
        rule.default_params.pop("solar_margin_minutes", None)
        rule.save(update_fields=["default_params"])


class Migration(migrations.Migration):
    dependencies = [("alarms", "0006_t35_relay_params")]

    operations = [migrations.RunPython(add_weather_solar_margin, remove_weather_solar_margin)]
