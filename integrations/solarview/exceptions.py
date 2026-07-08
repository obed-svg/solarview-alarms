class SolarViewError(Exception):
    """Base de todos los errores del cliente SolarView."""


class SolarViewAPIError(SolarViewError):
    """La API respondió con envelope success=false o un HTTP de error."""

    def __init__(self, message: str, *, path: str = "", status_code: int | None = None):
        super().__init__(message)
        self.path = path
        self.status_code = status_code


class SolarViewNotAssociated(SolarViewAPIError):
    """El proyecto no tiene el equipo consultado (estación meteo, relay, etc.).

    No es un error transitorio: las reglas que dependen de ese equipo deben
    saltarse para ese proyecto, sin reintentos ni alarmas de comunicación.
    """


class SolarViewAuthError(SolarViewError):
    """401/403: token inválido o sin permisos. No reintentar."""


class SolarViewTimeout(SolarViewError):
    """Timeout de conexión o lectura contra la API."""
