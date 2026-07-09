"""T30: censo pasivo del vocabulario de `state` de inversores."""
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.engine import _record_state_observations
from apps.plants.models import InverterStateObservation, Project
from integrations.solarview.exceptions import SolarViewAPIError
from integrations.solarview.schemas import InverterLive

NOW = datetime(2026, 7, 8, 12, 0)


def live(inv_id, state, dev_name="Inversor 1"):
    return InverterLive(
        id=inv_id, dev_name=dev_name, state=state,
        power=100.0, efficiency=None, temperature=None, time=NOW,
    )


@pytest.fixture
def project(db):
    return Project.objects.create(
        external_id=146, name="El Son", synced_at=timezone.now()
    )


def make_ctx(project, inverters):
    client = MagicMock()
    if isinstance(inverters, Exception):
        client.project_inverters.side_effect = inverters
    else:
        client.project_inverters.return_value = inverters
    ctx = EvaluationContext(project=project, client=client, now=NOW)
    ctx.inverters_live()  # poblar el cache, como haría una regla en el tick
    return ctx


@pytest.mark.django_db
class TestRecordStateObservations:
    def test_records_new_states_with_counts_and_breadcrumb(self, project):
        ctx = make_ctx(project, [
            live(1, "Grid-connected", "Inversor 1"),
            live(2, "Grid-connected", "Inversor 2"),
            live(3, "Grid connection: self-derating", "Inversor 3"),
        ])

        _record_state_observations(ctx)

        grid = InverterStateObservation.objects.get(state="Grid-connected")
        assert grid.times_seen == 2
        assert grid.first_project == project
        assert grid.first_dev_name == "Inversor 1"
        derating = InverterStateObservation.objects.get(
            state="Grid connection: self-derating"
        )
        assert derating.times_seen == 1
        assert derating.first_dev_name == "Inversor 3"

    def test_repeat_sightings_accumulate_and_keep_first_seen(self, project):
        ctx = make_ctx(project, [live(1, "Grid-connected")])
        _record_state_observations(ctx)
        first = InverterStateObservation.objects.get(state="Grid-connected")

        other = Project.objects.create(
            external_id=147, name="Otra", synced_at=timezone.now()
        )
        ctx2 = make_ctx(other, [live(9, "Grid-connected", "pv9")])
        _record_state_observations(ctx2)

        obs = InverterStateObservation.objects.get(state="Grid-connected")
        assert obs.times_seen == 2
        assert obs.first_seen_at == first.first_seen_at
        assert obs.first_project == project  # el primero, no el segundo
        assert obs.last_seen_at >= first.last_seen_at

    def test_none_states_are_skipped(self, project):
        # caso real nocturno: 314/314 inversores con state=None
        ctx = make_ctx(project, [live(1, None), live(2, None)])

        _record_state_observations(ctx)

        assert InverterStateObservation.objects.count() == 0

    def test_unavailable_inverters_do_nothing(self, project):
        ctx = make_ctx(project, SolarViewAPIError("500"))

        _record_state_observations(ctx)

        assert InverterStateObservation.objects.count() == 0

    def test_cache_not_populated_does_nothing(self, project):
        # ninguna regla pidió inversores en este tick → no consultar la API
        client = MagicMock()
        ctx = EvaluationContext(project=project, client=client, now=NOW)

        _record_state_observations(ctx)

        client.project_inverters.assert_not_called()
        assert InverterStateObservation.objects.count() == 0
