from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.context import EvaluationContext
from apps.alarms.models import Severity
from apps.alarms.rules.meters import (
    MeterInverterMismatch,
    MeterNoIncrement,
    _quoia_points,
)
from apps.plants.models import Project
from integrations.solarview.exceptions import SolarViewAPIError, SolarViewNotAssociated
from integrations.solarview.schemas import GenerationSummary

# T50: a las 12:10 con lag 5 la ventana es el DÍA ACUMULADO hasta la última
# hora completa: 00:00 → 12:00 (la hora 12→13 en curso nunca entra).
NOW = datetime(2026, 7, 8, 12, 10)
DAY_START = datetime(2026, 7, 8, 0, 0)
DAY_END = datetime(2026, 7, 8, 12, 0)
SUN_HOURS = range(6, 12)  # 6 horas diurnas dentro de la ventana


def quoia_day(hour_kwh=48.0, cadence_min=15, stop_hour=None, extra=None):
    """Serie del día etiquetada al CIERRE con la deriva real de segundos
    ("11:15:04"). `hour_kwh` se reparte entre los intervalos de cada hora;
    las horas nocturnas escriben 0.0, como los medidores reales."""
    points = {}
    step = timedelta(minutes=cadence_min)
    por_hora = hour_kwh if isinstance(hour_kwh, dict) else dict.fromkeys(SUN_HOURS, hour_kwh)
    ts = DAY_START + step
    while ts <= DAY_END:
        if stop_hour is not None and ts.hour >= stop_hour:
            break
        # etiqueta al CIERRE: el intervalo pertenece a la hora en que empieza
        hora = (ts - step).hour
        value = por_hora.get(hora, 0.0) / (60 / cadence_min)
        label = (ts + timedelta(seconds=4)).strftime("%Y-%m-%d %H:%M:%S")
        points[label] = {"value": float(value), "unit": "kWh"}
        ts += step
    points.update(extra or {})
    return points


def quoia_con_huecos(hour_kwh=48.0, conservar=2):
    """Serie del día a la que le faltan intervalos: de cada 4 se conservan
    `conservar` (caso real El Son: 42 de 70; La Paz Vallenata: 25 de 58)."""
    completa = sorted(quoia_day(hour_kwh=hour_kwh).items())
    huecos = {label: value for i, (label, value) in enumerate(completa) if i % 4 < conservar}
    huecos[completa[-1][0]] = completa[-1][1]  # el medidor SÍ llega al final
    return huecos


def quoia_total(points) -> float:
    return sum(p["value"] for p in points.values())


def generation(day_kwh=None, per_hour=50.0, current_partial_kwh=6.0, buckets=None):
    """Buckets horarios etiquetados al INICIO. El de 12:00 es la hora EN CURSO
    (parcial) y debe quedar SIEMPRE fuera de la ventana."""
    hourly = (
        buckets
        if buckets is not None
        else {
            datetime(2026, 7, 8, h, 0): (per_hour if h in SUN_HOURS else 0.0) for h in range(0, 12)
        }
    )
    total = day_kwh if day_kwh is not None else sum(hourly.values())
    hourly[datetime(2026, 7, 8, 12, 0)] = current_partial_kwh
    return GenerationSummary(project_id=146, total_kwh=total + current_partial_kwh, hourly=hourly)


def test_frontier_mwh_is_normalized_to_kwh():
    kwh = quoia_day(hour_kwh=48.0)
    mwh = {
        timestamp: {"value": point["value"] / 1000, "unit": "MWh"}
        for timestamp, point in kwh.items()
    }

    total_kwh = sum(value for _, value in _quoia_points(kwh, DAY_START, DAY_END))
    total_mwh = sum(value for _, value in _quoia_points(mwh, DAY_START, DAY_END))

    assert total_mwh == pytest.approx(total_kwh)


@pytest.fixture
def project(db):
    return Project.objects.create(
        external_id=146, name="El Son", installed_capacity_kw=1000, synced_at=timezone.now()
    )


def make_ctx(project, quoia=None, gen=None):
    client = MagicMock()
    if isinstance(quoia, Exception):
        client.border_history.side_effect = quoia
    else:
        client.border_history.return_value = quoia or {}
    client.generation.return_value = gen if gen is not None else generation()
    return EvaluationContext(project=project, client=client, now=NOW)


