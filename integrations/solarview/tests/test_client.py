import pytest
import requests
import responses

from integrations.solarview.client import SolarViewClient
from integrations.solarview.exceptions import (
    SolarViewAPIError,
    SolarViewAuthError,
    SolarViewNotAssociated,
    SolarViewTimeout,
)

BASE = "https://api.test"


def make_client() -> SolarViewClient:
    return SolarViewClient(base_url=BASE, token="secret-token")


def envelope(results, success=True, message="OK", error=None):
    return {"message": message, "error": error, "results": results, "success": success}


class TestBaseUrlNormalization:
    def test_prepends_https_when_scheme_missing(self):
        client = SolarViewClient(base_url="api.sole.tech", token="t")
        assert client.base_url == "https://api.sole.tech"

    def test_strips_trailing_slash(self):
        client = SolarViewClient(base_url="https://api.test/", token="t")
        assert client.base_url == "https://api.test"


class TestGet:
    @responses.activate
    def test_returns_results_and_sends_token_auth_via_monitoring(self):
        responses.get(f"{BASE}/monitoring/project/", json=envelope([{"id": 1}]))

        result = make_client().get("project/")

        assert result == [{"id": 1}]
        request = responses.calls[0].request
        assert request.headers["Authorization"] == "Token secret-token"
        assert "/monitoring/" in request.url

    @responses.activate
    def test_passes_query_params(self):
        responses.get(f"{BASE}/monitoring/project/1/power/", json=envelope({}))

        make_client().get("project/1/power/", params={"total_power": "1"})

        assert "total_power=1" in responses.calls[0].request.url

    @responses.activate
    def test_business_404_raises_not_associated(self):
        responses.get(
            f"{BASE}/monitoring/project/121/weather/",
            json=envelope({}, success=False, message="No existe estación meteorológica"),
            status=404,
        )

        with pytest.raises(SolarViewNotAssociated, match="No existe estación"):
            make_client().get("project/121/weather/")

    @responses.activate
    def test_envelope_success_false_raises_api_error(self):
        responses.get(
            f"{BASE}/monitoring/project/1/quoia_measurements_history/",
            json=envelope({}, success=False, message="No se pudo realizar la petición",
                          error="ProjectInfo matching query does not exist."),
            status=500,
        )

        with pytest.raises(SolarViewAPIError):
            make_client().get("project/1/quoia_measurements_history/")

    @responses.activate
    def test_401_raises_auth_error(self):
        responses.get(f"{BASE}/monitoring/project/", json={"detail": "bad token"}, status=401)

        with pytest.raises(SolarViewAuthError):
            make_client().get("project/")

    @responses.activate
    def test_timeout_raises_solarview_timeout(self):
        responses.get(
            f"{BASE}/monitoring/project/",
            body=requests.exceptions.ConnectTimeout("slow"),
        )

        with pytest.raises(SolarViewTimeout):
            make_client().get("project/")

    @responses.activate
    def test_retries_on_503_then_succeeds(self):
        responses.get(f"{BASE}/monitoring/project/", status=503)
        responses.get(f"{BASE}/monitoring/project/", json=envelope([{"id": 1}]))

        assert make_client().get("project/") == [{"id": 1}]

    @responses.activate
    def test_success_true_with_200_but_no_results_key_returns_raw_body(self):
        # /generation/ no usa el envelope estándar
        raw = {"project_id": 1, "total_generation_kwh": 10.5, "generation_kwh": {}}
        responses.get(f"{BASE}/monitoring/project/1/generation/", json=raw)

        assert make_client().get("project/1/generation/") == raw
