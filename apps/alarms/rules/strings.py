"""Fase 3 — reglas de strings DC y aislamiento."""

from apps.alarms.context import Unavailable

from .base import BaseRule, RuleOutcome, register
from .helpers import dev_name_to_external_id, poa_sustained_above, window_average

# El state real es "Modo: detalle" (enum Huawei SUN2000, ver inverter.py; visto:
# "Standby: insulation resistance detecting" = AUTO-TEST rutinario, no falla).
# Solo es falla si el texto de aislamiento viene acompañado de un calificador de
# problema. NOTA Huawei: la falla real de aislamiento llega como ALARMA separada
# ("Low insulation resistance", ID 2062) con state genérico "Shutdown: fault";
# el Modbus expone la resistencia en MΩ (registro 32088) — pedir al backend
# exponerla para pasar de keywords a umbral físico (T31).
ISOLATION_KEYWORDS = ("isolat", "insulat", "aislam")
FAULT_QUALIFIERS = ("low", "fault", "abnormal", "fail", "baja", "falla")


def _string_averages(ctx, dc_by_inverter, params) -> dict[str, dict[str, float]]:
    """{dev_name: {cs_name: promedio de la ventana}} — solo strings con datos."""
    minutes = params["persistence_minutes"]
    lag = params.get("data_lag_minutes", 5)
    result = {}
    for dev_name, variables in dc_by_inverter.items():
        averages = {}
        for cs_name, cs_series in variables.items():
            avg = window_average(ctx, cs_series, minutes, lag)
            if avg is not None:
                averages[cs_name] = avg
        if averages:
            result[dev_name] = averages
    return result


class _StringRuleBase(BaseRule):
    """Base común: valida POA, trae corrientes y delega la evaluación por string."""

    def evaluate(self, ctx) -> list[RuleOutcome]:
        params = ctx.params(self.code)
        poa_ok = poa_sustained_above(ctx, params)
        if poa_ok is None:
            return [RuleOutcome(status="not_computable", reason="poa:no_verificable")]
        if not poa_ok:
            return [RuleOutcome(status="ok", reason="excluded:low_irradiance")]

        dc = ctx.string_currents()
        if isinstance(dc, Unavailable):
            return [RuleOutcome(status="not_computable", reason=f"dc:{dc.reason}")]
        inverters = ctx.inverters_live()
        if isinstance(inverters, Unavailable):
            return [RuleOutcome(status="not_computable", reason=inverters.reason)]

        ids_by_dev = dev_name_to_external_id(inverters)
        outcomes = []
        for dev_name, averages in _string_averages(ctx, dc, params).items():
            ext_id = ids_by_dev.get(dev_name)
            suffix_base = f"inv:{ext_id}" if ext_id else f"dev:{dev_name}"

            if ext_id and ctx.flag_active("inverter_comm_lost", f"inv:{ext_id}"):
                outcomes.extend(
                    RuleOutcome(status="not_computable",
                                dedup_suffix=f"{suffix_base}:{cs}",
                                inverter_external_id=ext_id, component_id=cs,
                                reason="excluded:inverter_comm_lost")
                    for cs in averages
                )
                continue

            for cs_name in averages:
                outcomes.extend(
                    self.evaluate_string(ctx, params, dev_name, ext_id, suffix_base,
                                         cs_name, averages)
                )
        return outcomes

    def evaluate_string(self, ctx, params, dev_name, ext_id, suffix_base,
                        cs_name, averages) -> list[RuleOutcome]:
        raise NotImplementedError