@pytest.mark.django_db
class TestMeterNoIncrement:
    def test_energy_flowing_is_ok(self, project):
        ctx = make_ctx(project, quoia=quoia_day())

        assert MeterNoIncrement().evaluate(ctx)[0].status == "ok"

    def test_zero_energy_with_generation_fires(self, project):
        # caso real 2026-07-24 (Cedillanos, Nestlé DPA): el medidor escribe
        # todo el día en cero mientras los inversores generan normal
        ctx = make_ctx(project, quoia=quoia_day(hour_kwh=0.0), gen=generation())

        outcomes = MeterNoIncrement().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].evidence["frontier_energy_kwh"] == 0
        assert outcomes[0].evidence["window"] == "2026-07-08 00:00-12:00"

    def test_zero_energy_without_generation_is_ok(self, project):
        # la planta no generó (< piso): no es falla del medidor
        ctx = make_ctx(project, quoia=quoia_day(hour_kwh=0.0), gen=generation(per_hour=0.5))

        assert MeterNoIncrement().evaluate(ctx)[0].status == "ok"

    def test_meter_stopping_mid_day_is_not_computable(self, project):
        # principio de T48: un medidor que dejó de escribir es meter_comm_lost
        # (regla 8), no un mismatch. Y la ventana NO se recorta a lo que
        # alcanzó a cubrir: comparar tramos parciales reintroduce el desfase
        # de 15-30 min entre las series (caso real Las Piloneras, 22.6%).
        ctx = make_ctx(project, quoia=quoia_day(stop_hour=9))

        outcomes = MeterNoIncrement().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "quoia:no_cubre_hasta_el_final"

    def test_meter_stopping_mid_day_does_not_fabricate_mismatch(self, project):
        ctx = make_ctx(project, quoia=quoia_day(stop_hour=9))

        assert MeterInverterMismatch().evaluate(ctx)[0].status == "not_computable"

    def test_hourly_cadence_meter_is_computable(self, project):
        # T50: 3 de los 37 proyectos escriben CADA 60 MIN (El Son, La Paz
        # Vallenata, La Paz Esmeralda). Con la tolerancia fija de 20 min de
        # T48 quedaban en not_computable perpetuo y nunca se evaluaban.
        ctx = make_ctx(project, quoia=quoia_day(cadence_min=60))

        assert MeterNoIncrement().evaluate(ctx)[0].status == "ok"

    def test_no_quoia_meter_does_not_apply(self, project):
        ctx = make_ctx(project, quoia=SolarViewNotAssociated("sin medidor"))

        assert MeterNoIncrement().evaluate(ctx) == []

    def test_never_connected_meter_does_not_apply(self, project):
        # caso real T29: histórico crashea pero el live revela que el proyecto
        # no tiene nodos quoia en Manager → la regla no aplica
        ctx = make_ctx(project, quoia=SolarViewAPIError("updated_node"))
        ctx.client.border_live.side_effect = SolarViewNotAssociated("No se encontraron nodos")

        assert MeterNoIncrement().evaluate(ctx) == []

    def test_quoia_broken_is_not_computable(self, project):
        ctx = make_ctx(project, quoia=SolarViewAPIError("500"))

        assert MeterNoIncrement().evaluate(ctx)[0].status == "not_computable"

    def test_meter_silent_is_not_computable(self, project):
        # medidor mudo (T34): la regla 8 alarma; 9/10 no pueden computar energía
        ctx = make_ctx(project, quoia=SolarViewAPIError("updated_node"))
        ctx.client.border_live.side_effect = SolarViewAPIError("-1")

        assert MeterNoIncrement().evaluate(ctx)[0].status == "not_computable"

    def test_self_consumption_does_not_apply(self, db):
        # T35: en autoconsumo la frontera puede legítimamente no incrementar
        auto = Project.objects.create(
            external_id=200,
            name="Autoconsumo",
            is_self_consumption=True,
            synced_at=timezone.now(),
        )
        ctx = make_ctx(auto, quoia=quoia_day(hour_kwh=0.0))

        assert MeterNoIncrement().evaluate(ctx) == []


