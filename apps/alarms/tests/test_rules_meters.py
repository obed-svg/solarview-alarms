from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.models import Severity
from apps.alarms.rules.meters import MeterInverterMismatch, MeterNoIncrement
from apps.plants.models import Project
from integrations.solarview.exceptions import SolarViewAPIError, SolarViewNotAssociated
from integrations.solarview.schemas import GenerationSummary

# T45: a las 12:10 con lag 5 la hora evaluada es la ÚLTIMA COMPLETA: 11:00→12:00
NOW = datetime(2026, 7, 8, 12, 10)
HOUR_START = datetime(2026, 7, 8, 11, 0)


def quoia_series(intervals_kwh):
    """Intervalos de la hora 11→12 etiquetados al CIERRE con la deriva real de
    segundos ("11:15:04" = intervalo que termina a las 11:15)."""
    step = 60 / len(intervals_kwh)
    points = {}
    for i, kwh in enumerate(intervals_kwh, start=1):
        ts = HOUR_START + timedelta(minutes=step * i, seconds=4)
        points[ts.strftime("%Y-%m-%d %H:%M:%S")] = {"value": float(kwh), "unit": "kWh"}
    return points


def generation(hour_kwh, current_partial_kwh=6.0):
    """Buckets horarios etiquetados al INICIO: 11:00 = hora cerrada 11→12;
    12:00 = hora EN CURSO, parcial (la que fabricaba el mismatch de 520%)."""
    return GenerationSummary(
        project_id=146, total_kwh=hour_kwh + current_partial_kwh,
        hourly={
            HOUR_START: hour_kwh,
            datetime(2026, 7, 8, 12, 0): current_partial_kwh,
        },
    )


@pytest.fixture
def project(db):
    return Project.objects.create(
        external_id=146, name="El Son", installed_capacity_kw=1000, synced_at=timezone.now()
    )


def make_ctx(project, quoia=None, gen=None):
    client = MagicMock()
    if isinstance(quoia, Exception):
        client.quoia_history.side_effect = quoia
    else:
        client.quoia_history.return_value = quoia or {}
    client.generation.return_value = gen if gen is not None else generation(50.0)
    return EvaluationContext(project=project, client=client, now=NOW)


@pytest.mark.django_db
class TestMeterNoIncrement:
    def test_energy_flowing_is_ok(self, project):
        ctx = make_ctx(project, quoia=quoia_series([12, 12, 13, 12]))

        assert MeterNoIncrement().evaluate(ctx)[0].status == "ok"

    def test_zero_energy_with_generation_fires(self, project):
        ctx = make_ctx(project, quoia=quoia_series([0, 0, 0, 0]), gen=generation(50.0))

        outcomes = MeterNoIncrement().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["frontier_energy_kwh"] == 0
        assert outcomes[0].evidence["hour"] == "2026-07-08 11:00-12:00"

    def test_zero_energy_without_generation_is_ok(self, project):
        # la planta no generó en esa hora (< piso): no es falla del medidor
        ctx = make_ctx(project, quoia=quoia_series([0, 0, 0, 0]), gen=generation(0.5))

        assert MeterNoIncrement().evaluate(ctx)[0].status == "ok"

    def test_sparse_hour_is_not_computable(self, project):
        # 2 puntos pegados al cierre: abarcan < media hora → no sumar a ciegas
        sparse = {
            (HOUR_START + timedelta(minutes=m, seconds=4)).strftime("%Y-%m-%d %H:%M:%S"): {
                "value": 8.0, "unit": "kWh",
            }
            for m in (50, 60)
        }
        ctx = make_ctx(project, quoia=sparse)

        outcomes = MeterNoIncrement().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "quoia:ventana_insuficiente"

    def test_no_quoia_meter_does_not_apply(self, project):
        ctx = make_ctx(project, quoia=SolarViewNotAssociated("sin medidor"))

        assert MeterNoIncrement().evaluate(ctx) == []

    def test_never_connected_meter_does_not_apply(self, project):
        # caso real T29: histórico crashea pero el live revela que el proyecto
        # no tiene nodos quoia en Manager → la regla no aplica
        ctx = make_ctx(project, quoia=SolarViewAPIError("updated_node"))
        ctx.client.quoia_live.side_effect = SolarViewNotAssociated("No se encontraron nodos")

        assert MeterNoIncrement().evaluate(ctx) == []

    def test_quoia_broken_is_not_computable(self, project):
        ctx = make_ctx(project, quoia=SolarViewAPIError("500"))

        assert MeterNoIncrement().evaluate(ctx)[0].status == "not_computable"

    def test_meter_silent_is_not_computable(self, project):
        # medidor mudo (T34): la regla 8 alarma; 9/10 no pueden computar energía
        ctx = make_ctx(project, quoia=SolarViewAPIError("updated_node"))
        ctx.client.quoia_live.side_effect = SolarViewAPIError("-1")

        assert MeterNoIncrement().evaluate(ctx)[0].status == "not_computable"

    def test_self_consumption_does_not_apply(self, db):
        # T35: en autoconsumo la frontera puede legítimamente no incrementar
        auto = Project.objects.create(
            external_id=200, name="Autoconsumo", is_self_consumption=True,
            synced_at=timezone.now(),
        )
        ctx = make_ctx(auto, quoia=quoia_series([0, 0, 0, 0]))

        assert MeterNoIncrement().evaluate(ctx) == []


