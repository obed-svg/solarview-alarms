"""Fase 3 — reglas a nivel de proyecto."""

from apps.alarms.context import Unavailable

from .base import BaseRule, RuleOutcome, register

MIN_WINDOW_POINTS = 3  # menos puntos que esto no prueban una condición "sostenida"


@register
class ProjectNoGeneration(BaseRule):
    """Regla 1: proyecto sin generación con irradiancia disponible.

    Dispara si POA > umbral sostenido en la ventana Y potencia AC total ≈ 0,
    con el reconectador no-abierto. Exclusiones (en orden):
    - mantenimiento registrado → ok
    - meteo marcada inválida/caída en este tick → not_computable
    - reconectador abierto → ok (eso es la regla recloser_open)
    - POA bajo el umbral en algún punto → ok (amanecer/atardecer/nublado)

    Sobre el reconectador: `active=null` (visto en la API real) o proyecto sin
    relay NO bloquean la alarma — la condición eléctrica basta; el estado se
    registra en evidence como "unknown"/"none". Las tensiones u_a/u_b/u_c del
    relay real llegaron en 0 con la planta operando, así que NO se usan como
    condición hasta validar su semántica con el backend.
    """

    code = "project_no_generation"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        if ctx.in_maintenance():
            return [RuleOutcome(status="ok", reason="excluded:maintenance")]

        for meteo_flag in ("poa_invalid", "weather_comm_lost", "data_frozen"):
            if ctx.flag_active(meteo_flag):
                return [
                    RuleOutcome(status="not_computable", reason=f"excluded:{meteo_flag}")
                ]

        params = ctx.params(self.code)
        lag = params.get("data_lag_minutes", 5)
        minutes = params["persistence_minutes"]

        poa = ctx.poa_series()
        if isinstance(poa, Unavailable):
            return [RuleOutcome(status="not_computable", reason=f"poa:{poa.reason}")]
        poa_window = ctx.series_window(poa, minutes, lag)
        poa_values = [v for v in poa_window.values() if v is not None]
        if len(poa_values) < MIN_WINDOW_POINTS:
            return [RuleOutcome(status="not_computable", reason="poa:window_insuficiente")]

        if min(poa_values) <= params["poa_min_wm2"]:
            return [RuleOutcome(status="ok", reason="excluded:low_irradiance")]

        power = ctx.power()
        if isinstance(power, Unavailable):
            return [RuleOutcome(status="not_computable", reason=f"power:{power.reason}")]
        power_window = ctx.series_window(power.power, minutes, lag)
        power_values = [v for v in power_window.values() if v is not None]
        if len(power_values) < MIN_WINDOW_POINTS:
            return [RuleOutcome(status="not_computable", reason="power:window_insuficiente")]

        relay = ctx.relay()
        if isinstance(relay, Unavailable):
            relay_state = "none" if relay.reason == "not_associated" else "unavailable"
        elif relay.active is None:
            relay_state = "unknown"
        elif relay.active:
            relay_state = "closed"
        else:
            return [RuleOutcome(status="ok", reason="excluded:recloser_open")]

        capacity = float(ctx.project.installed_capacity_kw or 0)
        zero_threshold_kw = (
            capacity * params["power_zero_ratio"] if capacity else 1.0
        )

        if max(power_values) <= zero_threshold_kw:
            return [
                RuleOutcome(
                    status="firing",
                    evidence={
                        "poa_min_wm2_observed": min(poa_values),
                        "power_max_kw_observed": max(power_values),
                        "zero_threshold_kw": round(zero_threshold_kw, 2),
                        "window_minutes": minutes,
                        "window_points": len(power_values),
                        "relay_state": relay_state,
                    },
                )
            ]
        return [RuleOutcome(status="ok")]
