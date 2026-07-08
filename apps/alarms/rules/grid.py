"""Fase 3 — red/MT y calidad de energía (reconectador)."""

from apps.alarms.context import Unavailable

from .base import BaseRule, RuleOutcome, register


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

        params = ctx.params(self.code)

        if relay.kw is None or relay.kw < params["min_load_kw"]:
            return [RuleOutcome(status="ok", reason="excluded:low_load")]
        if relay.pf is None:
            return [RuleOutcome(status="not_computable", reason="relay:pf_ausente")]

        if abs(relay.pf) < params["pf_min"]:
            return [
                RuleOutcome(
                    status="firing",
                    evidence={
                        "pf": relay.pf,
                        "pf_min": params["pf_min"],
                        "kw": relay.kw,
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
