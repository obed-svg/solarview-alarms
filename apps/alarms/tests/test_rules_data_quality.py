from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.models import NonComputableInterval
from apps.alarms.rules.data_quality import (
    AvailabilityInputsMissing,
    DataFrozen,
    PoaInvalid,
    PrInputsMissing,
)
from apps.plants.models import Inverter, Project
from integrations.solarview.exceptions import SolarViewNotAssociated
from integrations.solarview.schemas import InverterLive, PowerSeries, WeatherSeries

NOW = datetime(2026, 7, 8, 12, 0)
NIGHT = datetime(2026, 7, 8, 2, 0)


def series(minutes_back, value_fn, step=1):
    return {
        NOW - timedelta(minutes=m): value_fn(m) for m in range(0, minutes_back + 1, step)
    }


def flat(value):
    return lambda m: value


def varying(base):
    return lambda m: base + m * 1.7


@pytest.fixture
def project(db):
    return Project.objects.create(
        external_id=146, name="El Son", installed_capacity_kw=1000, synced_at=timezone.now()
    )


def make_ctx(project, now=NOW, weather=None, power=None, inverters=None, quoia=None,
             dc=None):
    client = MagicMock()
    for attr, value in [
        ("project_weather", weather), ("project_power", power),
        ("project_inverters", inverters), ("quoia_history", quoia),
        ("measurements_dc", dc),
    ]:
        mock = getattr(client, attr)
        if isinstance(value, Exception):
            mock.side_effect = value
        else:
            mock.return_value = value
    return EvaluationContext(project=project, client=client, now=now)


def weather_of(poa_series, temp_series=None, tmod_series=None):
    # tmod por defecto sano: la regla 11 ahora exige T_mod cuando hay estación
    if tmod_series is None:
        tmod_series = {ts: 45.0 + i * 0.1 for i, ts in enumerate(poa_series)}
    return WeatherSeries(
        irradiation={}, irradiation_poa=poa_series,
        temperature=temp_series or {}, temperature_poa=tmod_series, wind_speed={},
    )


def power_of(power_series, irr_series=None, spire=False):
    return PowerSeries(unit="kW", power=power_series, irradiance=irr_series or {},
                       spire=spire)


@pytest.mark.django_db
class TestPoaInvalid:
    def test_healthy_poa_is_ok(self, project):
        ctx = make_ctx(project, weather=weather_of(series(45, varying(800))),
                       power=power_of(series(45, flat(400), step=5)))

        assert PoaInvalid().evaluate(ctx)[0].status == "ok"

    def test_negative_poa_fires(self, project):
        ctx = make_ctx(project, weather=weather_of(series(45, flat(-12))),
                       power=power_of(series(45, flat(0), step=5)))

        outcomes = PoaInvalid().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["issue"] == "negative"

    def test_poa_zero_with_real_generation_fires(self, project):
        ctx = make_ctx(project, weather=weather_of(series(45, flat(0.0))),
                       power=power_of(series(45, flat(350), step=5)))

        outcomes = PoaInvalid().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["issue"] == "zero_with_generation"

    def test_frozen_poa_in_solar_hours_fires(self, project):
        # sensor FÍSICO de estación congelado 45 min = falla real
        ctx = make_ctx(project, weather=weather_of(series(60, flat(731.5))),
                       power=power_of(series(60, flat(400), step=5)))

        outcomes = PoaInvalid().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["issue"] == "frozen"
        assert outcomes[0].evidence["poa_source"] == "station"

    def test_frozen_spire_model_is_ok(self, project):
        # T46 (llave `spire` de /power/, señalada por el usuario): el modelo
        # satelital regional se actualiza ~cada hora y comparte valores entre
        # vecinos (3 techos en exactamente 606.0) — congelado es su cadencia
        # normal, no una falla. 43 falsas de esta familia en producción.
        ctx = make_ctx(project, weather=SolarViewNotAssociated("no estación"),
                       power=power_of(series(60, flat(400), step=5),
                                      irr_series=series(45, flat(606.0)),
                                      spire=True))

        assert PoaInvalid().evaluate(ctx)[0].status == "ok"

    def test_frozen_power_sensor_still_fires(self, project):
        # spire=false en /power/ = sensor local vía datalogger → frozen SÍ es falla
        ctx = make_ctx(project, weather=SolarViewNotAssociated("no estación"),
                       power=power_of(series(60, flat(400), step=5),
                                      irr_series=series(45, flat(512.0)),
                                      spire=False))

        outcomes = PoaInvalid().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["poa_source"] == "power_sensor"

    def test_frozen_at_night_freezes(self, project):
        # T39: POA congelada en 0.0 de noche es lo normal; not_computable
        # congela alarmas abiertas sin resolverlas en falso al anochecer
        night_series = {NIGHT - timedelta(minutes=m): 0.0 for m in range(0, 60)}
        ctx = make_ctx(project, now=NIGHT, weather=weather_of(night_series),
                       power=power_of({}))

        outcome = PoaInvalid().evaluate(ctx)[0]
        assert outcome.status == "not_computable"
        assert outcome.reason == "excluded:night"

    def test_dawn_window_still_excluded(self, project):
        # T39 (visto al amanecer real): a las 6:30 la ventana de 45 min aún
        # contiene oscuridad → no evaluar hasta amanecer + margen (60)
        dawn = datetime(2026, 7, 8, 6, 30)
        dawn_series = {dawn - timedelta(minutes=m): 0.0 for m in range(0, 60)}
        ctx = make_ctx(project, now=dawn, weather=weather_of(dawn_series),
                       power=power_of({}))

        outcome = PoaInvalid().evaluate(ctx)[0]
        assert outcome.status == "not_computable"
        assert outcome.reason == "excluded:night"


