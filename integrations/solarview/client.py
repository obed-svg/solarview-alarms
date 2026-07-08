import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .exceptions import (
    SolarViewAPIError,
    SolarViewAuthError,
    SolarViewNotAssociated,
    SolarViewTimeout,
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
