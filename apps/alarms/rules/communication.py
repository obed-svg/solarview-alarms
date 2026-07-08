"""Fase 1 — reglas de comunicación: detectan fuentes de datos caídas.

Corren primero porque sus resultados excluyen a las reglas eléctricas
("no clasificar como falla del inversor si hay comunicación caída").
"""

from apps.alarms.context import Unavailable

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
