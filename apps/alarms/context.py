"""EvaluationContext: acceso a datos compartido por todas las reglas de un tick.

Se crea UNA instancia por (proyecto, tick). Cachea cada consulta a la API para
que ~17 reglas cuesten ~6-8 requests. Si la API falla para un dato, devuelve el
sentinel Unavailable y la regla que lo necesite reporta not_computable.

Convención de tiempo: la API entrega timestamps NAIVE en hora local del proyecto
(America/Bogota). `self.now` es naive local para comparar directo contra series.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone as dj_timezone

from apps.plants.models import MaintenanceWindow
from integrations.solarview.exceptions import (
    SolarViewAPIError,
    SolarViewError,
    SolarViewNotAssociated,
)
from integrations.solarview.schemas import TimeSeries


class Unavailable:
    """Sentinel: el dato no se pudo obtener. Falsy; `reason` explica por qué."""

    def __init__(self, reason: str):
        self.reason = reason

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"Unavailable({self.reason!r})"


class EvaluationContext:
    def __init__(self, project, client, now: datetime | None = None):
        self.project = project
        self.client = client
        self.tz = ZoneInfo(project.timezone)
        self.now = now or dj_timezone.now().astimezone(self.tz).replace(tzinfo=None)
        self._cache: dict[str, object] = {}
        self._params_cache: dict[str, dict] = {}
        self._firing: set[tuple[str, str]] = set()

    # --- Umbrales ---

    def params(self, rule_code: str) -> dict:
        if rule_code not in self._params_cache:
            from apps.alarms.models import AlarmRule

            rule = AlarmRule.objects.get(code=rule_code)
            self._params_cache[rule_code] = rule.params_for(self.project)
        return self._params_cache[rule_code]

    # --- Fetchers cacheados (una request real por tick, compartida entre reglas) ---

    def _cached(self, key: str, fetch):
        if key not in self._cache:
            try:
                self._cache[key] = fetch()
            except SolarViewNotAssociated:
                self._cache[key] = Unavailable("not_associated")
            except SolarViewError as exc:
                self._cache[key] = Unavailable(f"{type(exc).__name__}: {exc}")
        return self._cache[key]

    def inverters_live(self):
        return self._cached(
            "inverters", lambda: self.client.project_inverters(self.project.external_id)
        )

    def power(self):
        return self._cached(
            "power", lambda: self.client.project_power(self.project.external_id)
        )

    def weather(self):
        # T44: estación marcada como NO confiable en admin (sensores faltantes
        # que reportan 0 — envenenarían el POA de todas las reglas). Se trata
        # como "sin estación": reglas 14/15/16 no aplican y poa_series cae al
        # fallback de /power/. Ni siquiera se consulta la API.
        if self.project.ignore_weather_station:
            return Unavailable("not_associated")

        day = self.now.strftime("%Y-%m-%d")
        return self._cached(
            "weather",
            lambda: self.client.project_weather(
                self.project.external_id,
                date_from=f"{day} 00:00:00-05:00",
                date_to=f"{day} 23:59:59-05:00",
            ),
        )

    def relay(self):
        return self._cached("relay", lambda: self.client.relay_now(self.project.external_id))

    def string_currents(self):
        return self._cached(
            "dc_cs",
            lambda: self.client.measurements_dc(self.project.external_id, variable="cs"),
        )

    def generation(self):
        day = self.now.strftime("%Y-%m-%d")
        return self._cached(
            "generation",
            lambda: self.client.generation(self.project.external_id, day, day),
        )

    def quoia(self):
        """Mediciones de frontera (últimas ~24 h; sin fechas: cualquier query
        param provoca un 500 del backend, ver client.quoia_history).

        Si el histórico falla con error de API, consulta el live UNA vez como
        oráculo de existencia: su 404 de negocio ("No se encontraron nodos en
        Manager") es la única señal de que el proyecto NO tiene medidor →
        not_associated y las reglas 8/9/10 no aplican (45/77 proyectos). Si el
        live responde error de API (500 "-1"), el medidor EXISTE (encontró
        nodos) pero ninguna fuente entrega datos → "meter_silent": la regla 8
        lo trata como medidor sin comunicación (T34). Timeout/auth del live no
        afirman nada → se conserva la razón del histórico."""
        if "quoia" not in self._cache:
            result = self._cached(
                "quoia", lambda: self.client.quoia_history(self.project.external_id)
            )
            if isinstance(result, Unavailable) and result.reason != "not_associated":
                # meter_silent exige que el histórico haya fallado por API (un
                # timeout del histórico no prueba ausencia de datos)
                history_api_error = result.reason.startswith("SolarViewAPIError")
                try:
                    self.client.quoia_live(self.project.external_id)
                except SolarViewNotAssociated:
                    self._cache["quoia"] = Unavailable("not_associated")
                except SolarViewAPIError:
                    if history_api_error:
                        self._cache["quoia"] = Unavailable("meter_silent")
                except SolarViewError:
                    pass  # timeout/auth: conservar la razón del histórico
        return self._cache["quoia"]

    def poa_series(self) -> TimeSeries | Unavailable:
        """POA del proyecto: irradiation_POA de la estación meteo si existe;
        si el proyecto no tiene estación, la irradiance del endpoint de power."""
        return self.poa_with_source()[0]

    def poa_with_source(self) -> tuple[TimeSeries | Unavailable, str]:
        """(serie POA, fuente). Fuentes: "station" (sensor físico de la
        estación), "power_sensor" (irradiance de /power/ con spire=false =
        sensor local), "power_spire" (spire=true = modelo satelital regional
        Spire — llave señalada por el usuario, T46: se actualiza ~cada hora y
        comparte valores entre proyectos vecinos → congelamiento es su
        cadencia normal, no una falla)."""
        weather = self.weather()
        if not isinstance(weather, Unavailable) and weather.irradiation_poa:
            return weather.irradiation_poa, "station"
        power = self.power()
        if isinstance(power, Unavailable):
            return power, "unavailable"
        return power.irradiance, ("power_spire" if power.spire else "power_sensor")

    # --- Ventanas con tolerancia a lag ---

    def series_window(
        self, series: TimeSeries, minutes: int, lag_minutes: int = 0
    ) -> TimeSeries:
        """Puntos en [now - lag - minutes, now - lag]: nunca evalúa el borde
        presente, donde el backend aún puede no haber escrito."""
        end = self.now - timedelta(minutes=lag_minutes)
        start = end - timedelta(minutes=minutes)
        return {ts: v for ts, v in series.items() if start <= ts <= end}

    # --- Helpers semánticos ---

    def is_solar_hours(self, at: datetime | None = None, margin_minutes: int = 0) -> bool:
        """¿`at` cae en horario solar? `margin_minutes` recorta ambos extremos:
        los inversores duermen desde ANTES del ocaso astral (sol bajo = sin
        producción), así que las reglas sensibles al anochecer usan margen."""
        at = at or self.now
        margin = timedelta(minutes=margin_minutes)

        def fixed_window() -> bool:
            start = at.replace(hour=6, minute=0, second=0, microsecond=0) + margin
            end = at.replace(hour=18, minute=0, second=0, microsecond=0) - margin
            return start <= at < end

        if self.project.latitude is None or self.project.longitude is None:
            return fixed_window()
        from astral import LocationInfo
        from astral.sun import sun

        location = LocationInfo(
            latitude=float(self.project.latitude), longitude=float(self.project.longitude)
        )
        try:
            times = sun(location.observer, date=at.date(), tzinfo=self.tz)
        except ValueError:
            # coordenadas basura (visto: proyecto con lat/lon invertidas por la
            # API → "lat -75" = Antártida y astral explota). Fallback horario fijo.
            return fixed_window()
        sunrise = times["sunrise"]
        if not 4 <= sunrise.hour < 9:
            # T37: coordenadas centinela/basura que NO explotan (visto:
            # lat=-1, lon=-1 = "no configurado" → astral pone el amanecer a la
            # ~01:00 hora local y la ventana solar queda corrida ~5 h: reglas
            # evaluando de madrugada y gateadas en la tarde real). Un amanecer
            # fuera de [04:00, 09:00) local no es creíble para la zona horaria
            # del proyecto → horario fijo.
            return fixed_window()
        aware = at.replace(tzinfo=self.tz)
        return sunrise + margin <= aware <= times["sunset"] - margin

    def inverter_model(self, external_id: int):
        """Fila plants.Inverter por external_id (None si no está sincronizada)."""
        if "inverter_models" not in self._cache:
            self._cache["inverter_models"] = {
                inv.external_id: inv for inv in self.project.inverters.all()
            }
        return self._cache["inverter_models"].get(external_id)

    def in_maintenance(self, inverter=None) -> bool:
        aware_now = self.now.replace(tzinfo=self.tz)
        return (
            MaintenanceWindow.objects.active_at(self.project, aware_now, inverter=inverter)
            .exists()
        )

    # --- Flags entre fases (exclusiones "no clasificar si comunicación caída") ---

    def set_firing(self, rule_code: str, dedup_suffix: str = "") -> None:
        self._firing.add((rule_code, dedup_suffix))

    def flag_active(self, rule_code: str, dedup_suffix: str | None = None) -> bool:
        """¿La regla `rule_code` disparó en este tick? Sin sufijo: ¿disparó para
        CUALQUIER componente?"""
        if dedup_suffix is None:
            return any(code == rule_code for code, _ in self._firing)
        return (rule_code, dedup_suffix) in self._firing
