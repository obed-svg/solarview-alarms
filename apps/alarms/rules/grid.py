"""Fase 3 — red/MT y calidad de energía (reconectador)."""

from apps.alarms.context import Unavailable

from .base import BaseRule, RuleOutcome, register
from .relay_normalize import normalize_relay


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
                    "kw": relay.kw,
                    "f_abc": relay.f_abc,
                    "measured_at": str(relay.time),
                },
            )
        ]


@register
class PowerFactorLow(BaseRule):
    """Regla 18: FP bajo el umbral, solo con carga suficiente (en baja carga
    el FP no es representativo)."""

    code = "power_factor_low"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        relay = ctx.relay()
        if isinstance(relay, Unavailable):
            if relay.reason == "not_associated":
                return []
            return [RuleOutcome(status="not_computable", reason=f"relay:{relay.reason}")]

        # FP solo es representativo cuando la planta genera: de noche solo hay
        # consumo auxiliar con FP naturalmente malo.
        if not ctx.is_solar_hours():
            return [RuleOutcome(status="ok", reason="excluded:night")]

        params = ctx.params(self.code)

        # normalización de unidades (cada marca de reconectador reporta distinto):
        # ancla 1 = capacidad instalada; ancla 2 = potencia de inversores
        inverter_total = None
        inverters = ctx.inverters_live()
        if not isinstance(inverters, Unavailable):
            inverter_total = sum(inv.power or 0 for inv in inverters)
        normalized = normalize_relay(
            relay,
            capacity_kw=float(ctx.project.installed_capacity_kw or 0) or None,
            inverter_total_kw=inverter_total,
        )

        if normalized.kw is None:
            # unidad irresoluble; pero si BAJO CUALQUIER escala candidata la
            # carga queda bajo el umbral, la decisión es la misma: baja carga
            from .relay_normalize import KW_SCALES

            if relay.kw is not None and all(
                relay.kw * s < params["min_load_kw"] for s in KW_SCALES
            ):
                return [RuleOutcome(status="ok", reason="excluded:low_load")]
            return [
                RuleOutcome(status="not_computable", reason="relay:unidad_kw_ambigua")
            ]
        if normalized.kw < params["min_load_kw"]:
            return [RuleOutcome(status="ok", reason="excluded:low_load")]
        if normalized.pf is None:
            return [RuleOutcome(status="not_computable", reason="relay:pf_ausente")]

        if normalized.pf < params["pf_min"]:
            return [
                RuleOutcome(
                    status="firing",
                    evidence={
                        "pf": normalized.pf,
                        "pf_min": params["pf_min"],
                        "kw": round(normalized.kw, 2),
                        "kw_raw": relay.kw,
                        "normalization": normalized.notes,
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
