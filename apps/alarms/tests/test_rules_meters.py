from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.models import Severity
from apps.alarms.rules.meters import MeterInverterMismatch, MeterNoIncrement
from apps.plants.models import Project
from integrations.solarview.exceptions import SolarViewAPIError, SolarViewNotAssociated
from integrations.solarview.schemas import GenerationSummary, PowerSeries, WeatherSeries

NOW = datetime(2026, 7, 8, 12, 0)


def quoia_series(intervals_kwh):
    """Energía de frontera POR INTERVALO ({ts: {value, unit}}, forma real
    validada 2026-07-08): un punto cada 10 min hacia atrás desde NOW."""
    points = {}
    for i, kwh in enumerate(reversed(intervals_kwh)):
        ts = NOW - timedelta(minutes=10 * i)
        points[ts.strftime("%Y-%m-%d %H:%M:%S")] = {"value": float(kwh), "unit": "kWh"}
    return points


def generation(hourly_kwh):
    return GenerationSummary(
        project_id=146, total_kwh=sum(hourly_kwh),
        hourly={NOW - timedelta(hours=i): v for i, v in enumerate(reversed(hourly_kwh))},
    )


@pytest.fixture
def project(db):
    return Project.objects.create(
        external_id=146, name="El Son", installed_capacity_kw=1000, synced_at=timezone.now()
    )


def make_ctx(project, quoia=None, gen=None, poa_value=850.0):
    client = MagicMock()
    if isinstance(quoia, Exception):
        client.quoia_history.side_effect = quoia
    else:
        client.quoia_history.return_value = quoia or {}
    client.generation.return_value = gen or generation([50.0, 52.0])
    client.project_weather.return_value = WeatherSeries(
        irradiation={},
        irradiation_poa={NOW - timedelta(minutes=m): poa_value for m in range(0, 66)},
        temperature={}, temperature_poa={}, wind_speed={},
    )
    client.project_power.return_value = PowerSeries(unit="kW", power={}, irradiance={})
    return EvaluationContext(project=project, client=client, now=NOW)


@pytest.mark.django_db
class TestMeterNoIncrement:
    def test_energy_flowing_is_ok(self, project):
        ctx = make_ctx(project, quoia=quoia_series([8, 8, 9, 8, 8, 8]))

        assert MeterNoIncrement().evaluate(ctx)[0].status == "ok"

    def test_zero_energy_with_generation_fires(self, project):
        ctx = make_ctx(project, quoia=quoia_series([0, 0, 0, 0, 0, 0]),
                       gen=generation([50.0, 52.0]))

        outcomes = MeterNoIncrement().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["frontier_energy_kwh"] == 0

    def test_zero_energy_without_generation_is_ok(self, project):
        # la planta realmente no generó: no es falla del medidor
        ctx = make_ctx(project, quoia=quoia_series([0, 0, 0, 0, 0, 0]),
                       gen=generation([0.0, 0.0]))

        assert MeterNoIncrement().evaluate(ctx)[0].status == "ok"

    def test_low_poa_is_ok(self, project):
        ctx = make_ctx(project, quoia=quoia_series([0, 0, 0, 0, 0, 0]), poa_value=50.0)

        outcomes = MeterNoIncrement().evaluate(ctx)

        assert outcomes[0].status == "ok"
        assert outcomes[0].reason == "excluded:low_irradiance"

    def test_sparse_window_is_not_computable(self, project):
        # 2 puntos pegados al borde de la ventana: abarcan < media ventana →
        # sumar a ciegas subestimaría y daría falso "cero energía"
        sparse = {
            (NOW - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S"): {
                "value": 8.0, "unit": "kWh",
            }
            for m in (0, 10)
        }
        ctx = make_ctx(project, quoia=sparse)

        outcomes = MeterNoIncrement().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "quoia:ventana_insuficiente"

    def test_no_quoia_meter_does_not_apply(self, project):
        ctx = make_ctx(project, quoia=SolarViewNotAssociated("sin medidor"))

        assert MeterNoIncrement().evaluate(ctx) == []

    def test_never_connected_meter_does_not_apply(self, project):
        # caso real T29: histórico crashea (updated_node) pero el live revela
        # que el proyecto no tiene nodos quoia en Manager → la regla no aplica
        ctx = make_ctx(project, quoia=SolarViewAPIError("updated_node"))
        ctx.client.quoia_live.side_effect = SolarViewNotAssociated("No se encontraron nodos")

        assert MeterNoIncrement().evaluate(ctx) == []

    def test_quoia_broken_is_not_computable(self, project):
        ctx = make_ctx(project, quoia=SolarViewAPIError("500"))

        assert MeterNoIncrement().evaluate(ctx)[0].status == "not_computable"

    def test_meter_silent_is_not_computable(self, project):
        # medidor mudo (T34): la regla 8 alarma; 9/10 no pueden computar energía
        ctx = make_ctx(project, quoia=SolarViewAPIError("updated_node"))
        ctx.client.quoia_live.side_effect = SolarViewAPIError("-1")

        assert MeterNoIncrement().evaluate(ctx)[0].status == "not_computable"


@pytest.mark.django_db
class TestMeterInverterMismatch:
    # quoia_series con 6 puntos cada 10 min abarca 50 min (≥ media ventana de 60)
    # → energía de frontera = SUMA de los 6 intervalos.
    # La ventana horaria de generación solo incluye el punto de NOW (50 kWh).

    def test_small_difference_is_ok(self, project):
        # frontera 49.5 vs inversores 50 → 1%
        ctx = make_ctx(project, quoia=quoia_series([8.25] * 6), gen=generation([48, 50.0]))

        assert MeterInverterMismatch().evaluate(ctx)[0].status == "ok"

    def test_above_5pct_fires_high(self, project):
        # frontera 45 vs inversores 50 → 10%
        ctx = make_ctx(project, quoia=quoia_series([7.5] * 6), gen=generation([48, 50.0]))

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].severity == Severity.HIGH

    def test_between_3_and_5pct_fires_medium(self, project):
        # frontera 48 vs inversores 50 → 4%
        ctx = make_ctx(project, quoia=quoia_series([8.0] * 6), gen=generation([48, 50.0]))

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].severity == Severity.MEDIUM

    def test_no_inverter_energy_is_not_computable(self, project):
        ctx = make_ctx(project, quoia=quoia_series([8.0] * 6), gen=generation([0.0, 0.0]))

        assert MeterInverterMismatch().evaluate(ctx)[0].status == "not_computable"
