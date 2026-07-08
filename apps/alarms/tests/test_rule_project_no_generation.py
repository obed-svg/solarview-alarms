from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.rules.project import ProjectNoGeneration
from apps.plants.models import MaintenanceWindow, Project
from integrations.solarview.exceptions import SolarViewAPIError, SolarViewNotAssociated
from integrations.solarview.schemas import PowerSeries, RelayStatus

NOW = datetime(2026, 7, 8, 12, 0)


def series(minutes_back: int, value: float, step: int = 1) -> dict:
    """Serie densa que termina en NOW: {NOW-mb, ..., NOW} con el mismo valor."""
    return {
        NOW - timedelta(minutes=m): value for m in range(0, minutes_back + 1, step)
    }


def make_relay(active=True) -> RelayStatus:
    return RelayStatus(
        time=NOW, active=active, kw=1.0, kva=1.0, pf=0.9, f_abc=60.0,
        currents={}, voltages={},
    )


@pytest.fixture
def project(db):
    return Project.objects.create(
        external_id=146, name="El Son", installed_capacity_kw=1000,
        synced_at=timezone.now(),
    )


def make_ctx(project, poa_value=850.0, power_value=0.05, relay=None, weather_missing=True):
    """Contexto con POA desde power.irradiance (proyecto sin estación meteo)."""
    client = MagicMock()
    if weather_missing:
        client.project_weather.side_effect = SolarViewNotAssociated("no estación")
    client.project_power.return_value = PowerSeries(
        unit="kW",
        power=series(30, power_value, step=5),
        irradiance=series(30, poa_value),
    )
    if relay is None:
        client.relay_now.side_effect = SolarViewNotAssociated("Relay not found")
    elif isinstance(relay, Exception):
        client.relay_now.side_effect = relay
    else:
        client.relay_now.return_value = relay
    return EvaluationContext(project=project, client=client, now=NOW)


@pytest.mark.django_db
class TestProjectNoGeneration:
    def test_fires_with_poa_high_and_power_zero(self, project):
        ctx = make_ctx(project, poa_value=850.0, power_value=0.05, relay=make_relay(True))

        outcomes = ProjectNoGeneration().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["poa_min_wm2_observed"] == 850.0
        assert outcomes[0].evidence["power_max_kw_observed"] == 0.05

    def test_ok_when_generating(self, project):
        ctx = make_ctx(project, poa_value=850.0, power_value=450.0, relay=make_relay(True))

        assert ProjectNoGeneration().evaluate(ctx)[0].status == "ok"

    def test_ok_with_low_irradiance(self, project):
        # amanecer/atardecer: POA bajo el umbral NO es falla
        ctx = make_ctx(project, poa_value=40.0, power_value=0.0, relay=make_relay(True))

        outcomes = ProjectNoGeneration().evaluate(ctx)

        assert outcomes[0].status == "ok"
        assert outcomes[0].reason == "excluded:low_irradiance"

    def test_excluded_when_recloser_open(self, project):
        # reconectador abierto: es la alarma 17, no esta
        ctx = make_ctx(project, poa_value=850.0, power_value=0.0, relay=make_relay(False))

        outcomes = ProjectNoGeneration().evaluate(ctx)

        assert outcomes[0].status == "ok"
        assert outcomes[0].reason == "excluded:recloser_open"

    def test_fires_when_project_has_no_relay(self, project):
        # sin reconectador en el proyecto: la condición eléctrica basta
        ctx = make_ctx(project, poa_value=850.0, power_value=0.0, relay=None)

        assert ProjectNoGeneration().evaluate(ctx)[0].status == "firing"

    def test_relay_active_null_does_not_block(self, project):
        # active=null real de la API: estado desconocido, no bloquea la alarma
        ctx = make_ctx(project, poa_value=850.0, power_value=0.0, relay=make_relay(None))

        outcomes = ProjectNoGeneration().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["relay_state"] == "unknown"

    def test_excluded_in_maintenance(self, project):
        MaintenanceWindow.objects.create(
            project=project,
            starts_at=timezone.make_aware(NOW - timedelta(hours=1)),
            ends_at=timezone.make_aware(NOW + timedelta(hours=1)),
        )
        ctx = make_ctx(project, poa_value=850.0, power_value=0.0, relay=make_relay(True))

        outcomes = ProjectNoGeneration().evaluate(ctx)

        assert outcomes[0].status == "ok"
        assert outcomes[0].reason == "excluded:maintenance"

    def test_not_computable_when_meteo_flagged_invalid(self, project):
        ctx = make_ctx(project, poa_value=850.0, power_value=0.0, relay=make_relay(True))
        ctx.set_firing("poa_invalid")

        outcomes = ProjectNoGeneration().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "excluded:poa_invalid"

    def test_not_computable_without_power_data(self, project):
        client = MagicMock()
        client.project_weather.side_effect = SolarViewNotAssociated("no estación")
        client.project_power.side_effect = SolarViewAPIError("500")
        ctx = EvaluationContext(project=project, client=client, now=NOW)

        assert ProjectNoGeneration().evaluate(ctx)[0].status == "not_computable"

    def test_not_computable_with_sparse_window(self, project):
        # 2 puntos en 15 min no bastan para afirmar "sostenido"
        client = MagicMock()
        client.project_weather.side_effect = SolarViewNotAssociated("no estación")
        client.project_power.return_value = PowerSeries(
            unit="kW",
            power={NOW - timedelta(minutes=20): 0.0},
            irradiance={NOW - timedelta(minutes=10): 900.0, NOW: 900.0},
        )
        client.relay_now.return_value = make_relay(True)
        ctx = EvaluationContext(project=project, client=client, now=NOW)

        outcomes = ProjectNoGeneration().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert "window" in outcomes[0].reason
