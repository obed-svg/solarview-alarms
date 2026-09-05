import json
from datetime import datetime
from pathlib import Path

import responses

from integrations.solarview.client import SolarViewClient
from integrations.solarview.schemas import (
    AvailabilityDetail,
    EnergySeries,
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


def envelope_results(name: str):
    return fixture(name)


def make_client() -> SolarViewClient:
    return SolarViewClient(base_url=BASE, token="t")


class TestParseTs:
    def test_supported_formats(self):
        assert parse_ts("2026-07-08 16:10:04") == datetime(2026, 7, 8, 16, 10, 4)
        assert parse_ts("2026-07-08 00:05") == datetime(2026, 7, 8, 0, 5)
        assert parse_ts("2026-08-13T14:45:00-05:00") == datetime(2026, 8, 13, 14, 45)

    def test_invalid_returns_none(self):
        assert parse_ts("no-fecha") is None
        assert parse_ts(None) is None


class TestBorderHistory:
    @responses.activate
    def test_sends_required_range_and_preserves_interval_payload(self):
        responses.get(
            f"{BASE}/solarview/measurements/border/historical/",
            json=fixture("quoia_history"),
        )

        result = make_client().border_history(
            108,
            init_date="2026-07-08T00:00:00-05:00",
            end_date="2026-07-08T12:00:00-05:00",
        )

        request_url = responses.calls[0].request.url
        assert "project_id=108" in request_url
        assert "init_date=2026-07-08T00%3A00%3A00-05%3A00" in request_url
        assert "end_date=2026-07-08T12%3A00%3A00-05%3A00" in request_url
        assert result["2026-07-08 11:30:00"] == {"value": 212.98, "unit": "kWh"}


class TestProjects:
    @responses.activate
    def test_list_projects_uses_company_projects(self):
        body = fixture("project_list")
        results = body["results"]
        data = results.get("data", results) if isinstance(results, dict) else results
        responses.get(
            f"{BASE}/solarview/config/company-projects/",
            json={"message": "OK", "error": None, "results": data, "success": True},
        )

        projects = make_client().list_projects()

        assert projects
        assert isinstance(projects[0], ProjectInfo)
        assert "project_id" not in responses.calls[0].request.url

    @responses.activate
    def test_project_detail_parses_dirty_numeric_fields(self):
        body = fixture("project_detail_basic")
        body["results"]["installed_capacity"] = "Desconocida"
        body["results"]["lat"] = ""
        responses.get(
            f"{BASE}/solarview/config/project-detail/146/",
            json=body,
        )

        detail = make_client().project_detail(146)

        assert detail.installed_capacity is None
        assert detail.lat is None


class TestProjectInverters:
    @responses.activate
    def test_parses_live_inverters_and_sends_project_id(self):
        responses.get(
            f"{BASE}/solarview/measurements/inverters-list/",
            json=fixture("inverters_live"),
        )

        inverters = make_client().project_inverters(146)

        assert len(inverters) == 5
        assert isinstance(inverters[0], InverterLive)
        assert isinstance(inverters[0].time, datetime)
        assert "project_id=146" in responses.calls[0].request.url


class TestProjectPower:
    @responses.activate
    def test_parses_power_and_irradiance_series(self):
        responses.get(
            f"{BASE}/solarview/measurements/power/",
            json=fixture("power_today"),
        )

        series = make_client().project_power(146)

        assert isinstance(series, PowerSeries)
        assert len(series.power) > 0
        assert len(series.irradiance) > len(series.power)
        request_url = responses.calls[0].request.url
        assert "project_id=146" in request_url
        assert "power=active_power" in request_url
        assert "total_power=1" in request_url


class TestProjectWeather:
    @responses.activate
    def test_parses_poa_units_and_query(self):
        responses.get(
            f"{BASE}/solarview/measurements/weather/",
            json=fixture("weather_today"),
        )

        weather = make_client().project_weather(
            146,
            date_from="2026-07-08 00:00:00-05:00",
            date_to="2026-07-08 23:59:59-05:00",
        )

        assert isinstance(weather, WeatherSeries)
        assert weather.irradiation_poa
        assert weather.units["irradiation_POA"] == "W/m2"
        assert "project_id=146" in responses.calls[0].request.url


class TestRelayNow:
    @responses.activate
    def test_active_null_is_preserved(self):
        responses.get(
            f"{BASE}/solarview/config/recloser/",
            json=fixture("relay_now"),
        )

        relay = make_client().relay_now(146)

        assert isinstance(relay, RelayStatus)
        assert relay.active is None
        assert relay.f_abc == 59.98
        assert "project_id=146" in responses.calls[0].request.url


class TestGeneration:
    @responses.activate
    def test_parses_generation_and_sends_query(self):
        responses.get(
            f"{BASE}/solarview/measurements/generation/",
            json=fixture("generation_today"),
        )

        gen = make_client().generation(146, start_date="2026-07-08", end_date="2026-07-08")

        assert isinstance(gen, GenerationSummary)
        assert gen.total_kwh >= 0
        assert "project_id=146" in responses.calls[0].request.url


class TestEnergy:
    @responses.activate
    def test_parses_points_and_unit(self):
        responses.get(
            f"{BASE}/solarview/measurements/energy/",
            json={
                "message": "OK",
                "error": None,
                "success": True,
                "results": {
                    "project_id": "146",
                    "granularity": "hour",
                    "unit": "kWh",
                    "points": [{"time": "2026-08-13 10:00", "kwh": "12.5"}],
                },
            },
        )

        energy = make_client().energy(146, "hour", date_from="2026-08-13")

        assert isinstance(energy, EnergySeries)
        assert energy.points[0].kwh == 12.5
        assert "granularity=hour" in responses.calls[0].request.url


class TestMeasurements:
    @responses.activate
    def test_dc_is_indexed_by_inverter(self):
        responses.get(
            f"{BASE}/solarview/measurements/dc/",
            json=fixture("measurements_dc_cs"),
        )

        data = make_client().measurements_dc(146, variable="cs")

        assert "300KTL-Inversor1" in data
        assert isinstance(next(iter(data["300KTL-Inversor1"]["cs1"])), datetime)

    @responses.activate
    def test_ac_supports_per_inverter_shape(self):
        responses.get(
            f"{BASE}/solarview/measurements/ac/",
            json=fixture("measurement_vp1"),
        )

        data = make_client().measurements_ac(146, variable="vp1")

        dev_series = next(iter(data["vp1"].values()))
        assert isinstance(next(iter(dev_series)), datetime)


class TestAvailabilityDetail:
    @responses.activate
    def test_parses_inverters_and_strings(self):
        responses.get(
            f"{BASE}/solarview/kpis/availability/detail/",
            json=fixture("availability_detail"),
        )

        detail = make_client().availability_detail(146)

        assert isinstance(detail, AvailabilityDetail)
        assert detail.availability is not None
        assert "pv4" in detail.inverters["300KTL-Inversor1"].strings
        assert "project_id=146" in responses.calls[0].request.url
