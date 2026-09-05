"""Cliente del contrato público v1 de SolarView.

El gateway expone el contrato canónico bajo /solarview/. El alias
/api/solarview/ solo se sigue cuando aparece en enlaces paginados; el cliente
nunca construye rutas nuevas con los endpoints deprecados.
"""

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
    EnergySeries,
    GenerationSummary,
    InverterLive,
    PowerSeries,
    ProjectInfo,
    RelayStatus,
    WeatherSeries,
    parse_series,
)

DEFAULT_TIMEOUT = (5, 30)


class SolarViewClient:
    """Cliente HTTP para los endpoints GET de SolarView v1."""

    def __init__(self, base_url: str, token: str, timeout: tuple = DEFAULT_TIMEOUT):
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            base_url = f"https://{base_url}"
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Token {token}", "Accept": "application/json"}
        )
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 502, 503, 504],
            allowed_methods=["GET"],
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    @classmethod
    def from_settings(cls) -> "SolarViewClient":
        from django.conf import settings

        return cls(base_url=settings.SOLARVIEW_BASE_URL, token=settings.SOLARVIEW_STATIC_TOKEN)

    def get(self, path: str, params: dict | None = None):
        """Hace GET a /solarview/{path} y desempaqueta el envelope estándar."""
        url = f"{self.base_url}/solarview/{path.lstrip('/')}"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise SolarViewTimeout(f"Timeout en {path}") from exc
        except requests.exceptions.RetryError as exc:
            raise SolarViewAPIError(f"Reintentos agotados en {path}", path=path) from exc
        except requests.exceptions.RequestException as exc:
            raise SolarViewAPIError(f"Error de conexión en {path}: {exc}", path=path) from exc

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
            message = body.get("detail") if isinstance(body, dict) else None
            raise SolarViewAPIError(
                message or f"HTTP {response.status_code} en {path}",
                path=path,
                status_code=response.status_code,
            )
        if isinstance(body, dict) and "results" in body and "success" in body:
            return body["results"]
        return body

    # Configuración

    def list_projects(self) -> list[ProjectInfo]:
        return [ProjectInfo.from_api(project) for project in self.get("config/company-projects/")]

    def project_detail(self, project_id: int) -> ProjectInfo:
        return ProjectInfo.from_api(self.get(f"config/project-detail/{project_id}/"))

    def exclusions(self, project_id: int) -> list[dict]:
        return self.get("config/exclusions/", params={"project_id": project_id})

    def exclusion_types(self) -> dict:
        return self.get("config/exclusions/types/")

    def relay_now(self, project_id: int) -> RelayStatus:
        return RelayStatus.from_api(self.get("config/recloser/", params={"project_id": project_id}))

    def relay_events(
        self, project_id: int, *, page: int | None = None, page_size: int | None = None
    ) -> dict:
        params = {"project_id": project_id}
        if page is not None:
            params["page"] = page
        if page_size is not None:
            params["page_size"] = page_size
        return self.get("config/recloser/events/", params=params)

    def relay_historical(
        self, project_id: int, start_date: str, end_date: str, variables: str
    ) -> dict:
        return self.get(
            "config/recloser/historical/",
            params={
                "recloser": project_id,
                "start_date": start_date,
                "end_date": end_date,
                "vars": variables,
            },
        )

    # KPIs

    def availability(self) -> dict:
        return self.get("kpis/availability/")

    def availability_detail(self, project_id: int) -> AvailabilityDetail:
        raw = self.get("kpis/availability/detail/", params={"project_id": project_id})
        return AvailabilityDetail.from_api(raw)

    def performance_ratio(self, project_id: int) -> dict:
        raw = self.get("kpis/performance-ratio/", params={"project_id": project_id})
        if isinstance(raw, dict) and "results" in raw:
            raw = raw["results"]
        return parse_series(raw)

    def performance_ratio_historical(
        self,
        project_id: int,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        params: dict = {"project_id": project_id}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        raw = self.get("kpis/performance-ratio/historical/", params=params)
        if isinstance(raw, dict) and "results" in raw:
            raw = raw["results"]
        return parse_series((raw or {}).get("performance_ratio"))

    def project_summary(self, page: int | None = None) -> dict:
        params = {"page": page} if page is not None else None
        return self.get("kpis/summary/", params=params)

    # Mediciones

    def measurements_ac(
        self,
        project_id: int,
        variable: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        params: dict = {"project_id": project_id, "variable": variable}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        raw = self.get("measurements/ac/", params=params)
        parsed = {}
        for name, series in (raw or {}).items():
            if (
                isinstance(series, dict)
                and series
                and all(isinstance(value, dict) for value in series.values())
            ):
                parsed[name] = {
                    inverter: parse_series(values) for inverter, values in series.items()
                }
            else:
                parsed[name] = parse_series(series)
        return parsed

    def measurements_dc(
        self,
        project_id: int,
        variable: str = "cs",
        inverter: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        params: dict = {"project_id": project_id, "variable": variable}
        if inverter is not None:
            params["inverter"] = inverter
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        raw = self.get("measurements/dc/", params=params)
        return {
            dev_name: {name: parse_series(series) for name, series in variables.items()}
            for dev_name, variables in (raw or {}).items()
        }

    def energy(
        self,
        project_id: int,
        granularity: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> EnergySeries:
        params: dict = {"project_id": project_id, "granularity": granularity}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return EnergySeries.from_api(self.get("measurements/energy/", params=params))

    def generation(
        self,
        project_id: int,
        start_date: str,
        end_date: str,
        *,
        by_inverter: bool = False,
    ) -> GenerationSummary:
        params = {
            "project_id": project_id,
            "start_date": start_date,
            "end_date": end_date,
        }
        if by_inverter:
            params["by_inverter"] = "true"
        return GenerationSummary.from_api(self.get("measurements/generation/", params=params))

    def inverter_historical(
        self, inverter_id: int, variable: str, date_from: str, date_to: str
    ) -> dict:
        return self.get(
            "measurements/inverter/",
            params={
                "id": inverter_id,
                "variable": variable,
                "date_from": date_from,
                "date_to": date_to,
            },
        )

    def inverter_detail(self, inverter_id: int) -> InverterLive:
        return InverterLive.from_api(
            self.get("measurements/inverter-detail/", params={"id": inverter_id})
        )

    def project_inverters(self, project_id: int) -> list[InverterLive]:
        results = self.get("measurements/inverters-list/", params={"project_id": project_id})
        return [InverterLive.from_api(inverter) for inverter in results]

    def project_power(
        self,
        project_id: int,
        date_from: str | None = None,
        date_to: str | None = None,
        total_power: bool = True,
        power: str = "active_power",
    ) -> PowerSeries:
        params: dict = {
            "project_id": project_id,
            "power": power,
            "total_power": 1 if total_power else 0,
        }
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return PowerSeries.from_api(self.get("measurements/power/", params=params))

    def border_live(
        self,
        project_id: int,
        *,
        variables: str = "eae",
        meter: str = "latest",
        total: bool = True,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        params: dict = {
            "project_id": project_id,
            "vars": variables,
            "meter": meter,
            "total": 1 if total else 0,
        }
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self.get("measurements/border/", params=params)

    def border_history(self, project_id: int, init_date: str, end_date: str) -> dict:
        return self.get(
            "measurements/border/historical/",
            params={
                "project_id": project_id,
                "init_date": init_date,
                "end_date": end_date,
            },
        )

    def shelter(
        self,
        project_id: int,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        params: dict = {"project_id": project_id}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self.get("measurements/shelter/", params=params)

    def trackers(self, project_id: int) -> dict:
        return self.get("measurements/trackers/", params={"project_id": project_id})

    def project_weather(self, project_id: int, date_from: str, date_to: str) -> WeatherSeries:
        params = {"project_id": project_id, "date_from": date_from, "date_to": date_to}
        return WeatherSeries.from_api(self.get("measurements/weather/", params=params))
