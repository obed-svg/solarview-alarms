"""Estructuras tipadas de los responses de la API SolarView.

El parsing de timestamps y formas de payload vive SOLO aquí: el resto del
sistema nunca ve JSON crudo de la API.
"""

from dataclasses import dataclass, field
from datetime import datetime

# Serie temporal: timestamp local (America/Bogota, naive) -> valor (None = sin dato)
TimeSeries = dict[datetime, float | None]

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_ts(value: str | None) -> datetime | None:
    """Parsea los formatos de timestamp observados en la API real."""
    if not value:
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


def parse_series(raw: dict | None) -> TimeSeries:
    """Convierte {"2026-07-08 10:00": 12.5, ...} en {datetime: float|None}."""
    series: TimeSeries = {}
    for key, value in (raw or {}).items():
        ts = parse_ts(key)
        if ts is not None:
            series[ts] = value
    return series


@dataclass
class ProjectInfo:
    id: int
    name: str
    lon: float | None
    lat: float | None
    plant_code: str | None
    weather_plant_code: str | None
    is_minifarm: bool
    is_self_consumption: bool
    installed_capacity: float | None
    location: str | None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict) -> "ProjectInfo":
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            lon=data.get("lon"),
            lat=data.get("lat"),
            plant_code=data.get("plant_code"),
            weather_plant_code=data.get("weather_plant_code"),
            is_minifarm=bool(data.get("is_minifarm")),
            is_self_consumption=bool(data.get("is_self_consumption")),
            installed_capacity=data.get("installed_capacity"),
            location=data.get("location"),
            raw=data,
        )


@dataclass
class InverterLive:
    id: int
    dev_name: str
    state: str | None
    power: float | None
    efficiency: float | None
    temperature: float | None
    time: datetime | None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict) -> "InverterLive":
        return cls(
            id=data["id"],
            dev_name=data.get("dev_name", ""),
            state=data.get("state"),
            power=data.get("power"),
            efficiency=data.get("efficiency"),
            temperature=data.get("temperature"),
            time=parse_ts(data.get("time")),
            raw=data,
        )


@dataclass
class PowerSeries:
    unit: str
    power: TimeSeries
    irradiance: TimeSeries
    spire: bool = False

    @classmethod
    def from_api(cls, data: dict) -> "PowerSeries":
        return cls(
            unit=data.get("unit", "kW"),
            power=parse_series(data.get("power")),
            irradiance=parse_series(data.get("irradiance")),
            spire=bool(data.get("spire")),
        )


@dataclass
class WeatherSeries:
    irradiation: TimeSeries
    irradiation_poa: TimeSeries
    temperature: TimeSeries
    temperature_poa: TimeSeries
    wind_speed: TimeSeries
    units: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict) -> "WeatherSeries":
        return cls(
            irradiation=parse_series(data.get("irradiation")),
            irradiation_poa=parse_series(data.get("irradiation_POA")),
            temperature=parse_series(data.get("temperature")),
            temperature_poa=parse_series(data.get("temperature_POA")),
            wind_speed=parse_series(data.get("wind_speed")),
            units=data.get("unit") or {},
        )


@dataclass
class RelayStatus:
    time: datetime | None
    active: bool | None  # la API puede devolver null: estado desconocido
    kw: float | None
    kva: float | None
    pf: float | None
    f_abc: float | None
    currents: dict[str, float | None]
    voltages: dict[str, float | None]
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict) -> "RelayStatus":
        return cls(
            time=parse_ts(data.get("time")),
            active=data.get("active"),
            kw=data.get("kw"),
            kva=data.get("kva"),
            pf=data.get("pf"),
            f_abc=data.get("f_abc"),
            currents={k: data.get(k) for k in ("i_a", "i_b", "i_c", "i_n") if k in data},
            voltages={
                k: data.get(k)
                for k in ("u_a", "u_b", "u_c", "u_r", "u_s", "u_t", "v_three_phase")
                if k in data
            },
            raw=data,
        )


@dataclass
class GenerationSummary:
    project_id: int
    total_kwh: float
    hourly: TimeSeries

    @classmethod
    def from_api(cls, data: dict) -> "GenerationSummary":
        return cls(
            project_id=data.get("project_id", 0),
            total_kwh=data.get("total_generation_kwh") or 0.0,
            hourly=parse_series(data.get("generation_kwh")),
        )


@dataclass
class InverterAvailability:
    availability: float | None
    available: int | None
    not_available: int | None
    strings: dict[str, dict]  # {"pv4": {"cs4": 0.4, "vs4": 610.0}, ...}

    @classmethod
    def from_api(cls, data: dict) -> "InverterAvailability":
        reserved = {"availability", "available", "not_available"}
        return cls(
            availability=data.get("availability"),
            available=data.get("available"),
            not_available=data.get("not_available"),
            strings={k: v for k, v in data.items() if k not in reserved and isinstance(v, dict)},
        )


@dataclass
class AvailabilityDetail:
    availability: float | None
    project_id: int | None
    time: str | None
    inverters: dict[str, InverterAvailability]

    @classmethod
    def from_api(cls, data: dict) -> "AvailabilityDetail":
        return cls(
            availability=data.get("availability"),
            project_id=data.get("project"),
            time=data.get("time"),
            inverters={
                name: InverterAvailability.from_api(inv)
                for name, inv in (data.get("inverters_availability") or {}).items()
            },
        )
