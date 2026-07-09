from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.engine import validate_registry
from apps.alarms.rules.grid import PowerFactorLow, RecloserOpen
from apps.plants.models import MaintenanceWindow, Project
from integrations.solarview.exceptions import SolarViewNotAssociated, SolarViewTimeout
from integrations.solarview.schemas import RelayStatus

NOON = datetime(2026, 7, 8, 12, 0)
NIGHT = datetime(2026, 7, 8, 2, 0)


def relay(active=True, pf=0.98, currents=None, kw=240.0, voltages=None):
    return RelayStatus(
        time=NOON, active=active, kw=kw, kva=250.0, pf=pf, f_abc=60.0,
        currents={"i_a": 30.0, "i_b": 30.0, "i_c": 30.0} if currents is None else currents,
        voltages=voltages or {},
    )


@pytest.fixture
def project(db):
    return Project.objects.create(
        external_id=146, name="El Son", installed_capacity_kw=1000,
        synced_at=timezone.now(),
    )


def make_ctx(project, relay_value, now=NOON):
    client = MagicMock()
    if isinstance(relay_value, Exception):
        client.relay_now.side_effect = relay_value
    else:
        client.relay_now.return_value = relay_value
    client.project_inverters.return_value = []
    return EvaluationContext(project=project, client=client, now=now)


@pytest.mark.django_db
class TestRecloserOpen:
    def test_open_in_solar_hours_fires(self, project):
        outcomes = RecloserOpen().evaluate(make_ctx(project, relay(active=False)))

        assert outcomes[0].status == "firing"
        assert "currents_a" in outcomes[0].evidence

    def test_closed_is_ok(self, project):
        assert RecloserOpen().evaluate(make_ctx(project, relay(active=True)))[0].status == "ok"

    def test_active_null_is_not_computable(self, project):
        # visto en la API real: active puede venir null
        outcomes = RecloserOpen().evaluate(make_ctx(project, relay(active=None)))

        assert outcomes[0].status == "not_computable"

    def test_night_freezes(self, project):
        # T39: hay plantas que abren el reconectador de noche por operación →
        # not_computable congela sin abrir ni resolver en falso
        outcomes = RecloserOpen().evaluate(
            make_ctx(project, relay(active=False), now=NIGHT)
        )

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "excluded:night"

    def test_dawn_margin_still_frozen(self, project):
        # amanecer + margen 30: a las 6:10 las plantas aún están cerrando
        # sus reconectadores nocturnos
        outcomes = RecloserOpen().evaluate(
            make_ctx(project, relay(active=False), now=datetime(2026, 7, 8, 6, 10))
        )

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "excluded:night"

    def test_maintenance_is_scheduled_opening(self, project):
        MaintenanceWindow.objects.create(
            project=project,
            starts_at=timezone.make_aware(NOON - timedelta(hours=1)),
            ends_at=timezone.make_aware(NOON + timedelta(hours=1)),
        )

        outcomes = RecloserOpen().evaluate(make_ctx(project, relay(active=False)))

        assert outcomes[0].status == "ok"
        assert outcomes[0].reason == "excluded:maintenance"

    def test_project_without_relay_does_not_apply(self, project):
        assert RecloserOpen().evaluate(
            make_ctx(project, SolarViewNotAssociated("Relay not found"))
        ) == []

    def test_api_error_not_computable(self, project):
        outcomes = RecloserOpen().evaluate(make_ctx(project, SolarViewTimeout("slow")))

        assert outcomes[0].status == "not_computable"


