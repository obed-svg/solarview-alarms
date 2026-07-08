from django.db import migrations


def add_solar_margins(apps, schema_editor):
    """Anti-ruido nocturno (decisión 2026-07-08): las reglas de comunicación y
    de insumos no evalúan cerca del ocaso/amanecer, cuando los inversores
    duermen legítimamente."""
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    for code, margin in [
        ("inverter_comm_lost", 45),
        ("availability_inputs_missing", 45),
        ("pr_inputs_missing", 30),
    ]:
        rule = AlarmRule.objects.filter(code=code).first()
        if rule:
            rule.default_params = {**rule.default_params, "solar_margin_minutes": margin}
            rule.save(update_fields=["default_params"])


def remove_solar_margins(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    for code in ("inverter_comm_lost", "availability_inputs_missing", "pr_inputs_missing"):
        rule = AlarmRule.objects.filter(code=code).first()
        if rule:
            rule.default_params.pop("solar_margin_minutes", None)
            rule.save(update_fields=["default_params"])


class Migration(migrations.Migration):
    dependencies = [("alarms", "0004_enable_tmod_invalid")]

    operations = [migrations.RunPython(add_solar_margins, remove_solar_margins)]
