from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.rules.communication import InverterCommLost, MeterCommLost
from apps.plants.models import Inverter, MaintenanceWindow, Project
from integrations.solarview.exceptions import SolarViewNotAssociated, SolarViewTimeout
from integrations.solarview.schemas import InverterLive

NOW = datetime(2026, 7, 8, 12, 0)


def live(iid, minutes_ago, dev_name=None) -> InverterLive:
    return InverterLive(
        id=iid, dev_name=dev_name or f"INV-{iid}", state="Grid-connected", power=100.0,
        efficiency=98.0, temperature=60.0,
        time=None if minutes_ago is None else NOW - timedelta(minutes=minutes_ago),
    )


@pytest.fixture
def project(db):
    return Project.objects.create(external_id=146, name="El Son", synced_at=timezone.now())


def make_ctx(project, inverters=None, quoia=None):
    client = MagicMock()
    if isinstance(inverters, Exception):
        client.project_inverters.side_effect = inverters
    else:
        client.project_inverters.return_value = inverters or []
    if isinstance(quoia, Exception):
        client.quoia_history.side_effect = quoia
    else:
        client.quoia_history.return_value = quoia or {}
    return EvaluationContext(project=project, client=client, now=NOW)


@pytest.mark.django_db
class TestInverterCommLost:
    def test_fresh_inverters_are_ok(self, project):
        ctx = make_ctx(project, [live(1571, 3), live(1575, 8)])

        outcomes = InverterCommLost().evaluate(ctx)

        assert [o.status for o in outcomes] == ["ok", "ok"]

    def test_stale_inverter_fires_individually(self, project):
        # umbral = stale(15) + lag(5) = 20 min
        ctx = make_ctx(project, [live(1571, 45), live(1575, 3)])

        outcomes = {o.dedup_suffix: o for o in InverterCommLost().evaluate(ctx)}

        assert outcomes["inv:1571"].status == "firing"
        assert outcomes["inv:1571"].evidence["age_minutes"] == 45
        assert outcomes["inv:1571"].inverter_external_id == 1571
        assert outcomes["inv:1575"].status == "ok"

    def test_inverter_without_timestamp_fires(self, project):
        ctx = make_ctx(project, [live(1571, None)])

        outcomes = InverterCommLost().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["last_data_at"] is None

    def test_maintenance_excludes_that_inverter(self, project):
        inv_model = Inverter.objects.create(
            project=project, external_id=1571, dev_name="INV-1571", synced_at=timezone.now()
        )
        MaintenanceWindow.objects.create(
            project=project, inverter=inv_model,
            starts_at=timezone.make_aware(NOW - timedelta(hours=1)),
            ends_at=timezone.make_aware(NOW + timedelta(hours=1)),
        )
        ctx = make_ctx(project, [live(1571, 45), live(1575, 45)])

        outcomes = {o.dedup_suffix: o for o in InverterCommLost().evaluate(ctx)}

        assert outcomes["inv:1571"].status == "ok"
        assert outcomes["inv:1571"].reason == "excluded:maintenance"
        assert outcomes["inv:1575"].status == "firing"  # sin ventana, sí dispara

    def test_api_down_is_not_computable(self, project):
        ctx = make_ctx(project, SolarViewTimeout("slow"))

        outcomes = InverterCommLost().evaluate(ctx)

        assert len(outcomes) == 1
        assert outcomes[0].status == "not_computable"


@pytest.mark.django_db
class TestMeterCommLost:
    def quoia_data(self, minutes_ago):
        ts = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
        return {ts: {"value": 250.4, "unit": "kWh"}}

    def test_fresh_meter_is_ok(self, project):
        ctx = make_ctx(project, [live(1571, 3)], self.quoia_data(30))

        assert MeterCommLost().evaluate(ctx)[0].status == "ok"

    def test_stale_meter_with_live_inverters_fires(self, project):
        ctx = make_ctx(project, [live(1571, 3)], self.quoia_data(120))

        outcomes = MeterCommLost().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["age_minutes"] == 120

    def test_stale_meter_but_inverters_also_down_not_computable(self, project):
        # sin inversores reportando no se puede confirmar que sea el medidor
        ctx = make_ctx(project, [live(1571, 90)], self.quoia_data(120))

        outcomes = MeterCommLost().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert "inversores" in outcomes[0].reason

    def test_project_without_quoia_produces_no_outcomes(self, project):
        ctx = make_ctx(project, [live(1571, 3)], SolarViewNotAssociated("sin medidor"))

        assert MeterCommLost().evaluate(ctx) == []

    def test_quoia_api_error_is_not_computable(self, project):
        # caso real actual: quoia devuelve 500 en todos los proyectos
        ctx = make_ctx(project, [live(1571, 3)], SolarViewTimeout("500"))

        assert MeterCommLost().evaluate(ctx)[0].status == "not_computable"
