from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.rules.communication import InverterCommLost, MeterCommLost
from apps.plants.models import Inverter, MaintenanceWindow, Project
from integrations.solarview.exceptions import (
    SolarViewAPIError,
    SolarViewNotAssociated,
    SolarViewTimeout,
)
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

    def test_at_dusk_does_not_evaluate(self, project):
        # 17:40 con margen 45 min (ocaso fallback 18:00): inversores durmiéndose
        # no son falla de comunicación — no evalúa ni toca alarmas
        client_ctx = make_ctx(project, [live(1571, 45)])
        client_ctx.now = datetime(2026, 7, 8, 17, 40)

        assert InverterCommLost().evaluate(client_ctx) == []

    def test_at_night_does_not_evaluate(self, project):
        ctx = make_ctx(project, [live(1571, 400)])
        ctx.now = datetime(2026, 7, 8, 2, 0)

        assert InverterCommLost().evaluate(ctx) == []


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

    def test_quoia_timeout_is_not_computable(self, project):
        # un timeout no prueba ausencia de datos: no alarmar ni resolver
        ctx = make_ctx(project, [live(1571, 3)], SolarViewTimeout("lento"))

        assert MeterCommLost().evaluate(ctx)[0].status == "not_computable"

    def test_meter_silent_with_live_inverters_fires(self, project):
        # T34 (caso real: 143, 149, 104, 160, 174, 178): el oráculo confirma
        # nodos en Manager pero ni el histórico ni el live entregan datos
        ctx = make_ctx(project, [live(1571, 3)], SolarViewAPIError("updated_node"))
        ctx.client.quoia_live.side_effect = SolarViewAPIError("-1")

        outcomes = MeterCommLost().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["last_data_at"] is None
        assert "sin datos" in outcomes[0].evidence["diagnosis"]

    def test_meter_silent_with_inverters_down_not_computable(self, project):
        # medidor mudo pero inversores tampoco reportan: no se puede aislar
        ctx = make_ctx(project, [live(1571, 90)], SolarViewAPIError("updated_node"))
        ctx.client.quoia_live.side_effect = SolarViewAPIError("-1")

        assert MeterCommLost().evaluate(ctx)[0].status == "not_computable"

    def test_night_freezes_without_calling_api(self, project):
        # T38 (visto en producción 03:27): de noche los quoia cambian de
        # régimen (paran a las 20:30 o pasan a cadencia horaria → age 61 vs
        # umbral 60 = flap). De noche no se juzga ni se consulta la API; las
        # alarmas abiertas se congelan (not_computable, no ok).
        ctx = make_ctx(project, [live(1571, 3)], self.quoia_data(417))
        ctx.now = datetime(2026, 7, 9, 3, 27)

        outcomes = MeterCommLost().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "excluded:night"
        ctx.client.quoia_history.assert_not_called()
