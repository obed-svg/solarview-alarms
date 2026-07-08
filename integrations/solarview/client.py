import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .exceptions import (
    SolarViewAPIError,
    SolarViewAuthError,
    SolarViewNotAssociated,
    SolarViewTimeout,
)
from .schemas import (
    AvailabilityDetail,
    GenerationSummary,
    InverterLive,
    PowerSeries,
    ProjectInfo,
    RelayStatus,
    WeatherSeries,
    parse_series,
)

DEFAULT_TIMEOUT = (5, 30)  # (connect, read) segundos


class SolarViewClient:
    """Cliente HTTP de la API SolarView. Todas las rutas van por el alias /monitoring/."""

    def __init__(self, base_url: str, token: str, timeout: tuple = DEFAULT_TIMEOUT):
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            base_url = f"https://{base_url}"
        self.base_url = base_url
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Token {token}"
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    @classmethod
    def from_settings(cls) -> "SolarViewClient":
        """Construye el cliente desde los settings de Django (secretos del .env)."""
        from django.conf import settings

        return cls(base_url=settings.SOLARVIEW_BASE_URL, token=settings.SOLARVIEW_STATIC_TOKEN)

    def get(self, path: str, params: dict | None = None):
        """GET a /monitoring/{path}. Devuelve `results` del envelope, o el body
        completo si el endpoint no usa envelope (p.ej. /generation/)."""
        url = f"{self.base_url}/monitoring/{path.lstrip('/')}"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise SolarViewTimeout(f"Timeout en {path}") from exc
        except requests.exceptions.RetryError as exc:
            raise SolarViewAPIError(
                f"Reintentos agotados en {path}", path=path
            ) from exc

        if response.status_code in (401, 403):
            raise SolarViewAuthError(f"Auth rechazada ({response.status_code}) en {path}")

        try:
            body = response.json()
        except ValueError as exc:
            raise SolarViewAPIError(
                f"Respuesta no-JSON (HTTP {response.status_code}) en {path}",
                path=path,
                status_code=response.status_code,
            ) from exc

        if isinstance(body, dict) and body.get("success") is False:
            message = body.get("message") or str(body.get("error"))
            if response.status_code == 404:
                raise SolarViewNotAssociated(message, path=path, status_code=404)
            raise SolarViewAPIError(message, path=path, status_code=response.status_code)

        if response.status_code >= 400:
            raise SolarViewAPIError(
                f"HTTP {response.status_code} en {path}",
                path=path,
                status_code=response.status_code,
            )

        if isinstance(body, dict) and "results" in body and "success" in body:
            return body["results"]
        return body

    # --- Métodos por endpoint (devuelven estructuras tipadas de schemas.py) ---

    def list_projects(self) -> list[ProjectInfo]:
        results = self.get("project/")
        data = results.get("data", results) if isinstance(results, dict) else results
        return [ProjectInfo.from_api(p) for p in data]

    def project_inverters(self, project_id: int) -> list[InverterLive]:
        results = self.get(f"project/{project_id}/inverter/")
        return [InverterLive.from_api(i) for i in results]

    def project_power(
        self,
        project_id: int,
        date_from: str | None = None,
        date_to: str | None = None,
        total_power: bool = True,
    ) -> PowerSeries:
        params: dict = {"total_power": "1"} if total_power else {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return PowerSeries.from_api(self.get(f"project/{project_id}/power/", params=params))

    def project_weather(self, project_id: int, date_from: str, date_to: str) -> WeatherSeries:
        params = {"date_from": date_from, "date_to": date_to}
        return WeatherSeries.from_api(self.get(f"project/{project_id}/weather/", params=params))

    def relay_now(self, project_id: int) -> RelayStatus:
        return RelayStatus.from_api(self.get(f"project/{project_id}/relay/"))

    def relay_historical(self, project_id: int, start_date: str, end_date: str, variables: str):
        params = {"start_date": start_date, "end_date": end_date, "vars": variables}
        raw = self.get(f"project/{project_id}/relay/historical/", params=params)
        return {ts: vals for ts, vals in ((k, v) for k, v in (raw or {}).items())}

    def quoia_history(self, project_id: int, init_date: str, end_date: str):
        params = {"init_date": init_date, "end_date": end_date}
        return self.get(f"project/{project_id}/quoia_measurements_history/", params=params)

    def generation(self, project_id: int, start_date: str, end_date: str) -> GenerationSummary:
        params = {"start_date": start_date, "end_date": end_date}
        return GenerationSummary.from_api(
            self.get(f"project/{project_id}/generation/", params=params)
        )

    def measurements_dc(
        self,
        project_id: int,
        variable: str = "cs",
        inverter: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """{dev_name: {var: TimeSeries}} — indexado por dev_name, no por id."""
        params: dict = {"variable": variable}
        if inverter is not None:
            params["inverter"] = inverter
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        raw = self.get(f"project/{project_id}/measurements-dc/", params=params)
        return {
            dev_name: {var: parse_series(series) for var, series in variables.items()}
            for dev_name, variables in (raw or {}).items()
        }

    def project_measurement(
        self,
        project_id: int,
        variable: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """{variable: {dev_name: TimeSeries}}."""
        params: dict = {"variable": variable}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        raw = self.get(f"project/{project_id}/measurement/", params=params)
        parsed: dict = {}
        for var, by_dev in (raw or {}).items():
            if isinstance(by_dev, dict):
                parsed[var] = {dev: parse_series(series) for dev, series in by_dev.items()}
        return parsed

    def availability_detail(self, project_id: int) -> AvailabilityDetail:
        return AvailabilityDetail.from_api(self.get(f"project_availability_detail/{project_id}/"))
