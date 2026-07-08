"""Fase 2 — calidad de datos: validan que los insumos sean confiables ANTES de
que las reglas eléctricas los usen, y marcan intervalos no calculables.

Interpretación de "3 intervalos consecutivos" (Excel/COX): intervalos de registro
IEC de 15 min → ventana de frozen_intervals × 15 min sin variación.
"""

from datetime import timedelta

from apps.alarms.context import Unavailable
from apps.alarms.models import NonComputableInterval
from integrations.solarview.schemas import parse_ts

from .base import BaseRule, RuleOutcome, register

INTERVAL_MINUTES = 15  # intervalo de registro IEC 61724
MIN_POINTS = 3
GENERATION_THRESHOLD_KW = 5.0  # "generación real" para validar POA=0
FROZEN_EPSILON = 1e-6


def _mark_non_computable(ctx, metric: str, missing: list[str], inverter=None,
                         floor_minutes: int = 60) -> None:
    """Registra el intervalo no calculable (idempotente por constraint + floor)."""
    floored = ctx.now.replace(second=0, microsecond=0)
    floored = floored.replace(minute=(floored.minute // floor_minutes) * floor_minutes
                              if floor_minutes < 60 else 0)
    start = floored.replace(tzinfo=ctx.tz)
    NonComputableInterval.objects.get_or_create(
        project=ctx.project, inverter=inverter, metric=metric, interval_start=start,
        defaults={
            "interval_end": start + timedelta(minutes=floor_minutes),
            "missing_inputs": missing,
        },
    )


def _is_frozen(values: list[float]) -> bool:
    return (
        len(values) >= MIN_POINTS
        and max(values) - min(values) < FROZEN_EPSILON
    )


@register
class PoaInvalid(BaseRule):
    """Regla 15: POA inválida o congelada. Solo en horario solar."""

    code = "poa_invalid"
    phase = 2

    def evaluate(self, ctx) -> list[RuleOutcome]:
        if not ctx.is_solar_hours():
            return [RuleOutcome(status="ok", reason="excluded:night")]

        poa = ctx.poa_series()
        if isinstance(poa, Unavailable):
            return [RuleOutcome(status="not_computable", reason=f"poa:{poa.reason}")]

        params = ctx.params(self.code)
        window_minutes = params["frozen_intervals"] * INTERVAL_MINUTES
        window = ctx.series_window(poa, window_minutes)
        values = [v for v in window.values() if v is not None]
        if len(values) < MIN_POINTS:
            return [RuleOutcome(status="not_computable", reason="poa:window_insuficiente")]

        if min(values) < 0:
            return [
                RuleOutcome(status="firing",
                            evidence={"issue": "negative", "min_poa": min(values)})
            ]

        power = ctx.power()
        if not isinstance(power, Unavailable):
            power_values = [
                v for v in ctx.series_window(power.power, window_minutes).values()
                if v is not None
            ]
            if (
                max(values) <= FROZEN_EPSILON
                and power_values
                and max(power_values) > GENERATION_THRESHOLD_KW
            ):
                return [
                    RuleOutcome(
                        status="firing",
                        evidence={
                            "issue": "zero_with_generation",
                            "power_max_kw": max(power_values),
                        },
                    )
                ]

        if _is_frozen(values):
            return [
                RuleOutcome(
                    status="firing",
                    evidence={
                        "issue": "frozen",
                        "value": values[0],
                        "window_minutes": window_minutes,
                        "points": len(values),
                    },
                )
            ]
        return [RuleOutcome(status="ok")]


@register
class DataFrozen(BaseRule):
    """Regla 13: señales dinámicas congeladas (potencia; temperatura si hay
    estación). POA congelada la cubre poa_invalid — no se duplica aquí."""

    code = "data_frozen"
    phase = 2

    def evaluate(self, ctx) -> list[RuleOutcome]:
        if not ctx.is_solar_hours():
            return [
                RuleOutcome(status="ok", dedup_suffix="signal:power",
                            reason="excluded:night"),
            ]

        params = ctx.params(self.code)
        window_minutes = params["frozen_intervals"] * INTERVAL_MINUTES
        outcomes = []

        power = ctx.power()
        if isinstance(power, Unavailable):
            outcomes.append(
                RuleOutcome(status="not_computable", dedup_suffix="signal:power",
                            reason=f"power:{power.reason}")
            )
        else:
            values = [
                v for v in ctx.series_window(power.power, window_minutes).values()
                if v is not None
            ]
            # 0 constante no cuenta: potencia 0 con POA la cubre project_no_generation
            if _is_frozen(values) and abs(values[0]) > FROZEN_EPSILON:
                outcomes.append(
                    RuleOutcome(
                        status="firing", dedup_suffix="signal:power",
                        evidence={"signal": "power", "value": values[0],
                                  "window_minutes": window_minutes},
                    )
                )
            else:
                outcomes.append(RuleOutcome(status="ok", dedup_suffix="signal:power"))

        weather = ctx.weather()
        if not isinstance(weather, Unavailable) and weather.temperature:
            values = [
                v for v in ctx.series_window(weather.temperature, window_minutes).values()
                if v is not None
            ]
            if _is_frozen(values):
                outcomes.append(
                    RuleOutcome(
                        status="firing", dedup_suffix="signal:temperature",
                        evidence={"signal": "temperature", "value": values[0],
                                  "window_minutes": window_minutes},
                    )
                )
            else:
                outcomes.append(RuleOutcome(status="ok", dedup_suffix="signal:temperature"))

        return outcomes


@register
class PrInputsMissing(BaseRule):
    """Regla 11: insumos de PR incompletos en la última hora (energía AC de
    frontera, POA, P_DC). T_mod excluido (dato incierto). Marca
    NonComputableInterval(pr). Sin medidor de frontera la regla no aplica:
    ese proyecto no calcula PR contractual."""

    code = "pr_inputs_missing"
    phase = 2

    def evaluate(self, ctx) -> list[RuleOutcome]:
        if not ctx.is_solar_hours():
            return [RuleOutcome(status="ok", reason="excluded:night")]

        quoia = ctx.quoia()
        if isinstance(quoia, Unavailable) and quoia.reason == "not_associated":
            return []

        missing = []

        if isinstance(quoia, Unavailable):
            missing.append("energia_ac")
        else:
            timestamps = [ts for ts in (parse_ts(k) for k in quoia) if ts]
            fresh = [ts for ts in timestamps if (ctx.now - ts) <= timedelta(minutes=60)]
            if not fresh:
                missing.append("energia_ac")

        poa = ctx.poa_series()
        if isinstance(poa, Unavailable) or not ctx.series_window(poa, 60):
            missing.append("poa")

        dc = ctx.string_currents()
        if isinstance(dc, Unavailable):
            missing.append("p_dc")
        else:
            has_fresh_dc = any(
                ctx.series_window(series, 60)
                for variables in dc.values()
                for series in variables.values()
            )
            if not has_fresh_dc:
                missing.append("p_dc")

        if missing:
            _mark_non_computable(ctx, "pr", missing, floor_minutes=60)
            return [
                RuleOutcome(status="firing", evidence={"missing_inputs": missing})
            ]
        return [RuleOutcome(status="ok")]


@register
class AvailabilityInputsMissing(BaseRule):
    """Regla 12: insumos de disponibilidad incompletos POR inversor (POA válida,
    potencia/estado/timestamp del inversor). Nunca inventa 0%/100%: marca
    NonComputableInterval(availability, inverter)."""

    code = "availability_inputs_missing"
    phase = 2

    def evaluate(self, ctx) -> list[RuleOutcome]:
        if not ctx.is_solar_hours():
            return [RuleOutcome(status="ok", reason="excluded:night")]

        inverters = ctx.inverters_live()
        if isinstance(inverters, Unavailable):
            return [RuleOutcome(status="not_computable", reason=inverters.reason)]

        poa = ctx.poa_series()
        poa_missing = isinstance(poa, Unavailable) or not ctx.series_window(poa, 30)

        params = ctx.params("inverter_comm_lost")
        stale_threshold = params["stale_minutes"] + params.get("data_lag_minutes", 0)

        outcomes = []
        for inv in inverters:
            missing = []
            if poa_missing:
                missing.append("poa")
            if inv.time is None or (ctx.now - inv.time) > timedelta(minutes=stale_threshold):
                missing.append("timestamp")
            if inv.power is None:
                missing.append("power")
            if not inv.state:
                missing.append("state")

            suffix = f"inv:{inv.id}"
            if missing:
                _mark_non_computable(
                    ctx, "availability", missing,
                    inverter=ctx.inverter_model(inv.id), floor_minutes=15,
                )
                outcomes.append(
                    RuleOutcome(
                        status="firing", dedup_suffix=suffix,
                        inverter_external_id=inv.id,
                        evidence={"dev_name": inv.dev_name, "missing_inputs": missing},
                    )
                )
            else:
                outcomes.append(
                    RuleOutcome(status="ok", dedup_suffix=suffix,
                                inverter_external_id=inv.id)
                )
        return outcomes
