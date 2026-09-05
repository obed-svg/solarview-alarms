from django.db import migrations

DESCRIPTION = (
    "SolarView no calculó el PR del día anterior después de su corte diario. "
    "Consulta el resultado histórico de SolarView a partir de las 08:05."
)


def update_pr_rule(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    rule = AlarmRule.objects.filter(code="pr_inputs_missing").first()
    if rule is None:
        return
    params = dict(rule.default_params)
    params.pop("solar_margin_minutes", None)
    params.update({"calculation_hour": 8, "calculation_minute": 5})
    rule.default_params = params
    rule.description = DESCRIPTION
    rule.save(update_fields=["default_params", "description"])


class Migration(migrations.Migration):
    dependencies = [("alarms", "0016_recloser_comm_lost")]
    operations = [migrations.RunPython(update_pr_rule, migrations.RunPython.noop)]