@pytest.mark.django_db
class TestMeterInverterMismatch:
    """T45: frontera = Σ intervalos quoia de la hora CERRADA 11→12;
    inversores = bucket horario etiquetado 11:00 (nunca el parcial de 12:00)."""

    def test_small_difference_is_ok(self, project):
        # frontera 49.5 vs inversores 50 → 1%
        ctx = make_ctx(project, quoia=quoia_series([12.375] * 4), gen=generation(50.0))

        assert MeterInverterMismatch().evaluate(ctx)[0].status == "ok"

    def test_above_5pct_fires_high(self, project):
        # frontera 45 vs inversores 50 → 10%
        ctx = make_ctx(project, quoia=quoia_series([11.25] * 4), gen=generation(50.0))

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].severity == Severity.HIGH

    def test_between_3_and_5pct_fires_medium(self, project):
        # frontera 48 vs inversores 50 → 4%
        ctx = make_ctx(project, quoia=quoia_series([12.0] * 4), gen=generation(50.0))

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].severity == Severity.MEDIUM

    def test_partial_current_bucket_does_not_fabricate_mismatch(self, project):
        # REGRESIÓN caso real El Olimpo (2026-07-09): quoia ~280 kWh/h contra
        # el bucket PARCIAL de la hora en curso (6 min ≈ 38 kWh) daba "520%".
        # Alineado a la hora cerrada: 280 vs 274 → 2.2% → ok.
        ctx = make_ctx(
            project,
            quoia=quoia_series([72.22, 70.84, 68.08, 68.86]),  # Σ = 280
            gen=generation(274.0, current_partial_kwh=38.3),
        )

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "ok"

    def test_low_energy_hour_is_not_computable(self, project):
        # piso T41: con < 10 kWh los ratios no tienen sentido físico
        ctx = make_ctx(project, quoia=quoia_series([0.1] * 4), gen=generation(0.9))

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert "energia_ventana_insuficiente" in outcomes[0].reason

    def test_missing_hour_bucket_is_not_computable(self, project):
        # /generation/ sin el bucket de la hora cerrada → sin denominador
        gen = GenerationSummary(project_id=146, total_kwh=6.0,
                                hourly={datetime(2026, 7, 8, 12, 0): 6.0})
        ctx = make_ctx(project, quoia=quoia_series([12.0] * 4), gen=gen)

        assert MeterInverterMismatch().evaluate(ctx)[0].status == "not_computable"

    def test_self_consumption_does_not_apply(self, db):
        # T35: el mismatch inversores-vs-frontera es estructural en autoconsumo
        auto = Project.objects.create(
            external_id=201, name="Autoconsumo", is_self_consumption=True,
            synced_at=timezone.now(),
        )
        ctx = make_ctx(auto, quoia=quoia_series([8.0] * 4), gen=generation(50.0))

        assert MeterInverterMismatch().evaluate(ctx) == []
