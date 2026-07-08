"""Fase 3 — reglas de medidor de frontera (quoia).

⚠️ Estado T03/T18: el endpoint quoia devuelve 500 en TODOS los proyectos
(bug del backend). Estas reglas están implementadas contra la forma documentada
del payload ({ts: {value, unit}}, contador acumulado) y viven en not_computable
mientras quoia siga caído. Validar contra datos reales cuando lo arreglen
(pendiente en Bloqueadas del ROADMAP).
"""

from datetime import timedelta

from apps.alarms.context import Unavailable
from apps.alarms.models import Severity
from integrations.solarview.schemas import parse_ts

from .base import BaseRule, RuleOutcome, register
from .helpers import poa_sustained_above

MIN_COUNTER_POINTS = 2


def _frontier_delta_kwh(ctx, quoia_raw: dict, window_minutes: int) -> float | None:
    """ΔE del contador acumulado de frontera dentro de la ventana. None si no
    hay puntos suficientes."""
    cutoff = ctx.now - timedelta(minutes=window_minutes)
    points = []
    for key, payload in quoia_raw.items():
        ts = parse_ts(key)
        if ts is not None and ts >= cutoff and isinstance(payload, dict):
            points.append((ts, payload.get("value")))
    points.sort()
    values = [v for _, v in points if v is not None]
    if len(values) < MIN_COUNTER_POINTS:
        return None
    return values[-1] - values[0]


def _inverter_energy_kwh(ctx, window_minutes: int) -> float | None:
    """Energía de inversores en la ventana según /generation/ (serie horaria)."""
    gen = ctx.generation()
    if isinstance(gen, Unavailable):
        return None
    # borde EXCLUSIVO: el punto horario en now-60min etiqueta la hora anterior
    # completa; incluirlo duplicaría la energía de la ventana
    cutoff = ctx.now - timedelta(minutes=window_minutes)
    values = [v for ts, v in gen.hourly.items() if ts > cutoff and v is not None]
    if not values:
        return None
    return sum(values)


@register
class MeterNoIncrement(BaseRule):
    """Regla 9: el contador de frontera no aumenta aunque los inversores generan
    y hay POA. Sin medidor quoia la regla no aplica."""

    code = "meter_no_increment"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        quoia = ctx.quoia()
        if isinstance(quoia, Unavailable):
            if quoia.reason == "not_associated":
                return []
            return [RuleOutcome(status="not_computable", reason=f"quoia:{quoia.reason}")]

        params = ctx.params(self.code)
        window = params["window_minutes"]

        poa_ok = poa_sustained_above(
            ctx, {**params, "persistence_minutes": window}
        )
        if poa_ok is None:
            return [RuleOutcome(status="not_computable", reason="poa:no_verificable")]
        if not poa_ok:
            return [RuleOutcome(status="ok", reason="excluded:low_irradiance")]

        delta = _frontier_delta_kwh(ctx, quoia, window)
        if delta is None:
            return [RuleOutcome(status="not_computable", reason="quoia:ventana_insuficiente")]

        inverter_energy = _inverter_energy_kwh(ctx, window)
        if inverter_energy is None:
            return [
                RuleOutcome(status="not_computable", reason="generation:no_disponible")
            ]

        if delta <= params["delta_zero_kwh"] and inverter_energy > params["delta_zero_kwh"]:
            return [
                RuleOutcome(
                    status="firing",
                    evidence={
                        "delta_kwh": round(delta, 2),
                        "inverter_energy_kwh": round(inverter_energy, 2),
                        "window_minutes": window,
                    },
                )
            ]
        return [RuleOutcome(status="ok")]


@register
class MeterInverterMismatch(BaseRule):
    """Regla 10: |E_inv - E_frontera| / E_inv en ventana horaria.
    >5% → high; 3-5% → medium (escala si empeora, el engine no baja severidad)."""

    code = "meter_inverter_mismatch"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        quoia = ctx.quoia()
        if isinstance(quoia, Unavailable):
            if quoia.reason == "not_associated":
                return []
            return [RuleOutcome(status="not_computable", reason=f"quoia:{quoia.reason}")]

        params = ctx.params(self.code)
        window = params["window_minutes"]

        frontier = _frontier_delta_kwh(ctx, quoia, window)
        if frontier is None:
            return [RuleOutcome(status="not_computable", reason="quoia:ventana_insuficiente")]

        inverter_energy = _inverter_energy_kwh(ctx, window)
        if not inverter_energy:  # None o 0: sin denominador no hay ratio
            return [
                RuleOutcome(status="not_computable", reason="generation:sin_energia_inv")
            ]

        ratio = abs(inverter_energy - frontier) / inverter_energy
        evidence = {
            "inverter_energy_kwh": round(inverter_energy, 2),
            "frontier_energy_kwh": round(frontier, 2),
            "mismatch_ratio": round(ratio, 4),
            "window_minutes": window,
        }
        if ratio > params["high_ratio"]:
            return [RuleOutcome(status="firing", severity=Severity.HIGH, evidence=evidence)]
        if ratio > params["alert_ratio"]:
            return [RuleOutcome(status="firing", severity=Severity.MEDIUM, evidence=evidence)]
        return [RuleOutcome(status="ok")]