@pytest.mark.django_db
class TestPowerFactorLow:
    """T35: gate de carga por CORRIENTE — relay.kw nunca participa en lógica."""

    def test_low_pf_under_load_fires(self, project):
        outcomes = PowerFactorLow().evaluate(make_ctx(project, relay(pf=0.82)))

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["pf"] == 0.82
        assert outcomes[0].evidence["currents_a"]["i_a"] == 30.0

    def test_low_current_excluded_as_low_load(self, project):
        # corrientes ≈ 0: baja carga, el FP no es representativo
        outcomes = PowerFactorLow().evaluate(
            make_ctx(project, relay(pf=0.2, currents={"i_a": 0.0, "i_b": 1.0, "i_c": 0.5}))
        )

        assert outcomes[0].status == "ok"
        assert outcomes[0].reason == "excluded:low_load"

    def test_missing_currents_not_computable(self, project):
        # algunos relays no reportan corriente (error del equipo): no se puede
        # calcular y ya — NUNCA caer al kw del relay
        outcomes = PowerFactorLow().evaluate(
            make_ctx(project, relay(pf=0.5, currents={}, kw=200.0))
        )

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "relay:sin_corrientes"

    def test_all_null_currents_not_computable(self, project):
        outcomes = PowerFactorLow().evaluate(
            make_ctx(project, relay(pf=0.5, currents={"i_a": None, "i_b": None, "i_c": None}))
        )

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "relay:sin_corrientes"

    def test_pf_zero_with_load_is_firmware_diagnosis(self, project):
        # caso del fixture real: 34 A por fase con kw=1, pf=0, tensiones=0
        # (firmware desactualizado entrega enteros) → diagnóstico, no alarma
        outcomes = PowerFactorLow().evaluate(
            make_ctx(project, relay(
                pf=0, kw=1.0,
                currents={"i_a": 34.0, "i_b": 34.0, "i_c": 34.0},
                voltages={"u_a": 0, "u_b": 0, "u_c": 0},
            ))
        )

        assert outcomes[0].status == "not_computable"
        assert "firmware" in outcomes[0].reason
        assert outcomes[0].evidence["raw_readings"]["kw"] == 1.0

    def test_open_relay_excluded(self, project):
        # planta abierta: la 17 alarma la apertura; sin flujo no hay pf
        outcomes = PowerFactorLow().evaluate(make_ctx(project, relay(active=False, pf=0.2)))

        assert outcomes[0].status == "ok"
        assert outcomes[0].reason == "excluded:recloser_open"

    def test_pf_in_percent_is_normalized(self, project):
        # firmware que entrega pf en % (95) → 0.95 → sano
        outcomes = PowerFactorLow().evaluate(make_ctx(project, relay(pf=95)))

        assert outcomes[0].status == "ok"

    def test_healthy_pf_is_ok(self, project):
        assert PowerFactorLow().evaluate(
            make_ctx(project, relay(pf=0.98))
        )[0].status == "ok"

    def test_missing_pf_not_computable(self, project):
        outcomes = PowerFactorLow().evaluate(make_ctx(project, relay(pf=None)))

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "relay:pf_ausente"

    def test_no_relay_does_not_apply(self, project):
        assert PowerFactorLow().evaluate(
            make_ctx(project, SolarViewNotAssociated("Relay not found"))
        ) == []

    def test_night_is_excluded(self, project):
        # visto en producción: FP 0.318 a las 18:40 = consumo auxiliar nocturno.
        # T39: not_computable (congelar, no resolver en falso al anochecer)
        outcomes = PowerFactorLow().evaluate(
            make_ctx(project, relay(pf=0.318), now=NIGHT)
        )

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "excluded:night"

    def test_self_consumption_does_not_apply(self, db):
        # autoconsumo: el pf de frontera lo domina la carga del cliente
        auto = Project.objects.create(
            external_id=200, name="Autoconsumo", is_self_consumption=True,
            synced_at=timezone.now(),
        )

        assert PowerFactorLow().evaluate(make_ctx(auto, relay(pf=0.5))) == []


@pytest.mark.django_db
class TestRegistryComplete:
    def test_every_catalog_rule_has_a_class(self):
        # las 19 reglas de engine (todas menos alarm_sla_breach) tienen clase
        assert validate_registry() == []
