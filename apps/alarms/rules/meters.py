"""Fase 3 — reglas de medidor de frontera (quoia).

Payload real VALIDADO (2026-07-08, proyecto 108 y 25 más): {ts: {value, unit}}
donde `value` es la energía kWh DEL INTERVALO (~15 min, cadencia variable por
proyecto), NO un contador acumulado — 0.0 de noche, sigue la curva solar.
El endpoint devuelve las últimas ~24 h y NO acepta parámetros de fecha (ver
client.quoia_history). En proyectos cuyo quoia sigue roto server-side estas
reglas viven en not_computable (comportamiento correcto, sin ruido).

Ventana de comparación (T45): la última hora COMPLETA, nunca ventanas
rodantes. La primera versión comparaba quoia rodante [now-60, now] contra los
buckets HORARIOS de /generation/ con el bucket de la hora en curso PARCIAL:
evaluando a los :06 el bucket llevaba 6 minutos → "mismatch 520%" fabricado
(caso real El Olimpo: medidor 280 vs inversores 274 kWh/h — sanos). Ambas
fuentes se alinean ahora a la misma hora cerrada:
- quoia: intervalos etiquetados al CIERRE (real: "14:00:05" = intervalo que
  termina a las 14:00) → suma sobre (hora_inicio, hora_fin] con tolerancia
  de deriva de 2 min.
- generation: bucket etiquetado al INICIO (real: "13:00" = hora 13→14) →
  el punto exacto de hora_inicio.
"""

from datetime import timedelta

from apps.alarms.context import Unavailable
from apps.alarms.models import Severity
from integrations.solarview.schemas import parse_ts

from .base import BaseRule, RuleOutcome, register

MIN_INTERVAL_POINTS = 2
LABEL_DRIFT = timedelta(minutes=2)  # etiquetas quoia reales: "14:00:05", "15:15:04"


def _last_full_hour(ctx, lag_minutes: int = 5):
    """(inicio, fin) de la última hora completa cuyo cierre ya superó el lag
    de escritura del backend. A las 12:03 con lag 5 la hora 11→12 aún puede
    estar incompleta → se evalúa la 10→11; desde las 12:05, la 11→12."""
    effective = ctx.now - timedelta(minutes=lag_minutes)
    hour_end = effective.replace(minute=0, second=0, microsecond=0)
    return hour_end - timedelta(hours=1), hour_end


EDGE_TOLERANCE = timedelta(minutes=20)  # una cadencia de 15 min + margen


def _frontier_energy_between(quoia_raw: dict, start, end) -> float | None:
    """Suma de los intervalos quoia dentro de (start, end], tolerando la
    deriva de segundos de las etiquetas. None si la cobertura no llega a AMBOS
    bordes de la hora (T48, caso real Joropo 17-18h: su medidor dejó de
    escribir a mitad de hora — patrón nocturno que arranca temprano — y la
    suma coja fabricó un 'déficit' del 72% en una planta sana). Un medidor
    que no cubre la hora completa es not_computable, no un mismatch."""
    lo, hi = start + LABEL_DRIFT, end + LABEL_DRIFT
    points = []
    for key, payload in quoia_raw.items():
        ts = parse_ts(key)
        if ts is not None and lo < ts <= hi and isinstance(payload, dict):
            if payload.get("value") is not None:
                points.append((ts, payload["value"]))
    if len(points) < MIN_INTERVAL_POINTS:
        return None
    points.sort()
    first_ts, last_ts = points[0][0], points[-1][0]
    if first_ts > lo + EDGE_TOLERANCE or last_ts < hi - EDGE_TOLERANCE:
        return None
    return sum(v for _, v in points)


def _inverter_energy_for_hour(ctx, hour_start) -> float | None:
    """Bucket de /generation/ etiquetado exactamente en hour_start (hora ya
    cerrada → energía completa, nunca parcial)."""
    gen = ctx.generation()
    if isinstance(gen, Unavailable):
        return None
    for ts, value in gen.hourly.items():
        if ts == hour_start:
            return value
    return None


@register
class MeterNoIncrement(BaseRule):
    """Regla 9: la frontera no registró energía en la última hora completa
    aunque los inversores sí generaron (bucket ≥ min_window_energy_kwh — el
    propio bucket de generación es la prueba de producción, T45). Sin medidor
    quoia la regla no aplica.

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
        hour_start, hour_end = _last_full_hour(
            ctx, params.get("data_lag_minutes", 5)
        )

        frontier_energy = _frontier_energy_between(quoia, hour_start, hour_end)
        if frontier_energy is None:
            return [RuleOutcome(status="not_computable", reason="quoia:ventana_insuficiente")]

        inverter_energy = _inverter_energy_for_hour(ctx, hour_start)
        if inverter_energy is None:
            return [
                RuleOutcome(status="not_computable", reason="generation:no_disponible")
            ]

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
                        "hour": f"{hour_start:%Y-%m-%d %H:%M}-{hour_end:%H:%M}",
                    },
                )
            ]
        return [RuleOutcome(status="ok")]


@register
class MeterInverterMismatch(BaseRule):
    """Regla 10: |E_inv - E_frontera| / E_inv sobre la última hora COMPLETA
    (T45: ambas fuentes alineadas a la misma hora cerrada — comparar una
    ventana rodante contra un bucket parcial fabricaba mismatch de hasta
    520% en plantas sanas). >5% → high; 3-5% → medium (escala si empeora,
    el engine no baja severidad).

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
        hour_start, hour_end = _last_full_hour(
            ctx, params.get("data_lag_minutes", 5)
        )

        frontier = _frontier_energy_between(quoia, hour_start, hour_end)
        if frontier is None:
            return [RuleOutcome(status="not_computable", reason="quoia:ventana_insuficiente")]

        inverter_energy = _inverter_energy_for_hour(ctx, hour_start)
        if not inverter_energy:  # None o 0: sin denominador no hay ratio
            return [
                RuleOutcome(status="not_computable", reason="generation:sin_energia_inv")
            ]
        # piso de energía (T41): sin energía significativa los ratios no
        # tienen sentido físico (visto: 75-100% con centésimas de kWh)
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
            "hour": f"{hour_start:%Y-%m-%d %H:%M}-{hour_end:%H:%M}",
        }
        if ratio > params["high_ratio"]:
            return [RuleOutcome(status="firing", severity=Severity.HIGH, evidence=evidence)]
        if ratio > params["alert_ratio"]:
            return [RuleOutcome(status="firing", severity=Severity.MEDIUM, evidence=evidence)]
        return [RuleOutcome(status="ok")]
