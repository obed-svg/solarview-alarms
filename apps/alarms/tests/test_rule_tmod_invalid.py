from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.rules.data_quality import TmodInvalid
from apps.plants.models import Project
from integrations.solarview.exceptions import SolarViewNotAssociated
from integrations.solarview.schemas import WeatherSeries

NOW = datetime(2026, 7, 8, 12, 0)
NIGHT = datetime(2026, 7, 8, 2, 0)


def series(value_fn, minutes=60, step=1):
    return {NOW - timedelta(minutes=m): value_fn(m) for m in range(0, minutes + 1, step)}


def weather(tmod_fn=None, tamb_fn=None, poa_fn=None):
    return WeatherSeries(
        irradiation={},
        irradiation_poa=series(poa_fn or (lambda m: 800 + m)),
        temperature=series(tamb_fn or (lambda m: 30 + m * 0.01)),
        temperature_poa=series(tmod_fn) if tmod_fn else {},
        wind_speed={},
    )


@pytest.fixture
def project(db):
    return Project.objects.create(external_id=146, name="El Son", synced_at=timezone.now())


def make_ctx(project, weather_value, now=NOW):
    client = MagicMock()
    if isinstance(weather_value, Exception):
        client.project_weather.side_effect = weather_value
    else:
        client.project_weather.return_value = weather_value
    return EvaluationContext(project=project, client=client, now=now)


@pytest.mark.django_db
class TestTmodInvalid:
    def test_healthy_tmod_is_ok(self, project):
        # panel a ~50°C variando, ambiente 30, POA alta: coherente
        ctx = make_ctx(project, weather(tmod_fn=lambda m: 50 + m * 0.1))

        assert TmodInvalid().evaluate(ctx)[0].status == "ok"

    def test_missing_tmod_with_station_fires(self, project):
        # la estación reporta otras variables pero T_mod viene vacía
        ctx = make_ctx(project, weather(tmod_fn=None))

        outcomes = TmodInvalid().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["issue"] == "missing"

    def test_frozen_tmod_fires(self, project):
        ctx = make_ctx(project, weather(tmod_fn=lambda m: 47.3))

        outcomes = TmodInvalid().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["issue"] == "frozen"

    def test_out_of_range_fires(self, project):
        ctx = make_ctx(project, weather(tmod_fn=lambda m: 132.0 + m))

        outcomes = TmodInvalid().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["issue"] == "out_of_range"

    def test_incoherent_with_ambient_fires(self, project):
        # POA alta pero panel 15°C bajo el ambiente: sensor malo
        ctx = make_ctx(project, weather(tmod_fn=lambda m: 15 + m * 0.01,
                                        tamb_fn=lambda m: 30 + m * 0.01))

        outcomes = TmodInvalid().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["issue"] == "incoherent_vs_ambient"

    def test_night_is_ok(self, project):
        night_weather = WeatherSeries(
            irradiation={}, irradiation_poa={},
            temperature={NIGHT - timedelta(minutes=m): 22.0 for m in range(60)},
            temperature_poa={NIGHT - timedelta(minutes=m): 21.5 for m in range(60)},
            wind_speed={},
        )
        ctx = make_ctx(project, night_weather, now=NIGHT)

        assert TmodInvalid().evaluate(ctx)[0].status == "ok"

    def test_no_station_does_not_apply(self, project):
        ctx = make_ctx(project, SolarViewNotAssociated("No existe estación"))

        assert TmodInvalid().evaluate(ctx) == []
