from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.rules.inverter import InverterDerating, InverterUnavailable
from apps.alarms.rules.strings import DcIsolationLow, StringLowCurrent, StringZeroCurrent
from apps.plants.models import Project
from integrations.solarview.schemas import InverterLive, PowerSeries, WeatherSeries

NOW = datetime(2026, 7, 8, 12, 0)


def series(value, minutes=30, step=5):
    return {NOW - timedelta(minutes=m): value for m in range(0, minutes + 1, step)}


def live(iid, dev_name, power=250.0, state="Grid-connected", temperature=60.0):
    return InverterLive(
        id=iid, dev_name=dev_name, state=state, power=power, efficiency=98.0,
        temperature=temperature, time=NOW - timedelta(minutes=3),
    )


@pytest.fixture
def project(db):
    return Project.objects.create(
        external_id=146, name="El Son", installed_capacity_kw=1000, synced_at=timezone.now()
    )


def make_ctx(project, inverters, dc=None, poa_value=850.0):
    client = MagicMock()
    client.project_inverters.return_value = inverters
    client.project_weather.return_value = WeatherSeries(
        irradiation={}, irradiation_poa=series(poa_value, step=1),
        temperature={}, temperature_poa={}, wind_speed={},
    )
    client.project_power.return_value = PowerSeries(
        unit="kW", power=series(sum(i.power or 0 for i in inverters)), irradiance={},
    )
    client.measurements_dc.return_value = dc or {}
    return EvaluationContext(project=project, client=client, now=NOW)


@pytest.mark.django_db
class TestInverterUnavailable:
    def dc_for(self, dev_name, cs_value):
        return {dev_name: {"cs1": series(cs_value), "cs2": series(cs_value)}}

    def test_dead_inverter_with_others_generating_fires(self, project):
        inverters = [live(1, "INV-1", power=0.0), live(2, "INV-2", power=250.0)]
        ctx = make_ctx(project, inverters, dc=self.dc_for("INV-1", 0.0))

        outcomes = {o.dedup_suffix: o for o in InverterUnavailable().evaluate(ctx)}

        assert outcomes["inv:1"].status == "firing"
        assert outcomes["inv:2"].status == "ok"

    def test_all_inverters_down_is_project_level_not_this_rule(self, project):
        inverters = [live(1, "INV-1", power=0.0), live(2, "INV-2", power=0.0)]
        ctx = make_ctx(project, inverters, dc=self.dc_for("INV-1", 0.0))

        outcomes = {o.dedup_suffix: o for o in InverterUnavailable().evaluate(ctx)}

        assert outcomes["inv:1"].status == "ok"
        assert outcomes["inv:1"].reason == "excluded:no_comparable_generating"

    def test_comm_lost_inverter_is_not_computable(self, project):
        inverters = [live(1, "INV-1", power=0.0), live(2, "INV-2", power=250.0)]
        ctx = make_ctx(project, inverters, dc=self.dc_for("INV-1", 0.0))
        ctx.set_firing("inverter_comm_lost", "inv:1")

        outcomes = {o.dedup_suffix: o for o in InverterUnavailable().evaluate(ctx)}

        assert outcomes["inv:1"].status == "not_computable"

    def test_low_irradiance_is_ok(self, project):
        inverters = [live(1, "INV-1", power=0.0), live(2, "INV-2", power=100.0)]
        ctx = make_ctx(project, inverters, dc=self.dc_for("INV-1", 0.0), poa_value=50.0)

        outcomes = InverterUnavailable().evaluate(ctx)

        assert all(o.status == "ok" for o in outcomes)


@pytest.mark.django_db
class TestInverterDerating:
    def test_state_keyword_fires(self, project):
        inverters = [live(1, "INV-1", state="Derating: over-temperature")]
        ctx = make_ctx(project, inverters)

        outcomes = InverterDerating().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["trigger"] == "state"

    def test_hot_inverter_underproducing_fires(self, project):
        inverters = [
            live(1, "INV-1", power=100.0, temperature=105.0),
            live(2, "INV-2", power=250.0, temperature=65.0),
            live(3, "INV-3", power=245.0, temperature=64.0),
        ]
        ctx = make_ctx(project, inverters)

        outcomes = {o.dedup_suffix: o for o in InverterDerating().evaluate(ctx)}

        assert outcomes["inv:1"].status == "firing"
        assert outcomes["inv:1"].evidence["trigger"] == "temperature"

    def test_hot_but_producing_normally_is_ok(self, project):
        inverters = [
            live(1, "INV-1", power=250.0, temperature=105.0),
            live(2, "INV-2", power=250.0, temperature=65.0),
        ]
        ctx = make_ctx(project, inverters)

        assert all(o.status == "ok" for o in InverterDerating().evaluate(ctx))


