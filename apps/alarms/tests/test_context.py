from datetime import datetime
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext, Unavailable
from apps.plants.models import MaintenanceWindow, Project
from integrations.solarview.exceptions import SolarViewAPIError, SolarViewNotAssociated
from integrations.solarview.schemas import WeatherSeries

NOW = datetime(2026, 7, 8, 12, 0)  # mediodía local


def make_project(**kwargs):
    defaults = {"external_id": 146, "name": "El Son", "synced_at": timezone.now()}
    defaults.update(kwargs)
    return Project.objects.create(**defaults)


def make_ctx(project=None, client=None, now=NOW):
    return EvaluationContext(
        project=project or make_project(), client=client or MagicMock(), now=now
    )


@pytest.mark.django_db
class TestSeriesWindow:
    def test_filters_by_window_with_lag(self):
        ctx = make_ctx()
        series = {
            datetime(2026, 7, 8, 11, 39): 1.0,  # fuera (antes del inicio)
            datetime(2026, 7, 8, 11, 45): 2.0,  # dentro
            datetime(2026, 7, 8, 11, 55): 3.0,  # dentro (== end)
            datetime(2026, 7, 8, 11, 58): 4.0,  # fuera (dentro del lag)
        }

        result = ctx.series_window(series, minutes=15, lag_minutes=5)

        # ventana = [11:40, 11:55]
        assert result == {datetime(2026, 7, 8, 11, 45): 2.0, datetime(2026, 7, 8, 11, 55): 3.0}


@pytest.mark.django_db
class TestUnavailable:
    def test_not_associated_becomes_unavailable_and_is_cached(self):
        client = MagicMock()
        client.project_weather.side_effect = SolarViewNotAssociated("No existe estación")
        ctx = make_ctx(client=client)

        first = ctx.weather()
        second = ctx.weather()

        assert isinstance(first, Unavailable)
        assert first.reason == "not_associated"
        assert not first  # falsy
        assert second is first
        assert client.project_weather.call_count == 1  # cacheado, no re-consulta

    def test_api_error_becomes_unavailable(self):
        client = MagicMock()
        client.project_inverters.side_effect = SolarViewAPIError("500")
        ctx = make_ctx(client=client)

        assert isinstance(ctx.inverters_live(), Unavailable)


@pytest.mark.django_db
class TestPoaSeries:
    def test_prefers_weather_poa_over_power_irradiance(self):
        client = MagicMock()
        client.project_weather.return_value = WeatherSeries(
            irradiation={}, irradiation_poa={NOW: 850.0}, temperature={},
            temperature_poa={}, wind_speed={},
        )
        ctx = make_ctx(client=client)

        assert ctx.poa_series() == {NOW: 850.0}
        client.project_power.assert_not_called()

    def test_falls_back_to_power_irradiance_without_weather(self):
        client = MagicMock()
        client.project_weather.side_effect = SolarViewNotAssociated("no estación")
        power = MagicMock()
        power.irradiance = {NOW: 500.0}
        client.project_power.return_value = power
        ctx = make_ctx(client=client)

        assert ctx.poa_series() == {NOW: 500.0}


@pytest.mark.django_db
class TestHelpers:
    def test_solar_hours_fallback_without_coordinates(self):
        ctx = make_ctx(now=datetime(2026, 7, 8, 12, 0))
        assert ctx.is_solar_hours() is True

        ctx_night = make_ctx(
            project=Project.objects.create(
                external_id=1, name="x", synced_at=timezone.now()
            ),
            now=datetime(2026, 7, 8, 2, 0),
        )
        assert ctx_night.is_solar_hours() is False

    def test_garbage_coordinates_fall_back_to_fixed_window(self):
        # visto en producción: la API entrega lat/lon INVERTIDAS (lat=-75 = Antártida)
        # y astral lanza ValueError ("Sun never reaches 6 degrees below the horizon")
        project = Project.objects.create(
            external_id=151, name="deprecated", latitude=-75.199598, longitude=10.583292,
            synced_at=timezone.now(),
        )
        ctx = make_ctx(project=project, now=datetime(2026, 7, 8, 12, 0))

        assert ctx.is_solar_hours() is True  # mediodía en ventana fija, sin explotar
        assert ctx.is_solar_hours(margin_minutes=45) is True

    def test_in_maintenance_uses_windows(self):
        project = make_project()
        MaintenanceWindow.objects.create(
            project=project,
            starts_at=timezone.make_aware(datetime(2026, 7, 8, 11, 0)),
            ends_at=timezone.make_aware(datetime(2026, 7, 8, 13, 0)),
        )
        ctx = make_ctx(project=project)

        assert ctx.in_maintenance() is True

    def test_firing_flags(self):
        ctx = make_ctx()
        assert ctx.flag_active("inverter_comm_lost", "inv:5") is False

        ctx.set_firing("inverter_comm_lost", "inv:5")

        assert ctx.flag_active("inverter_comm_lost", "inv:5") is True
        assert ctx.flag_active("inverter_comm_lost") is True  # sin sufijo: ¿alguna?
        assert ctx.flag_active("inverter_comm_lost", "inv:9") is False
