"""Catálogo de las 20 alarmas de Fase 1 (Excel "Alarmas Solarview" + criterios COX).

Fuente única para la data migration de seed. Los umbrales aquí son los DEFAULTS
globales; los overrides por proyecto viven en RuleConfig.

Notas:
- `tmod_invalid` disabled: T_mod aún es incierto (decisión 2026-07-08).
- `thd_abnormal` disabled: la API no expone THD.
- Cadencias reales del sondeo T03: inversores 5 min, weather ~1 min.
"""

CATALOG = [
    {
        "code": "project_no_generation",
        "name": "Proyecto sin generación con irradiancia disponible",
        "description": (
            "POA > 100 W/m² durante 15 min con potencia total AC ≈ 0, reconectador "
            "cerrado y tensión coherente. Excluir: baja irradiancia, meteo inválida, "
            "mantenimiento, pérdida de red."
        ),
        "category": "project",
        "component_type": "project",
        "default_severity": "critical",
        "rule_group": "fast",
        "default_params": {
            "poa_min_wm2": 100,
            "persistence_minutes": 15,
            "power_zero_ratio": 0.005,  # ≈0 = < 0.5% de capacidad instalada
            "data_lag_minutes": 5,
            "min_window_coverage": 0.6,
        },
    },
    {
        "code": "inverter_unavailable",
        "name": "Inversor no disponible",
        "description": (
            "POA > 100 W/m², P_inv ≈ 0 durante ≥15 min con otros inversores comparables "
            "generando. Excluir: comunicación caída, red, mantenimiento."
        ),
        "category": "inverter",
        "component_type": "inverter",
        "default_severity": "critical",
        "rule_group": "fast",
        "default_params": {
            "poa_min_wm2": 100,
            "persistence_minutes": 15,
            "power_zero_kw": 0.1,
            "data_lag_minutes": 5,
        },
    },
    {
        "code": "inverter_derating",
        "name": "Potencia limitada / derating",
        "description": (
            "Alarma de derating/fan fault del inversor, o temperatura interna > 100°C "
            "con producción menor que comparables."
        ),
        "category": "inverter",
        "component_type": "inverter",
        "default_severity": "high",
        "rule_group": "fast",
        "default_params": {"temp_max_c": 80, "comparable_low_ratio": 0.8},
    },
    {
        "code": "inverter_comm_lost",
        "name": "Sin comunicación con inversor",
        "description": "El inversor no actualiza datos. Tratar como problema de comunicación.",
        "category": "inverter",
        "component_type": "inverter",
        "default_severity": "high",
        "rule_group": "fast",
        "default_params": {
            "stale_minutes": 15,
            "expected_cadence_minutes": 5,
            "data_lag_minutes": 5,
            "solar_margin_minutes": 45,  # de noche los inversores duermen: no evaluar
        },
    },
    {
        "code": "string_zero_current",
        "name": "String nulo",
        "description": (
            "I_string ≈ 0 durante ≥15 min con POA > 100, inversor activo y strings "
            "comparables > 1 A."
        ),
        "category": "dc_string",
        "component_type": "string",
        "default_severity": "critical",
        "rule_group": "fast",
        "default_params": {
            "poa_min_wm2": 100,
            "persistence_minutes": 15,
            "zero_current_a": 0.1,
            "comparable_min_current_a": 1.0,
        },
    },
    {
        "code": "string_low_current",
        "name": "String con corriente baja",
        "description": (
            "Corriente del string < 80% del promedio de strings comparables del mismo "
            "inversor, con POA > 100 durante 15 min."
        ),
        "category": "dc_string",
        "component_type": "string",
        "default_severity": "high",
        "rule_group": "fast",
        "default_params": {
            "poa_min_wm2": 100,
            "persistence_minutes": 15,
            "low_ratio": 0.8,
        },
    },
    {
        "code": "dc_isolation_low",
        "name": "Aislamiento DC bajo",
        "description": "El inversor reporta alarma de aislamiento. Requiere revisión en sitio.",
        "category": "dc_string",
        "component_type": "inverter",
        "default_severity": "critical",
        "rule_group": "fast",
        "default_params": {},
    },
    {
        "code": "meter_comm_lost",
        "name": "Medidor de frontera sin comunicación",
        "description": "El medidor de frontera no actualiza > 60 min mientras inversores sí.",
        "category": "meter",
        "component_type": "meter",
        "default_severity": "high",
        "rule_group": "hourly",
        "default_params": {"stale_minutes": 60, "solar_margin_minutes": 30},
    },
    {
        "code": "meter_no_increment",
        "name": "Medidor sin incremento con generación",
        "description": "ΔE_frontera ≈ 0 durante 60 min con generación confirmada y POA > 100.",
        "category": "meter",
        "component_type": "meter",
        "default_severity": "high",
        "rule_group": "hourly",
        "default_params": {
            "poa_min_wm2": 100,
            "window_minutes": 60,
            "delta_zero_kwh": 0.1,
        },
    },
    {
        "code": "meter_inverter_mismatch",
        "name": "Diferencia inversores vs frontera",
        "description": (
            "ABS(E_inv - E_frontera)/E_inv en ventana horaria: >3% alerta (high), "
            ">5% escala."
        ),
        "category": "meter",
        "component_type": "meter",
        "default_severity": "high",
        "rule_group": "hourly",
        "default_params": {"window_minutes": 60, "alert_ratio": 0.03, "high_ratio": 0.05},
    },
    {
        "code": "pr_inputs_missing",
        "name": "Datos insuficientes para PR",
        "description": (
            "Falta energía AC, POA, P_DC o T_mod (esta última solo si el proyecto "
            "tiene estación). Marca el intervalo como no calculable."
        ),
        "category": "data",
        "component_type": "project",
        "default_severity": "critical",
        "rule_group": "hourly",
        "default_params": {"solar_margin_minutes": 30},
    },
    {
        "code": "availability_inputs_missing",
        "name": "Datos insuficientes para disponibilidad",
        "description": (
            "Falta POA válida, potencia/estado del inversor o timestamp. Marcar como "
            "no calculable por inversor, sin inventar 0%/100%."
        ),
        "category": "data",
        "component_type": "inverter",
        "default_severity": "high",
        "rule_group": "fast",
        "default_params": {},
    },
    {
        "code": "data_frozen",
        "name": "Datos congelados",
        "description": (
            "POA, potencia o temperatura sin cambio durante 3 intervalos consecutivos "
            "cuando deberían variar. No aplica de noche."
        ),
        "category": "data",
        "component_type": "project",
        "default_severity": "medium",
        "rule_group": "fast",
        "default_params": {"frozen_intervals": 3},
    },
    {
        "code": "weather_comm_lost",
        "name": "Estación meteorológica sin comunicación",
        "description": "La estación meteo no actualiza datos (cadencia real ~1 min).",
        "category": "weather",
        "component_type": "weather_station",
        "default_severity": "high",
        "rule_group": "fast",
        "default_params": {
            "stale_minutes": 5,
            "expected_cadence_minutes": 1,
            "data_lag_minutes": 5,
            "solar_margin_minutes": 30,
        },
    },
    {
        "code": "poa_invalid",
        "name": "POA inválida o congelada",
        "description": (
            "POA < 0, POA = 0 con generación real, o sin variación durante 3 intervalos "
            "en horario solar."
        ),
        "category": "weather",
        "component_type": "weather_station",
        "default_severity": "high",
        "rule_group": "fast",
        "default_params": {"frozen_intervals": 3},
    },
    {
        "code": "tmod_invalid",
        "name": "Temperatura de módulo inválida",
        "description": (
            "T_mod (temperature_POA, sensor del panel) nula, congelada, fuera de "
            "rango físico o incoherente con el ambiente bajo POA alta."
        ),
        "category": "weather",
        "component_type": "weather_station",
        "default_severity": "medium",
        "rule_group": "fast",
        "default_params": {
            "frozen_intervals": 3,
            "tmod_min_c": -10,
            "tmod_max_c": 90,
            "coherence_margin_c": 5,
            "poa_for_coherence_wm2": 300,
        },
    },
    {
        "code": "recloser_open",
        "name": "Reconectador abierto o disparado",
        "description": (
            "Estado del reconectador abierto/trip/protección durante horario solar. "
            "active=null en la API = estado desconocido (not_computable)."
        ),
        "category": "grid",
        "component_type": "relay",
        "default_severity": "critical",
        "rule_group": "fast",
        "default_params": {},
    },
    {
        "code": "power_factor_low",
        "name": "Factor de potencia fuera de rango",
        "description": (
            "FP < 0.95. Validar baja carga: con poca potencia el FP no es representativo."
        ),
        "category": "power_quality",
        "component_type": "relay",
        "default_severity": "medium",
        "rule_group": "fast",
        "default_params": {"pf_min": 0.95, "min_load_current_a": 5},
    },
    {
        "code": "thd_abnormal",
        "name": "Reactiva o armónicos anormales",
        "description": "DESHABILITADA: la API actual no expone THD ni variables de calidad.",
        "category": "power_quality",
        "component_type": "meter",
        "default_severity": "medium",
        "rule_group": "fast",
        "default_params": {},
        "enabled": False,
    },
    {
        "code": "alarm_sla_breach",
        "name": "Evento cerca de vencer o vencido",
        "description": (
            "Alarma ACTIVE sin reconocer por más del SLA. Escala al doble del SLA. "
            "Corre en el task check_sla, no en el engine."
        ),
        "category": "om",
        "component_type": "project",
        "default_severity": "high",
        "rule_group": "fast",
        "default_params": {"sla_ack_minutes": 60, "escalate_after_multiplier": 2},
    },
]


def seed_rules(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    for entry in CATALOG:
        entry = dict(entry)
        code = entry.pop("code")
        AlarmRule.objects.update_or_create(code=code, defaults=entry)


def unseed_rules(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    AlarmRule.objects.filter(code__in=[e["code"] for e in CATALOG]).delete()