@pytest.mark.django_db
class TestStringZeroCurrent:
    def test_dead_string_with_live_siblings_fires(self, project):
        dc = {"INV-1": {
            "cs1": series(0.0), "cs2": series(8.4), "cs3": series(8.1),
        }}
        ctx = make_ctx(project, [live(1, "INV-1")], dc=dc)

        outcomes = {o.dedup_suffix: o for o in StringZeroCurrent().evaluate(ctx)}

        assert outcomes["inv:1:cs1"].status == "firing"
        assert outcomes["inv:1:cs1"].component_id == "cs1"
        assert outcomes["inv:1:cs2"].status == "ok"

    def test_all_strings_zero_is_inverter_level(self, project):
        dc = {"INV-1": {"cs1": series(0.0), "cs2": series(0.0)}}
        ctx = make_ctx(project, [live(1, "INV-1")], dc=dc)

        outcomes = StringZeroCurrent().evaluate(ctx)

        # sin strings comparables con corriente, no es alarma de string
        assert all(o.status == "ok" for o in outcomes)

    def test_inverter_comm_lost_excluded(self, project):
        dc = {"INV-1": {"cs1": series(0.0), "cs2": series(8.0)}}
        ctx = make_ctx(project, [live(1, "INV-1")], dc=dc)
        ctx.set_firing("inverter_comm_lost", "inv:1")

        outcomes = StringZeroCurrent().evaluate(ctx)

        assert all(o.status == "not_computable" for o in outcomes)

    def test_low_irradiance_ok(self, project):
        dc = {"INV-1": {"cs1": series(0.0), "cs2": series(2.0)}}
        ctx = make_ctx(project, [live(1, "INV-1")], dc=dc, poa_value=40.0)

        assert all(o.status == "ok" for o in StringZeroCurrent().evaluate(ctx))


@pytest.mark.django_db
class TestStringLowCurrent:
    def test_weak_string_fires(self, project):
        dc = {"INV-1": {
            "cs1": series(5.0), "cs2": series(9.0), "cs3": series(9.2),
        }}
        ctx = make_ctx(project, [live(1, "INV-1")], dc=dc)

        outcomes = {o.dedup_suffix: o for o in StringLowCurrent().evaluate(ctx)}

        # 5.0 < 0.8 × avg(9.0, 9.2)
        assert outcomes["inv:1:cs1"].status == "firing"
        assert outcomes["inv:1:cs2"].status == "ok"

    def test_zero_string_left_to_zero_rule(self, project):
        dc = {"INV-1": {"cs1": series(0.05), "cs2": series(9.0), "cs3": series(9.2)}}
        ctx = make_ctx(project, [live(1, "INV-1")], dc=dc)

        outcomes = {o.dedup_suffix: o for o in StringLowCurrent().evaluate(ctx)}

        assert outcomes["inv:1:cs1"].status == "ok"
        assert outcomes["inv:1:cs1"].reason == "excluded:zero_string"

    def test_balanced_strings_ok(self, project):
        dc = {"INV-1": {"cs1": series(8.8), "cs2": series(9.0), "cs3": series(9.2)}}
        ctx = make_ctx(project, [live(1, "INV-1")], dc=dc)

        assert all(o.status == "ok" for o in StringLowCurrent().evaluate(ctx))


@pytest.mark.django_db
class TestDcIsolationLow:
    def test_isolation_state_fires(self, project):
        ctx = make_ctx(project, [live(1, "INV-1", state="Insulation resistance low")])

        outcomes = DcIsolationLow().evaluate(ctx)

        assert outcomes[0].status == "firing"

    def test_normal_state_ok(self, project):
        ctx = make_ctx(project, [live(1, "INV-1")])

        assert DcIsolationLow().evaluate(ctx)[0].status == "ok"
