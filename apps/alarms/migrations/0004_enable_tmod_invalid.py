from django.db import migrations

PARAMS = {
    "frozen_intervals": 3,
    "tmod_min_c": -10,
    "tmod_max_c": 90,
    "coherence_margin_c": 5,
    "poa_for_coherence_wm2": 300,
}

DESCRIPTION = (
    "T_mod (temperature_POA, sensor del panel) nula, congelada, fuera de "
    "rango físico o incoherente con el ambiente bajo POA alta."
)


def enable_tmod(apps, schema_editor):
    """T_mod quedó definido (2026-07-08): es temperature_POA, la temperatura
    del panel. Se habilita la regla 16 con sus umbrales."""
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    AlarmRule.objects.filter(code="tmod_invalid").update(
        enabled=True, default_params=PARAMS, description=DESCRIPTION
    )


def disable_tmod(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    AlarmRule.objects.filter(code="tmod_invalid").update(enabled=False, default_params={})


class Migration(migrations.Migration):
    dependencies = [("alarms", "0003_default_schedules")]

    operations = [migrations.RunPython(enable_tmod, disable_tmod)]
