from django.db import migrations


def apply_t35_params(apps, schema_editor):
    """T35 (decisiones del usuario 2026-07-08):
    - Regla 18: gate de carga por CORRIENTE (min_load_current_a, default 5 A
      conservador — calibración empírica por proyecto vía RuleConfig en T32).
      min_load_kw se retira: NUNCA usar relay.kw en lógica.
    - Regla 3: temp_max_c 100 → 80 °C (alarma típica de los Huawei SUN2000
      sobre temperatura interna)."""
    AlarmRule = apps.get_model("alarms", "AlarmRule")

    pf_rule = AlarmRule.objects.filter(code="power_factor_low").first()
    if pf_rule:
        pf_rule.default_params.pop("min_load_kw", None)
        pf_rule.default_params["min_load_current_a"] = 5
        pf_rule.save(update_fields=["default_params"])

    derating = AlarmRule.objects.filter(code="inverter_derating").first()
    if derating:
        derating.default_params["temp_max_c"] = 80
        derating.save(update_fields=["default_params"])


def revert_t35_params(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")

    pf_rule = AlarmRule.objects.filter(code="power_factor_low").first()
    if pf_rule:
        pf_rule.default_params.pop("min_load_current_a", None)
        pf_rule.default_params["min_load_kw"] = 10
        pf_rule.save(update_fields=["default_params"])

    derating = AlarmRule.objects.filter(code="inverter_derating").first()
    if derating:
        derating.default_params["temp_max_c"] = 100
        derating.save(update_fields=["default_params"])


class Migration(migrations.Migration):
    dependencies = [("alarms", "0005_solar_margin_params")]

    operations = [migrations.RunPython(apply_t35_params, revert_t35_params)]
