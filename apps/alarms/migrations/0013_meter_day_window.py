from django.db import migrations

NEW_RATIOS = {"alert_ratio": 0.05, "high_ratio": 0.10}
OLD_RATIOS = {"alert_ratio": 0.03, "high_ratio": 0.05}

NEW_DESCRIPTIONS = {
    "meter_inverter_mismatch": (
        "ABS(E_inv - E_frontera)/E_inv sobre la energía acumulada del día: "
        ">5% alerta (medium), >10% escala (high)."
    ),
    "meter_no_increment": (
        "E_frontera ≈ 0 en lo que va del día con generación confirmada de "
        "los inversores."
    ),
}
OLD_DESCRIPTIONS = {
    "meter_inverter_mismatch": (
        "ABS(E_inv - E_frontera)/E_inv en ventana horaria: >3% alerta (high), "
        ">5% escala."
    ),
    "meter_no_increment": (
        "ΔE_frontera ≈ 0 durante 60 min con generación confirmada y POA > 100."
    ),
}


def _apply(apps, ratios, descriptions):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    for code, description in descriptions.items():
        rule = AlarmRule.objects.filter(code=code).first()
        if not rule:
            continue
        rule.description = description
        if code == "meter_inverter_mismatch":
            rule.default_params = {**rule.default_params, **ratios}
        rule.save(update_fields=["description", "default_params"])


def to_day_window(apps, schema_editor):
    """T50: reglas 9/10 comparan la energía ACUMULADA DEL DÍA, no una hora.

    Censo real del 2026-07-24 sobre 26 medidores: con ventana de una hora el
    ruido mediano entre plantas SANAS era 51% (peor caso 290%) — 10 alarmas
    falsas en el primer arranque. Acumulando el día el ruido cae a 1.0%, con
    las plantas sanas de cadencia fina entre 0.1% y 1.6%, y los dos casos
    patológicos reales (Valencia Or_1 y El Son, medidores midiendo incompleto)
    en 34% y 46%. Los umbrales suben a 5%/10%: sobre el día son mucho más
    estrictos en la práctica que el 3%/5% horario, y dejan 3× de margen sobre
    la peor planta sana observada.
    """
    _apply(apps, NEW_RATIOS, NEW_DESCRIPTIONS)


def to_hour_window(apps, schema_editor):
    _apply(apps, OLD_RATIOS, OLD_DESCRIPTIONS)


class Migration(migrations.Migration):
    dependencies = [("alarms", "0012_meter_full_hour_window")]

    operations = [migrations.RunPython(to_day_window, to_hour_window)]