@pytest.mark.django_db
class TestMeterInverterMismatch:
    """T50: ambos lados acumulan el día completo hasta la última hora cerrada."""

    def test_small_difference_is_ok(self, project):
        # frontera 297 vs inversores 300 → 1% (pérdidas reales de trafo/cableado)
        ctx = make_ctx(project, quoia=quoia_day(hour_kwh=49.5), gen=generation())

        assert MeterInverterMismatch().evaluate(ctx)[0].status == "ok"

    def test_above_10pct_fires_high(self, project):
        # caso real Valencia Or_1: la serie del día se reescaló a 2/3 al perder
        # un nodo del medidor → 33% de déficit sostenido
        ctx = make_ctx(project, quoia=quoia_day(hour_kwh=33.3), gen=generation())

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].severity == Severity.HIGH
        assert outcomes[0].evidence["window"] == "2026-07-08 00:00-12:00"
        assert outcomes[0].evidence["mismatch_ratio"] == pytest.approx(0.334, abs=0.005)
        assert outcomes[0].evidence["mismatch_percent"] == pytest.approx(33.4, abs=0.5)

    def test_between_5_and_10pct_fires_medium(self, project):
        # frontera 279 vs inversores 300 → 7%
        ctx = make_ctx(project, quoia=quoia_day(hour_kwh=46.5), gen=generation())

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "firing"
        assert outcomes[0].severity == Severity.MEDIUM

    def test_healthy_plant_with_hourly_noise_is_ok(self, project):
        # REGRESIÓN 2026-07-24: las 10 plantas sanas del arranque. Hora a hora
        # el desfase de 15-30 min entre las series daba 15-89% de "mismatch";
        # acumulando el día quedan en ~2%, que es lo que realmente miden.
        # Misma energía total (300 kWh), repartida distinto entre las horas.
        turnos = dict(zip(SUN_HOURS, [70.0, 30.0] * 3, strict=True))
        ctx = make_ctx(project, quoia=quoia_day(hour_kwh=turnos), gen=generation())

        assert MeterInverterMismatch().evaluate(ctx)[0].status == "ok"

    def test_serie_incompleta_es_not_computable(self, project):
        # caso real El Son (42 de 70 intervalos) y La Paz Vallenata (25 de 58):
        # su "déficit" del 46% era exactamente lo que faltaba de la serie
        ctx = make_ctx(project, quoia=quoia_con_huecos(conservar=2), gen=generation())

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert "serie_incompleta" in outcomes[0].reason

    def test_hueco_grande_es_not_computable(self, project):
        # caso real La Paz Verso: huecos de hasta 150 min en medio de la serie
        completa = quoia_day()
        sin_dos_horas = {
            label: value
            for label, value in completa.items()
            if " 08:" not in label and " 09:" not in label
        }
        ctx = make_ctx(project, quoia=sin_dos_horas, gen=generation())

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert "hueco" in outcomes[0].reason or "incompleta" in outcomes[0].reason

    def test_corrupt_generation_bucket_is_not_computable(self, project):
        # caso real La Puya: cuatro horas en 0.00 y un bucket de 2107 kWh en
        # una planta de 1304 kW — imposible; el backend redistribuyó energía
        buckets = {datetime(2026, 7, 8, h, 0): 0.0 for h in range(0, 12)}
        buckets[datetime(2026, 7, 8, 10, 0)] = 2107.51  # > capacidad (1000)
        ctx = make_ctx(project, quoia=quoia_day(), gen=generation(buckets=buckets))

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert outcomes[0].reason == "generation:bucket_mayor_que_capacidad"

    def test_duplicate_labels_do_not_inflate_frontier(self, project):
        # caso real Gandalf: "17:00:00"=12.621 y "17:00:01"=12.592 son el MISMO
        # intervalo reescrito; sumarlos inflaba la frontera
        base = quoia_day(hour_kwh=48.0)
        duplicados = {}
        for h in SUN_HOURS:
            ts = datetime(2026, 7, 8, h, 15, 5)  # 1 s después de "h:15:04"
            duplicados[ts.strftime("%Y-%m-%d %H:%M:%S")] = {"value": 12.0, "unit": "kWh"}
        limpio = MeterInverterMismatch().evaluate(make_ctx(project, quoia=base))
        con_dups = MeterInverterMismatch().evaluate(make_ctx(project, quoia={**base, **duplicados}))

        assert limpio[0].status == con_dups[0].status == "ok"

    def test_low_energy_day_is_not_computable(self, project):
        # piso T41: con < 10 kWh los ratios no tienen sentido físico
        ctx = make_ctx(project, quoia=quoia_day(hour_kwh=0.1), gen=generation(per_hour=0.9))

        outcomes = MeterInverterMismatch().evaluate(ctx)

        assert outcomes[0].status == "not_computable"
        assert "energia_ventana_insuficiente" in outcomes[0].reason

    def test_only_partial_bucket_is_not_computable(self, project):
        # /generation/ sin buckets cerrados en la ventana → sin denominador
        gen = GenerationSummary(
            project_id=146, total_kwh=6.0, hourly={datetime(2026, 7, 8, 12, 0): 6.0}
        )
        ctx = make_ctx(project, quoia=quoia_day(), gen=gen)

        assert MeterInverterMismatch().evaluate(ctx)[0].status == "not_computable"

    def test_self_consumption_does_not_apply(self, db):
        # T35: el mismatch inversores-vs-frontera es estructural en autoconsumo
        auto = Project.objects.create(
            external_id=201,
            name="Autoconsumo",
            is_self_consumption=True,
            synced_at=timezone.now(),
        )
        ctx = make_ctx(auto, quoia=quoia_day(hour_kwh=8.0), gen=generation())

        assert MeterInverterMismatch().evaluate(auto and ctx) == []
