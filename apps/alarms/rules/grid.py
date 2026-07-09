"""Fase 3 — red/MT y calidad de energía (reconectador).

Decisiones del usuario (2026-07-08, T35):
- "Planta activa" = `active` de /project/{id}/relay/ — ÚNICA señal de
  abierto/cerrado (el backend ya condensa las tensiones u_a/b/c lado planta y
  u_r/s/t lado red en `active`; nunca re-derivarlo de voltajes).
- NUNCA usar `relay.kw` en lógica: cada reconectador reporta la potencia en
  unidades distintas y algunos la leen mal. El gate de carga de la regla 18 es
  por CORRIENTE (amperios, sin ambigüedad de escala). Sin corrientes →
  not_computable, sin fallback.
- Relays con firmware desactualizado entregan lecturas enteras sin sentido
  (fixture real: i_a/b/c=34 A con kw=1, kva=1, pf=0, tensiones=0) → pf=0 con
  corriente fluyendo se reporta como diagnóstico de firmware, no como alarma.
"""

from apps.alarms.context import Unavailable

from .base import BaseRule, RuleOutcome, register
from .relay_normalize import normalize_pf


@register
class RecloserOpen(BaseRule):
    """Regla 17: reconectador abierto/disparado en horario solar.

    `active=null` (real en la API) = estado desconocido → not_computable.
    Apertura dentro de ventana de mantenimiento = programada → ok.
    """

    code = "recloser_open"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        relay = ctx.relay()
        if isinstance(relay, Unavailable):
            if relay.reason == "not_associated":
                return []
            return [RuleOutcome(status="not_computable", reason=f"relay:{relay.reason}")]

        if not ctx.is_solar_hours():
            return [RuleOutcome(status="ok", reason="excluded:night")]

        if relay.active is None:
            return [
                RuleOutcome(status="not_computable", reason="relay:active_desconocido")
            ]
        if relay.active:
            return [RuleOutcome(status="ok")]

        if ctx.in_maintenance():
            return [RuleOutcome(status="ok", reason="excluded:maintenance")]

        return [
            RuleOutcome(
                status="firing",
                evidence={
                    "active": relay.active,
                    "currents_a": relay.currents,
                    "f_abc": relay.f_abc,
                    "measured_at": str(relay.time),
                },
            )
        ]


@register
class PowerFactorLow(BaseRule):
    """Regla 18: FP bajo el umbral, solo con carga suficiente (en baja carga
    el FP no es representativo).

    Gate de carga por CORRIENTE (max de i_a/i_b/i_c ≥ min_load_current_a) —
    nunca por relay.kw (ver docstring del módulo). NO aplica en autoconsumo
    (el pf de frontera lo domina la carga del cliente, no la planta).
    pf=0 exacto con corriente fluyendo = firmware desactualizado del
    reconectador → not_computable con diagnóstico (las lecturas crudas van
    como evidencia forense, jamás a una decisión)."""

    code = "power_factor_low"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        if ctx.project.is_self_consumption:
            return []

        relay = ctx.relay()
        if isinstance(relay, Unavailable):
            if relay.reason == "not_associated":
                return []
            return [RuleOutcome(status="not_computable", reason=f"relay:{relay.reason}")]

        # FP solo es representativo cuando la planta genera: de noche solo hay
        # consumo auxiliar con FP naturalmente malo.
        if not ctx.is_solar_hours():
            return [RuleOutcome(status="ok", reason="excluded:night")]

        # planta abierta: sin flujo no hay pf que evaluar (la 17 alarma la apertura)
        if relay.active is False:
            return [RuleOutcome(status="ok", reason="excluded:recloser_open")]

        params = ctx.params(self.code)

        currents = [
            relay.currents.get(phase)
            for phase in ("i_a", "i_b", "i_c")
            if relay.currents.get(phase) is not None
        ]
        if not currents:
            return [RuleOutcome(status="not_computable", reason="relay:sin_corrientes")]

        max_current = max(currents)
        if max_current < params["min_load_current_a"]:
            return [RuleOutcome(status="ok", reason="excluded:low_load")]

        pf = normalize_pf(relay.pf)
        if pf is None:
            return [RuleOutcome(status="not_computable", reason="relay:pf_ausente")]
        if pf == 0:
            # corriente fluyendo con pf=0 exacto: lectura implausible — relays
            # sin actualización de firmware entregan enteros (kw=1, pf=0,
            # tensiones=0). Diagnóstico visible para gestionar el firmware.
            return [
                RuleOutcome(
                    status="not_computable",
                    reason=(
                        "relay:pf_cero_con_carga (lecturas enteras — probable "
                        "firmware desactualizado del reconectador)"
                    ),
                    evidence={
                        "currents_a": relay.currents,
                        "raw_readings": {
                            "kw": relay.kw, "kva": relay.kva, "pf": relay.pf,
                            "voltages": relay.voltages,
                        },
                        "measured_at": str(relay.time),
                    },
                )
            ]

        if pf < params["pf_min"]:
            return [
                RuleOutcome(
                    status="firing",
                    evidence={
                        "pf": pf,
                        "pf_min": params["pf_min"],
                        "currents_a": relay.currents,
                        "min_load_current_a": params["min_load_current_a"],
                        "measured_at": str(relay.time),
                    },
                )
            ]
        return [RuleOutcome(status="ok")]


@register
class ThdAbnormal(BaseRule):
    """Regla 19 — STUB deshabilitado en el seed: la API no expone THD ni
    variables de calidad de energía."""

    code = "thd_abnormal"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        return []