@pytest.mark.django_db
class TestDataFrozen:
    def test_varying_power_is_ok(self, project):
        ctx = make_ctx(project, power=power_of(series(60, varying(300), step=5)),
                       weather=SolarViewNotAssociated("no estación"))

        outcomes = {o.dedup_suffix: o for o in DataFrozen().evaluate(ctx)}

        assert outcomes["signal:power"].status == "ok"

    def test_frozen_nonzero_power_fires(self, project):
        ctx = make_ctx(project, power=power_of(series(60, flat(415.3), step=5)),
                       weather=SolarViewNotAssociated("no estación"))

        outcomes = {o.dedup_suffix: o for o in DataFrozen().evaluate(ctx)}

        assert outcomes["signal:power"].status == "firing"

    def test_night_freezes(self, project):
        # T39: not_computable de noche (temperatura/power constantes son
        # normales de noche; no abrir ni resolver en falso)
        frozen_night = {NIGHT - timedelta(minutes=m): 0.0 for m in range(0, 60, 5)}
        ctx = make_ctx(project, now=NIGHT, power=power_of(frozen_night),
                       weather=SolarViewNotAssociated("no estación"))

        outcomes = DataFrozen().evaluate(ctx)

        assert all(o.status == "not_computable" for o in outcomes)


@pytest.mark.django_db
class TestPrInputsMissing:
    def quoia_fresh(self):
        return {
            (NOW - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S"): {"value": 100}
            for m in range(0, 60, 10)
        }

    def dc_fresh(self):
        return {"INV-1": {"cs1": series(60, flat(5.0), step=5)}}

    def test_all_inputs_present_is_ok(self, project):
        ctx = make_ctx(project, weather=weather_of(series(60, varying(700))),
                       quoia=self.quoia_fresh(), dc=self.dc_fresh(),
                       power=power_of(series(60, flat(300), step=5)))

        assert PrInputsMissing().evaluate(ctx)[0].status == "ok"

    def test_missing_poa_fires_and_marks_interval(self, project):
        ctx = make_ctx(project, weather=SolarViewNotAssociated("no estación"),
                       power=power_of(series(60, flat(300), step=5)),  # sin irradiance
                       quoia=self.quoia_fresh(), dc=self.dc_fresh())

        outcomes = PrInputsMissing().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert "poa" in outcomes[0].evidence["missing_inputs"]
        interval = NonComputableInterval.objects.get()
        assert interval.metric == "pr"
        assert "poa" in interval.missing_inputs

    def test_project_without_quoia_does_not_apply(self, project):
        ctx = make_ctx(project, weather=weather_of(series(60, varying(700))),
                       quoia=SolarViewNotAssociated("sin medidor"), dc=self.dc_fresh(),
                       power=power_of(series(60, flat(300), step=5)))

        assert PrInputsMissing().evaluate(ctx) == []

    def test_night_is_ok(self, project):
        ctx = make_ctx(project, now=NIGHT, weather=weather_of({}), quoia={}, dc={},
                       power=power_of({}))

        assert PrInputsMissing().evaluate(ctx)[0].status == "ok"

    def test_dusk_edge_excluded_by_solar_margin(self, project):
        # 17:20: la ventana [16:20, 17:20] toca el margen del ocaso (18:00 - 30min)
        dusk = datetime(2026, 7, 8, 17, 45)
        ctx = make_ctx(project, now=dusk, weather=SolarViewNotAssociated("no estación"),
                       power=power_of({}), quoia={}, dc={})

        outcomes = PrInputsMissing().evaluate(ctx)

        assert outcomes[0].status == "ok"
        assert outcomes[0].reason == "excluded:solar_margin"

    def test_interval_idempotent_same_hour(self, project):
        ctx_kwargs = dict(
            weather=SolarViewNotAssociated("no estación"),
            power=power_of(series(60, flat(300), step=5)),
            quoia=self.quoia_fresh(), dc=self.dc_fresh(),
        )
        PrInputsMissing().evaluate(make_ctx(project, **ctx_kwargs))
        PrInputsMissing().evaluate(make_ctx(project, now=NOW + timedelta(minutes=5),
                                            **ctx_kwargs))

        assert NonComputableInterval.objects.count() == 1


@pytest.mark.django_db
class TestAvailabilityInputsMissing:
    def inverters(self, time_ok=True, with_state=True, with_power=True):
        return [
            InverterLive(
                id=1571, dev_name="INV-1", state="Grid-connected" if with_state else None,
                power=100.0 if with_power else None, efficiency=98.0, temperature=60.0,
                time=NOW - timedelta(minutes=3) if time_ok else None,
            )
        ]

    def test_complete_inputs_ok(self, project):
        ctx = make_ctx(project, inverters=self.inverters(),
                       weather=weather_of(series(30, varying(700))))

        outcomes = AvailabilityInputsMissing().evaluate(ctx)

        assert outcomes[0].status == "ok"

    def test_silent_inverter_fires_and_marks(self, project):
        # silencio real (T42): ni time ni power
        Inverter.objects.create(project=project, external_id=1571, dev_name="INV-1",
                                synced_at=timezone.now())
        ctx = make_ctx(project,
                       inverters=self.inverters(time_ok=False, with_power=False),
                       weather=weather_of(series(30, varying(700))))

        outcomes = AvailabilityInputsMissing().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].dedup_suffix == "inv:1571"
        assert "timestamp" in outcomes[0].evidence["missing_inputs"]
        interval = NonComputableInterval.objects.get()
        assert interval.metric == "availability"
        assert interval.inverter.external_id == 1571

    def test_missing_time_or_state_with_power_is_ok(self, project):
        # T42: time/state ausentes con power presente = lote incompleto del
        # live, el inversor comunica (flap de 122 en una mañana; el censo
        # demostró state=None frecuentísimo comunicando)
        ctx = make_ctx(project,
                       inverters=self.inverters(time_ok=False, with_state=False),
                       weather=weather_of(series(30, varying(700))))

        outcomes = AvailabilityInputsMissing().evaluate(ctx)

        assert outcomes[0].status == "ok"

    def test_missing_poa_is_not_computable(self, project):
        # T40: sin POA verificable no se puede saber si los inversores deben
        # estar despiertos (arrancan por irradiancia) → not_computable. La
        # señal de POA rota a mediodía la dan las reglas 15/11, no esta.
        ctx = make_ctx(project, inverters=self.inverters(),
                       weather=SolarViewNotAssociated("no estación"),
                       power=power_of(series(30, flat(300), step=5)))  # sin irradiance

        outcomes = AvailabilityInputsMissing().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "poa:no_verificable"
        assert NonComputableInterval.objects.count() == 0

    def test_night_does_not_evaluate(self, project):
        ctx = make_ctx(project, now=NIGHT, inverters=self.inverters(),
                       weather=weather_of({}))

        assert AvailabilityInputsMissing().evaluate(ctx) == []

    def test_dusk_margin_does_not_evaluate(self, project):
        dusk = datetime(2026, 7, 8, 17, 30)  # margen 45 sobre ocaso fallback 18:00
        ctx = make_ctx(project, now=dusk, inverters=self.inverters(time_ok=False),
                       weather=weather_of({}))

        assert AvailabilityInputsMissing().evaluate(ctx) == []
