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
from .helpers import poa_sustained_above

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
    """Regla 15: POA inválida o congelada. Solo con la ventana de evaluación
    COMPLETAMENTE diurna (T39): al amanecer la ventana de 45 min aún contiene
    oscuridad y dispara falsos — "frozen 0.0" (cierto de noche), "negative"
    (offset nocturno del piranómetro, visto -1.0 W/m²) y "zero_with_generation"
    (generación difusa cruza los 5 kW antes de que el sensor registre). De
    noche not_computable: congela abiertas sin resolver en falso."""

    code = "poa_invalid"
    phase = 2

    def evaluate(self, ctx) -> list[RuleOutcome]:
        params_gate = ctx.params(self.code)
        if not ctx.is_solar_hours(
            margin_minutes=params_gate.get("solar_margin_minutes", 60)
        ):
            return [RuleOutcome(status="not_computable", reason="excluded:night")]

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
        params = ctx.params(self.code)
        # ventana completamente diurna (T39): al amanecer los 45 min de ventana
        # aún contienen oscuridad — temperatura/power constantes son normales
        # (visto: temperature "frozen" en -1.0 = offset nocturno del sensor)
        if not ctx.is_solar_hours(
            margin_minutes=params.get("solar_margin_minutes", 60)
        ):
            return [
                RuleOutcome(status="not_computable", dedup_suffix="signal:power",
                            reason="excluded:night"),
            ]

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
class TmodInvalid(BaseRule):
    """Regla 16: temperatura de módulo inválida. T_mod = `temperature_POA` del
    endpoint weather (confirmado por el usuario 2026-07-08: temperatura del panel).

    Dispara si, en horario solar y con estación presente: la serie viene vacía,
    está congelada, sale del rango físico, o es incoherente con el ambiente
    (panel mucho más frío que el aire con POA alta = sensor malo).
    """

    code = "tmod_invalid"
    phase = 2

    def evaluate(self, ctx) -> list[RuleOutcome]:
        weather = ctx.weather()
        if isinstance(weather, Unavailable):
            if weather.reason == "not_associated":
                return []  # sin estación no hay sensor de panel: la regla no aplica
            return [RuleOutcome(status="not_computable", reason=weather.reason)]

        params = ctx.params(self.code)
        # ventana completamente diurna (T39), ver PoaInvalid
        if not ctx.is_solar_hours(
            margin_minutes=params.get("solar_margin_minutes", 60)
        ):
            return [RuleOutcome(status="not_computable", reason="excluded:night")]

        window_minutes = params["frozen_intervals"] * INTERVAL_MINUTES

        tmod = [
            v for v in ctx.series_window(weather.temperature_poa, window_minutes).values()
            if v is not None
        ]
        if len(tmod) < MIN_POINTS:
            return [
                RuleOutcome(
                    status="firing",
                    evidence={"issue": "missing", "points_in_window": len(tmod)},
                )
            ]

        if min(tmod) < params["tmod_min_c"] or max(tmod) > params["tmod_max_c"]:
            return [
                RuleOutcome(
                    status="firing",
                    evidence={
                        "issue": "out_of_range",
                        "min_c": min(tmod), "max_c": max(tmod),
                        "valid_range_c": [params["tmod_min_c"], params["tmod_max_c"]],
                    },
                )
            ]

        if _is_frozen(tmod):
            return [
                RuleOutcome(
                    status="firing",
                    evidence={"issue": "frozen", "value_c": tmod[0],
                              "window_minutes": window_minutes},
                )
            ]

        poa = [
            v for v in ctx.series_window(weather.irradiation_poa, window_minutes).values()
            if v is not None
        ]
        tamb = [
            v for v in ctx.series_window(weather.temperature, window_minutes).values()
            if v is not None
        ]
        if poa and tamb and min(poa) > params["poa_for_coherence_wm2"]:
            avg_tmod = sum(tmod) / len(tmod)
            avg_tamb = sum(tamb) / len(tamb)
            if avg_tmod < avg_tamb - params["coherence_margin_c"]:
                return [
                    RuleOutcome(
                        status="firing",
                        evidence={
                            "issue": "incoherent_vs_ambient",
                            "avg_tmod_c": round(avg_tmod, 1),
                            "avg_ambient_c": round(avg_tamb, 1),
                            "poa_min_wm2": round(min(poa)),
                        },
                    )
                ]

        return [RuleOutcome(status="ok")]


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

        # la ventana de 60 min debe ser COMPLETAMENTE diurna (con margen):
        # evita el flap diario cuando el tick hourly cae en el borde del ocaso
        params = ctx.params(self.code)
        margin = params.get("solar_margin_minutes", 30)
        window_start = ctx.now - timedelta(minutes=60)
        if not (
            ctx.is_solar_hours(margin_minutes=margin)
            and ctx.is_solar_hours(at=window_start, margin_minutes=margin)
        ):
            return [RuleOutcome(status="ok", reason="excluded:solar_margin")]

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

        # T_mod (temperature_POA) solo exigible si el proyecto tiene estación
        weather = ctx.weather()
        if not isinstance(weather, Unavailable):
            if not ctx.series_window(weather.temperature_poa, 60):
                missing.append("t_mod")

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
        # mismo margen solar que inverter_comm_lost: inversores dormidos al
        # anochecer no son "datos insuficientes"
        params_comm = ctx.params("inverter_comm_lost")
        if not ctx.is_solar_hours(
            margin_minutes=params_comm.get("solar_margin_minutes", 45)
        ):
            return []

        # T40: mismo gate físico por POA que la regla 4 — los inversores
        # arrancan por irradiancia, no por reloj (ola matinal de 88 falsas).
        # .get con defaults: DBs sin la migración 0010 no deben explotar.
        poa_ok = poa_sustained_above(ctx, {
            "poa_min_wm2": params_comm.get("poa_min_wm2", 100),
            "persistence_minutes": params_comm.get("persistence_minutes", 15),
            "data_lag_minutes": params_comm.get("data_lag_minutes", 5),
        })
        if poa_ok is None:
            return [RuleOutcome(status="not_computable", reason="poa:no_verificable")]
        if not poa_ok:
            return [RuleOutcome(status="ok", reason="excluded:low_irradiance")]

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
