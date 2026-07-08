import json
from datetime import datetime
from pathlib import Path

import responses

from integrations.solarview.client import SolarViewClient
from integrations.solarview.schemas import (
    AvailabilityDetail,
    GenerationSummary,
    InverterLive,
    PowerSeries,
    ProjectInfo,
    RelayStatus,
    WeatherSeries,
    parse_ts,
)

BASE = "https://api.test"
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def make_client() -> SolarViewClient:
    return SolarViewClient(base_url=BASE, token="t")


class TestParseTs:
    def test_with_seconds(self):
        assert parse_ts("2026-07-08 16:10:04") == datetime(2026, 7, 8, 16, 10, 4)

    def test_without_seconds(self):
        assert parse_ts("2026-07-08 00:05") == datetime(2026, 7, 8, 0, 5)

    def test_invalid_returns_none(self):
        assert parse_ts("no-fecha") is None
        assert parse_ts(None) is None


class TestListProjects:
    @responses.activate
    def test_parses_projects_from_real_fixture(self):
        responses.get(f"{BASE}/monitoring/project/", json=fixture("project_list"))

        projects = make_client().list_projects()

        assert len(projects) > 0
        first = projects[0]
        assert isinstance(first, ProjectInfo)
        assert first.id == 121
        assert first.name == "Laureles Campestre"
        assert isinstance(first.is_minifarm, bool)


class TestProjectInverters:
    @responses.activate
    def test_parses_live_inverters(self):
        responses.get(f"{BASE}/monitoring/project/146/inverter/", json=fixture("inverters_live"))

        inverters = make_client().project_inverters(146)

        assert len(inverters) == 5
        inv = inverters[0]
        assert isinstance(inv, InverterLive)
        assert inv.dev_name == "300KTL-Inversor1"
        assert inv.state == "Grid-connected"
        assert isinstance(inv.time, datetime)


class TestProjectPower:
    @responses.activate
    def test_parses_power_and_irradiance_series(self):
        responses.get(f"{BASE}/monitoring/project/146/power/", json=fixture("power_today"))

        series = make_client().project_power(146)

        assert isinstance(series, PowerSeries)
        assert series.unit == "kW"
        assert len(series.power) > 0
        assert len(series.irradiance) > len(series.power)  # irradiancia ~1min vs power 5min
        ts, value = next(iter(series.power.items()))
        assert isinstance(ts, datetime)
        assert value is None or isinstance(value, int | float)
        assert "total_power=1" in responses.calls[0].request.url


class TestProjectWeather:
    @responses.activate
    def test_parses_poa_and_units(self):
        responses.get(f"{BASE}/monitoring/project/146/weather/", json=fixture("weather_today"))

        weather = make_client().project_weather(
            146, date_from="2026-07-08 00:00:00-05:00", date_to="2026-07-08 23:59:59-05:00"
        )

        assert isinstance(weather, WeatherSeries)
        assert len(weather.irradiation_poa) > 0
        assert weather.units["irradiation_POA"] == "W/m2"
        assert all(isinstance(k, datetime) for k in list(weather.irradiation_poa)[:5])


class TestRelayNow:
    @responses.activate
    def test_active_null_is_preserved_as_none(self):
        responses.get(f"{BASE}/monitoring/project/146/relay/", json=fixture("relay_now"))

        relay = make_client().relay_now(146)

        assert isinstance(relay, RelayStatus)
        assert relay.active is None  # fixture real trae null
        assert relay.f_abc == 59.98
        assert isinstance(relay.time, datetime)


class TestGeneration:
    @responses.activate
    def test_parses_generation_without_envelope(self):
        responses.get(
            f"{BASE}/monitoring/project/146/generation/", json=fixture("generation_today")
        )

        gen = make_client().generation(146, start_date="2026-07-08", end_date="2026-07-08")

        assert isinstance(gen, GenerationSummary)
        assert gen.total_kwh >= 0
        assert all(isinstance(k, datetime) for k in list(gen.hourly)[:5])


class TestMeasurementsDc:
    @responses.activate
    def test_indexed_by_dev_name_with_parsed_timestamps(self):
        responses.get(
            f"{BASE}/monitoring/project/146/measurements-dc/",
            json=fixture("measurements_dc_cs"),
        )

        data = make_client().measurements_dc(146, variable="cs")

        assert "300KTL-Inversor1" in data
        strings = data["300KTL-Inversor1"]
        assert "cs1" in strings
        ts = next(iter(strings["cs1"]))
        assert isinstance(ts, datetime)


class TestProjectMeasurement:
    @responses.activate
    def test_variable_then_dev_name_then_series(self):
        responses.get(
            f"{BASE}/monitoring/project/146/measurement/", json=fixture("measurement_vp1")
        )

        data = make_client().project_measurement(146, variable="vp1")

        assert "vp1" in data
        dev_series = next(iter(data["vp1"].values()))
        ts = next(iter(dev_series))
        assert isinstance(ts, datetime)


class TestAvailabilityDetail:
    @responses.activate
    def test_parses_inverters_and_strings(self):
        responses.get(
            f"{BASE}/monitoring/project_availability_detail/146/",
            json=fixture("availability_detail"),
        )

        detail = make_client().availability_detail(146)

        assert isinstance(detail, AvailabilityDetail)
        assert detail.availability is not None
        inv = detail.inverters["300KTL-Inversor1"]
        assert inv.availability is not None
        assert "pv4" in inv.strings
