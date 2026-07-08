from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.engine import evaluate_project
from apps.alarms.models import Alarm
from apps.alarms.rules.communication import WeatherCommLost
from apps.plants.models import Project
from integrations.solarview.exceptions import SolarViewNotAssociated, SolarViewTimeout
from integrations.solarview.schemas import WeatherSeries

NOW = datetime(2026, 7, 8, 12, 0)


def weather_with_last_point(minutes_ago: int) -> WeatherSeries:
    ts = NOW - timedelta(minutes=minutes_ago)
    return WeatherSeries(
        irradiation={ts: 800.0},
        irradiation_poa={ts - timedelta(minutes=1): 820.0, ts: 850.0},
        temperature={ts: 28.5},
        temperature_poa={},
        wind_speed={},
    )


@pytest.fixture
def project(db):
    return Project.objects.create(external_id=146, name="El Son", synced_at=timezone.now())


def make_ctx(project, weather):
    client = MagicMock()
    if isinstance(weather, Exception):
        client.project_weather.side_effect = weather
    else:
        client.project_weather.return_value = weather
    return EvaluationContext(project=project, client=client, now=NOW)


@pytest.mark.django_db
class TestWeatherCommLost:
    def test_fresh_data_is_ok(self, project):
        # último dato hace 3 min < stale(5) + lag(5)
        outcomes = WeatherCommLost().evaluate(make_ctx(project, weather_with_last_point(3)))

        assert len(outcomes) == 1
        assert outcomes[0].status == "ok"

    def test_stale_data_fires_with_evidence(self, project):
        outcomes = WeatherCommLost().evaluate(make_ctx(project, weather_with_last_point(45)))

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["age_minutes"] == 45
        assert outcomes[0].evidence["last_data_at"] == "2026-07-08 11:15:00"

    def test_station_with_no_data_today_fires(self, project):
        empty = WeatherSeries(
            irradiation={}, irradiation_poa={}, temperature={}, temperature_poa={},
            wind_speed={},
        )

        outcomes = WeatherCommLost().evaluate(make_ctx(project, empty))

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["last_data_at"] is None

    def test_project_without_station_produces_no_outcomes(self, project):
        outcomes = WeatherCommLost().evaluate(
            make_ctx(project, SolarViewNotAssociated("No existe estación"))
        )

        assert outcomes == []

    def test_api_error_is_not_computable(self, project):
        outcomes = WeatherCommLost().evaluate(make_ctx(project, SolarViewTimeout("slow")))

        assert outcomes[0].status == "not_computable"


@pytest.mark.django_db
class TestFullCycleThroughEngine:
    """El piloto de verdad: la regla real corriendo dentro del engine real."""

    def _run(self, project, weather):
        client = MagicMock()
        client.project_weather.return_value = weather
        return evaluate_project(project, client=client, now=NOW)

    def test_open_dedup_and_autoresolve(self, project):
        # tick 1: estación caída → abre alarma
        self._run(project, weather_with_last_point(45))
        alarm = Alarm.objects.get(rule__code="weather_comm_lost")
        assert alarm.status == Alarm.Status.ACTIVE

        # tick 2: sigue caída → misma alarma, occurrence_count sube
        self._run(project, weather_with_last_point(50))
        alarm.refresh_from_db()
        assert alarm.occurrence_count == 2
        assert Alarm.objects.count() == 1

        # tick 3: vuelve el dato → resuelta de inmediato
        self._run(project, weather_with_last_point(2))
        alarm.refresh_from_db()
        assert alarm.status == Alarm.Status.RESOLVED
        assert alarm.resolution_type == Alarm.ResolutionType.AUTO
