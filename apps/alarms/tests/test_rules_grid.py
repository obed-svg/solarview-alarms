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


def relay(active=True, pf=0.98, kw=240.0):
    return RelayStatus(
        time=NOON, active=active, kw=kw, kva=250.0, pf=pf, f_abc=60.0,
        currents={}, voltages={},
    )


@pytest.fixture
def project(db):
    return Project.objects.create(external_id=146, name="El Son", synced_at=timezone.now())


def make_ctx(project, relay_value, now=NOON):
    client = MagicMock()
    if isinstance(relay_value, Exception):
        client.relay_now.side_effect = relay_value
    else:
        client.relay_now.return_value = relay_value
    return EvaluationContext(project=project, client=client, now=now)


@pytest.mark.django_db
class TestRecloserOpen:
    def test_open_in_solar_hours_fires(self, project):
        outcomes = RecloserOpen().evaluate(make_ctx(project, relay(active=False)))

        assert outcomes[0].status == "firing"

    def test_closed_is_ok(self, project):
        assert RecloserOpen().evaluate(make_ctx(project, relay(active=True)))[0].status == "ok"

    def test_active_null_is_not_computable(self, project):
        # visto en la API real: active puede venir null
        outcomes = RecloserOpen().evaluate(make_ctx(project, relay(active=None)))

        assert outcomes[0].status == "not_computable"

    def test_night_is_ok(self, project):
        outcomes = RecloserOpen().evaluate(
            make_ctx(project, relay(active=False), now=NIGHT)
        )

        assert outcomes[0].status == "ok"
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
    def test_low_pf_under_load_fires(self, project):
        outcomes = PowerFactorLow().evaluate(make_ctx(project, relay(pf=0.82, kw=200.0)))

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["pf"] == 0.82

    def test_low_pf_at_low_load_is_ok(self, project):
        # con poca carga el FP no es representativo (validación del Excel)
        outcomes = PowerFactorLow().evaluate(make_ctx(project, relay(pf=0.2, kw=3.0)))

        assert outcomes[0].status == "ok"
        assert outcomes[0].reason == "excluded:low_load"

    def test_healthy_pf_is_ok(self, project):
        assert PowerFactorLow().evaluate(
            make_ctx(project, relay(pf=0.98, kw=200.0))
        )[0].status == "ok"

    def test_missing_pf_not_computable(self, project):
        outcomes = PowerFactorLow().evaluate(make_ctx(project, relay(pf=None, kw=200.0)))

        assert outcomes[0].status == "not_computable"

    def test_no_relay_does_not_apply(self, project):
        assert PowerFactorLow().evaluate(
            make_ctx(project, SolarViewNotAssociated("Relay not found"))
        ) == []


@pytest.mark.django_db
class TestRegistryComplete:
    def test_every_catalog_rule_has_a_class(self):
        # las 19 reglas de engine (todas menos alarm_sla_breach) tienen clase
        assert validate_registry() == []
