"""Fase 3 — reglas de medidor de frontera (quoia).

Payload real VALIDADO (2026-07-08, proyecto 108 y 25 más): {ts: {value, unit}}
donde `value` es la energía kWh DEL INTERVALO (~15 min, cadencia variable por
proyecto), NO un contador acumulado — 0.0 de noche, sigue la curva solar.
El endpoint devuelve las últimas ~24 h y NO acepta parámetros de fecha (ver
client.quoia_history). En proyectos cuyo quoia sigue roto server-side estas
reglas viven en not_computable (comportamiento correcto, sin ruido).
"""

from datetime import timedelta

from apps.alarms.context import Unavailable
from apps.alarms.models import Severity
from integrations.solarview.schemas import parse_ts

from .base import BaseRule, RuleOutcome, register
from .helpers import poa_sustained_above

MIN_INTERVAL_POINTS = 2


def _frontier_energy_kwh(ctx, quoia_raw: dict, window_minutes: int) -> float | None:
    """Energía de frontera en la ventana: SUMA de los intervalos quoia dentro
    de ella. None si la cobertura es insuficiente — menos de
    MIN_INTERVAL_POINTS puntos o puntos que abarcan menos de media ventana
    (sumar con huecos grandes subestima y produciría falsos "cero energía")."""
    cutoff = ctx.now - timedelta(minutes=window_minutes)
    points = []
    for key, payload in quoia_raw.items():
        ts = parse_ts(key)
        if ts is not None and ts >= cutoff and isinstance(payload, dict):
            if payload.get("value") is not None:
                points.append((ts, payload["value"]))
    if len(points) < MIN_INTERVAL_POINTS:
        return None
    points.sort()
    span_minutes = (points[-1][0] - points[0][0]).total_seconds() / 60
    if span_minutes < window_minutes / 2:
        return None
    return sum(v for _, v in points)


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
    """Regla 9: la frontera no registra energía aunque los inversores generan
    y hay POA. Sin medidor quoia la regla no aplica.

    NO aplica en autoconsumo (decisión del usuario 2026-07-08, T35): la
    energía se consume localmente, la frontera puede legítimamente no
    incrementar mientras los inversores generan."""

    code = "meter_no_increment"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        if ctx.project.is_self_consumption:
            return []

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

        frontier_energy = _frontier_energy_kwh(ctx, quoia, window)
        if frontier_energy is None:
            return [RuleOutcome(status="not_computable", reason="quoia:ventana_insuficiente")]

        inverter_energy = _inverter_energy_kwh(ctx, window)
        if inverter_energy is None:
            return [
                RuleOutcome(status="not_computable", reason="generation:no_disponible")
            ]

        # piso de energía (T41): al alba la generación de la ventana es <1 kWh
        # y cualquier diferencia dispara falsos — exigir energía significativa
        min_energy = params.get("min_window_energy_kwh", 10)
        if (
            frontier_energy <= params["delta_zero_kwh"]
            and inverter_energy >= min_energy
        ):
            return [
                RuleOutcome(
                    status="firing",
                    evidence={
                        "frontier_energy_kwh": round(frontier_energy, 2),
                        "inverter_energy_kwh": round(inverter_energy, 2),
                        "window_minutes": window,
                    },
                )
            ]
        return [RuleOutcome(status="ok")]


@register
class MeterInverterMismatch(BaseRule):
    """Regla 10: |E_inv - E_frontera| / E_inv en ventana horaria.
    >5% → high; 3-5% → medium (escala si empeora, el engine no baja severidad).

    NO aplica en autoconsumo (decisión del usuario 2026-07-08, T35): el
    mismatch inversores-vs-frontera es estructural cuando la energía se
    consume localmente."""

    code = "meter_inverter_mismatch"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        if ctx.project.is_self_consumption:
            return []

        quoia = ctx.quoia()
        if isinstance(quoia, Unavailable):
            if quoia.reason == "not_associated":
                return []
            return [RuleOutcome(status="not_computable", reason=f"quoia:{quoia.reason}")]

        params = ctx.params(self.code)
        window = params["window_minutes"]

        frontier = _frontier_energy_kwh(ctx, quoia, window)
        if frontier is None:
            return [RuleOutcome(status="not_computable", reason="quoia:ventana_insuficiente")]

        inverter_energy = _inverter_energy_kwh(ctx, window)
        if not inverter_energy:  # None o 0: sin denominador no hay ratio
            return [
                RuleOutcome(status="not_computable", reason="generation:sin_energia_inv")
            ]
        # piso de energía (T41, visto al alba real: 0.19-0.94 kWh en ventana →
        # ratios de 75-100% con diferencias de centésimas de kWh, sin sentido)
        min_energy = params.get("min_window_energy_kwh", 10)
        if inverter_energy < min_energy:
            return [
                RuleOutcome(
                    status="not_computable",
                    reason=f"energia_ventana_insuficiente (<{min_energy} kWh)",
                )
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