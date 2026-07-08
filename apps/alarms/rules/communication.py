"""Fase 1 — reglas de comunicación: detectan fuentes de datos caídas.

Corren primero porque sus resultados excluyen a las reglas eléctricas
("no clasificar como falla del inversor si hay comunicación caída").
"""

from apps.alarms.context import Unavailable
from integrations.solarview.schemas import parse_ts

from .base import BaseRule, RuleOutcome, register


@register
class WeatherCommLost(BaseRule):
    """Regla 14: estación meteorológica sin comunicación.

    Solo aplica a proyectos CON estación (los 404 "no existe estación" no
    generan outcome). Umbral: stale_minutes + data_lag_minutes sobre el
    timestamp más reciente de cualquier serie meteo.
    """

    code = "weather_comm_lost"
    phase = 1

    def evaluate(self, ctx) -> list[RuleOutcome]:
        weather = ctx.weather()
        if isinstance(weather, Unavailable):
            if weather.reason == "not_associated":
                return []  # el proyecto no tiene estación: la regla no aplica
            return [RuleOutcome(status="not_computable", reason=weather.reason)]

        params = ctx.params(self.code)
        threshold_minutes = params["stale_minutes"] + params.get("data_lag_minutes", 0)

        all_timestamps = [
            ts
            for series in (
                weather.irradiation_poa, weather.irradiation,
                weather.temperature, weather.temperature_poa, weather.wind_speed,
            )
            for ts in series
        ]
        last_at = max(all_timestamps, default=None)

        if last_at is None:
            return [
                RuleOutcome(
                    status="firing",
                    evidence={"last_data_at": None, "detail": "estación sin datos hoy"},
                )
            ]

        age_minutes = (ctx.now - last_at).total_seconds() / 60
        if age_minutes > threshold_minutes:
            return [
                RuleOutcome(
                    status="firing",
                    evidence={
                        "last_data_at": str(last_at),
                        "age_minutes": round(age_minutes),
                        "threshold_minutes": threshold_minutes,
                    },
                )
            ]
        return [RuleOutcome(status="ok")]


@register
class InverterCommLost(BaseRule):
    """Regla 4: inversor sin comunicación.

    Un outcome POR inversor (dedup "inv:{external_id}"). Es problema de
    comunicación, no falla eléctrica: las reglas eléctricas de fase 3 se
    excluyen consultando este flag. Inversor sin `time` (nunca reportó) = firing.
    """

    code = "inverter_comm_lost"
    phase = 1

    def evaluate(self, ctx) -> list[RuleOutcome]:
        params = ctx.params(self.code)
        # Fuera de horario solar (con margen) los inversores duermen y no
        # reportar es normal: no evaluar ni tocar alarmas (decisión 2026-07-08,
        # evita la ola nocturna de ~47 falsas al anochecer)
        if not ctx.is_solar_hours(margin_minutes=params.get("solar_margin_minutes", 45)):
            return []

        inverters = ctx.inverters_live()
        if isinstance(inverters, Unavailable):
            return [RuleOutcome(status="not_computable", reason=inverters.reason)]
        threshold = params["stale_minutes"] + params.get("data_lag_minutes", 0)

        outcomes = []
        for inv in inverters:
            suffix = f"inv:{inv.id}"
            if ctx.in_maintenance(inverter=ctx.inverter_model(inv.id)):
                outcomes.append(
                    RuleOutcome(
                        status="ok", dedup_suffix=suffix,
                        inverter_external_id=inv.id, reason="excluded:maintenance",
                    )
                )
                continue

            age_minutes = (
                None if inv.time is None
                else (ctx.now - inv.time).total_seconds() / 60
            )
            if age_minutes is None or age_minutes > threshold:
                outcomes.append(
                    RuleOutcome(
                        status="firing",
                        dedup_suffix=suffix,
                        inverter_external_id=inv.id,
                        evidence={
                            "dev_name": inv.dev_name,
                            "last_data_at": str(inv.time) if inv.time else None,
                            "age_minutes": None if age_minutes is None else round(age_minutes),
                            "threshold_minutes": threshold,
                        },
                    )
                )
            else:
                outcomes.append(
                    RuleOutcome(
                        status="ok", dedup_suffix=suffix, inverter_external_id=inv.id
                    )
                )
        return outcomes


@register
class MeterCommLost(BaseRule):
    """Regla 8: medidor de frontera (quoia) sin comunicación.

    Solo dispara si los INVERSORES sí reportan (si todo está caído no se puede
    culpar al medidor → not_computable). Proyecto sin medidor quoia: no aplica.
    NOTA T03: quoia devuelve 500 en todos los proyectos hoy — mientras siga así
    esta regla vivirá en not_computable, que es el comportamiento correcto.
    """

    code = "meter_comm_lost"
    phase = 1

    def evaluate(self, ctx) -> list[RuleOutcome]:
        quoia = ctx.quoia()
        if isinstance(quoia, Unavailable):
            if quoia.reason == "not_associated":
                return []
            return [RuleOutcome(status="not_computable", reason=f"quoia:{quoia.reason}")]

        params = ctx.params(self.code)
        threshold = params["stale_minutes"]

        timestamps = [ts for ts in (parse_ts(key) for key in quoia) if ts is not None]
        last_at = max(timestamps, default=None)
        age_minutes = (
            None if last_at is None else (ctx.now - last_at).total_seconds() / 60
        )

        if age_minutes is not None and age_minutes <= threshold:
            return [RuleOutcome(status="ok")]

        # medidor viejo o sin datos: confirmar que los inversores SÍ reportan
        inverters = ctx.inverters_live()
        if isinstance(inverters, Unavailable):
            return [RuleOutcome(status="not_computable", reason=inverters.reason)]
        inverter_params = ctx.params("inverter_comm_lost")
        inv_threshold = (
            inverter_params["stale_minutes"] + inverter_params.get("data_lag_minutes", 0)
        )
        any_inverter_live = any(
            inv.time and (ctx.now - inv.time).total_seconds() / 60 <= inv_threshold
            for inv in inverters
        )
        if not any_inverter_live:
            return [
                RuleOutcome(
                    status="not_computable",
                    reason="inversores tampoco reportan: no se puede aislar el medidor",
                )
            ]

        return [
            RuleOutcome(
                status="firing",
                evidence={
                    "last_data_at": str(last_at) if last_at else None,
                    "age_minutes": None if age_minutes is None else round(age_minutes),
                    "threshold_minutes": threshold,
                    "inverters_reporting": True,
                },
            )
        ]
