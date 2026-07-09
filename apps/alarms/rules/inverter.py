"""Fase 3 — reglas por inversor."""

from apps.alarms.context import Unavailable

from .base import BaseRule, RuleOutcome, register
from .helpers import poa_sustained_above, window_average

# Keywords sobre `state`. El backend consulta inversores Huawei SUN2000: `state`
# es el enum "Device status" (registro Modbus 32089) en texto. Redacción exacta
# de SolarView POR CONFIRMAR con el censo InverterStateObservation (T31);
# observados reales: "Grid-connected", "Standby: insulation resistance detecting".
#
# Vocabulario Huawei esperado (por familia):
#   Standby: initializing | insulation resistance detecting | irradiation
#     detecting | grid detecting          → auto-tests, NO falla
#   Starting                              → transitorio del amanecer
#   Grid-connected (On-grid)              → normal
#   Grid connection: power limited        → DERATING ("limit" lo atrapa)
#   Grid connection: self-derating        → DERATING ("derat" lo atrapa)
#   Shutdown: fault | command | OVGR | communication disconnected |
#     power limited | manual startup required | DC switches disconnected |
#     rapid cutoff | input underpower     → apagados (la regla 2 los cubre)
#   Grid scheduling: cosφ-P | Q-U | PF-U | dry contact | Q-P curve
#                                         → gestión del operador de red, NO falla
#   Spot-check | Inspecting | AFCI self check | I-V scanning |
#     DC input detection                  → diagnósticos rutinarios
#   (de noche SolarView entrega state=None, censo 2026-07-08: 314/314)
DERATING_KEYWORDS = ("derat", "limit", "fan", "over-temp", "overtemp")


@register
class InverterUnavailable(BaseRule):
    """Regla 2: inversor no disponible.

    P_inv ≈ 0 con POA sostenida y OTROS inversores generando. La persistencia
    de 15 min se verifica con las corrientes DC del inversor (cadencia 5 min):
    potencia live ≈ 0 puede ser un instante; strings en 0 sostenido no.
    Exclusiones: comm lost (fase 1) → not_computable; mantenimiento → ok;
    todos los inversores caídos → ok (eso es project_no_generation).
    """

    code = "inverter_unavailable"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        inverters = ctx.inverters_live()
        if isinstance(inverters, Unavailable):
            return [RuleOutcome(status="not_computable", reason=inverters.reason)]

        params = ctx.params(self.code)
        poa_ok = poa_sustained_above(ctx, params)
        if poa_ok is None:
            return [RuleOutcome(status="not_computable", reason="poa:no_verificable")]
        if not poa_ok:
            return [
                RuleOutcome(status="ok", dedup_suffix=f"inv:{inv.id}",
                            inverter_external_id=inv.id, reason="excluded:low_irradiance")
                for inv in inverters
            ]

        zero_kw = params["power_zero_kw"]
        generating = [inv for inv in inverters if (inv.power or 0) > zero_kw]
        dc = ctx.string_currents()

        outcomes = []
        for inv in inverters:
            suffix = f"inv:{inv.id}"

            if ctx.flag_active("inverter_comm_lost", suffix):
                outcomes.append(
                    RuleOutcome(status="not_computable", dedup_suffix=suffix,
                                inverter_external_id=inv.id,
                                reason="excluded:inverter_comm_lost")
                )
                continue
            if ctx.in_maintenance(inverter=ctx.inverter_model(inv.id)):
                outcomes.append(
                    RuleOutcome(status="ok", dedup_suffix=suffix,
                                inverter_external_id=inv.id,
                                reason="excluded:maintenance")
                )
                continue
            if (inv.power or 0) > zero_kw:
                outcomes.append(
                    RuleOutcome(status="ok", dedup_suffix=suffix,
                                inverter_external_id=inv.id)
                )
                continue

            comparables = [g for g in generating if g.id != inv.id]
            if not comparables:
                outcomes.append(
                    RuleOutcome(status="ok", dedup_suffix=suffix,
                                inverter_external_id=inv.id,
                                reason="excluded:no_comparable_generating")
                )
                continue

            # persistencia: corrientes DC del inversor ≈ 0 en toda la ventana
            sustained_zero = None
            if not isinstance(dc, Unavailable) and inv.dev_name in dc:
                averages = [
                    window_average(ctx, cs_series, params["persistence_minutes"],
                                   params.get("data_lag_minutes", 5))
                    for cs_series in dc[inv.dev_name].values()
                ]
                known = [a for a in averages if a is not None]
                sustained_zero = bool(known) and all(a <= 0.1 for a in known)

            if sustained_zero is None:
                outcomes.append(
                    RuleOutcome(status="not_computable", dedup_suffix=suffix,
                                inverter_external_id=inv.id,
                                reason="dc:sin_ventana_para_persistencia")
                )
            elif sustained_zero:
                outcomes.append(
                    RuleOutcome(
                        status="firing", dedup_suffix=suffix,
                        inverter_external_id=inv.id,
                        evidence={
                            "dev_name": inv.dev_name,
                            "power_kw": inv.power,
                            "state": inv.state,
                            "comparables_generating": [c.dev_name for c in comparables],
                            "window_minutes": params["persistence_minutes"],
                        },
                    )
                )
            else:
                outcomes.append(
                    RuleOutcome(status="ok", dedup_suffix=suffix,
                                inverter_external_id=inv.id,
                                reason="dc:con_corriente_reciente")
                )
        return outcomes


@register
class InverterDerating(BaseRule):
    """Regla 3: derating. Dispara por `state` con keyword de limitación, o por
    temperatura > umbral con producción bajo el ratio de los comparables."""

    code = "inverter_derating"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        inverters = ctx.inverters_live()
        if isinstance(inverters, Unavailable):
            return [RuleOutcome(status="not_computable", reason=inverters.reason)]

        params = ctx.params(self.code)
        outcomes = []
        for inv in inverters:
            suffix = f"inv:{inv.id}"

            if ctx.flag_active("inverter_comm_lost", suffix):
                outcomes.append(
                    RuleOutcome(status="not_computable", dedup_suffix=suffix,
                                inverter_external_id=inv.id,
                                reason="excluded:inverter_comm_lost")
                )
                continue

            state = (inv.state or "").lower()
            if any(kw in state for kw in DERATING_KEYWORDS):
                outcomes.append(
                    RuleOutcome(
                        status="firing", dedup_suffix=suffix,
                        inverter_external_id=inv.id,
                        evidence={"trigger": "state", "state": inv.state,
                                  "dev_name": inv.dev_name},
                    )
                )
                continue

            others = [o.power for o in inverters if o.id != inv.id and o.power]
            avg_others = sum(others) / len(others) if others else None
            hot = (inv.temperature or 0) > params["temp_max_c"]
            underproducing = (
                avg_others is not None
                and (inv.power or 0) < params["comparable_low_ratio"] * avg_others
            )
            if hot and underproducing:
                outcomes.append(
                    RuleOutcome(
                        status="firing", dedup_suffix=suffix,
                        inverter_external_id=inv.id,
                        evidence={
                            "trigger": "temperature",
                            "temperature_c": inv.temperature,
                            "power_kw": inv.power,
                            "comparable_avg_kw": round(avg_others, 1),
                            "dev_name": inv.dev_name,
                        },
                    )
                )
            else:
                outcomes.append(
                    RuleOutcome(status="ok", dedup_suffix=suffix,
                                inverter_external_id=inv.id)
                )
        return outcomes