@register
class StringZeroCurrent(_StringRuleBase):
    """Regla 5: string nulo — I ≈ 0 sostenida mientras strings hermanas del
    mismo inversor superan el mínimo comparable. Si TODAS están en 0 no es
    alarma de string (es el inversor o el proyecto)."""

    code = "string_zero_current"
    phase = 3

    def evaluate_string(self, ctx, params, dev_name, ext_id, suffix_base,
                        cs_name, averages):
        avg = averages[cs_name]
        siblings = {k: v for k, v in averages.items() if k != cs_name}
        comparable = [v for v in siblings.values()
                      if v > params["comparable_min_current_a"]]

        if avg <= params["zero_current_a"] and comparable:
            return [
                RuleOutcome(
                    status="firing", dedup_suffix=f"{suffix_base}:{cs_name}",
                    inverter_external_id=ext_id, component_id=cs_name,
                    evidence={
                        "dev_name": dev_name, "string": cs_name,
                        "avg_current_a": round(avg, 2),
                        "siblings_avg_a": {k: round(v, 2) for k, v in siblings.items()},
                        "window_minutes": params["persistence_minutes"],
                    },
                )
            ]
        return [
            RuleOutcome(status="ok", dedup_suffix=f"{suffix_base}:{cs_name}",
                        inverter_external_id=ext_id, component_id=cs_name)
        ]


@register
class StringLowCurrent(_StringRuleBase):
    """Regla 6: string con corriente < low_ratio × promedio de sus hermanas.
    Strings en ~0 se excluyen: las cubre string_zero_current."""

    code = "string_low_current"
    phase = 3

    def evaluate_string(self, ctx, params, dev_name, ext_id, suffix_base,
                        cs_name, averages):
        avg = averages[cs_name]
        suffix = f"{suffix_base}:{cs_name}"
        zero_threshold = ctx.params("string_zero_current")["zero_current_a"]

        if avg <= zero_threshold:
            return [
                RuleOutcome(status="ok", dedup_suffix=suffix,
                            inverter_external_id=ext_id, component_id=cs_name,
                            reason="excluded:zero_string")
            ]

        siblings = [v for k, v in averages.items() if k != cs_name]
        if not siblings:
            return [
                RuleOutcome(status="ok", dedup_suffix=suffix,
                            inverter_external_id=ext_id, component_id=cs_name,
                            reason="excluded:sin_comparables")
            ]
        siblings_avg = sum(siblings) / len(siblings)

        # comparables con corriente insignificante (goteo del atardecer/amanecer)
        # no sirven de línea base: 0.11 vs 0.17 A no es un string degradado
        min_baseline = ctx.params("string_zero_current")["comparable_min_current_a"]
        if siblings_avg < min_baseline:
            return [
                RuleOutcome(status="ok", dedup_suffix=suffix,
                            inverter_external_id=ext_id, component_id=cs_name,
                            reason="excluded:baseline_insignificante")
            ]

        if avg < params["low_ratio"] * siblings_avg:
            return [
                RuleOutcome(
                    status="firing", dedup_suffix=suffix,
                    inverter_external_id=ext_id, component_id=cs_name,
                    evidence={
                        "dev_name": dev_name, "string": cs_name,
                        "avg_current_a": round(avg, 2),
                        "comparable_avg_a": round(siblings_avg, 2),
                        "ratio": round(avg / siblings_avg, 2),
                        "threshold_ratio": params["low_ratio"],
                    },
                )
            ]
        return [
            RuleOutcome(status="ok", dedup_suffix=suffix,
                        inverter_external_id=ext_id, component_id=cs_name)
        ]


@register
class DcIsolationLow(BaseRule):
    """Regla 7: alarma de aislamiento reportada por el inversor (vía `state`).
    Keywords base — los códigos reales de falla no se han observado aún (T03)."""

    code = "dc_isolation_low"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        inverters = ctx.inverters_live()
        if isinstance(inverters, Unavailable):
            return [RuleOutcome(status="not_computable", reason=inverters.reason)]

        outcomes = []
        for inv in inverters:
            state = (inv.state or "").lower()
            suffix = f"inv:{inv.id}"
            mentions_isolation = any(kw in state for kw in ISOLATION_KEYWORDS)
            is_fault = mentions_isolation and any(q in state for q in FAULT_QUALIFIERS)
            if is_fault:
                outcomes.append(
                    RuleOutcome(
                        status="firing", dedup_suffix=suffix,
                        inverter_external_id=inv.id,
                        evidence={"dev_name": inv.dev_name, "state": inv.state},
                    )
                )
            else:
                outcomes.append(
                    RuleOutcome(status="ok", dedup_suffix=suffix,
                                inverter_external_id=inv.id)
                )
        return outcomes
