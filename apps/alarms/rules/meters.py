"""Fase 3 — reglas de medidor de frontera (quoia).

Payload real VALIDADO (2026-07-08, proyecto 108 y 25 más): {ts: {value, unit}}
donde `value` es la energía kWh DEL INTERVALO (~15 min, cadencia variable por
proyecto), NO un contador acumulado — 0.0 de noche, sigue la curva solar.
El endpoint devuelve las últimas ~24 h y NO acepta parámetros de fecha (ver
client.border_history). En proyectos cuyo quoia sigue roto server-side estas
reglas viven en not_computable (comportamiento correcto, sin ruido).

Ventana de comparación (T50): energía ACUMULADA del día, de 00:00 a la última
hora completa. Sustituye a la hora suelta de T45, que seguía fabricando
mismatch en plantas sanas. Censo del 2026-07-24 sobre 26 medidores reales:

- Comparando UNA hora, el ruido mediano entre plantas sanas era 51% y el peor
  290%. Acumulando el día baja a 1.0%, con las 10 plantas sanas de cadencia
  fina entre 0.1% y 1.6% — y los casos patológicos reales en 34% y 46%.
- Las dos series están DESFASADAS entre 15 y 30 min: el error total del día
  se minimiza corriendo quoia −15/−30 min, nunca en 0 (mejora de 2 a 4×). El
  supuesto de T45 ("etiquetas al cierre") NO se sostiene con los datos; −15
  min es exactamente lo que se vería si marcaran el INICIO. Pendiente de
  confirmar con el backend: mientras tanto se conserva la convención de
  cierre porque sesga a incluir un intervalo de más (medidor "más grande" =
  menos falsos déficits) y, sobre un día entero, el borde pesa poco.
- `/measurements/generation/` entrega buckets horarios CORRUPTOS en casos puntuales (caso
  real La Puya: cuatro horas en 0.00 y luego 2107 kWh sobre 1304 kW
  instalados, físicamente imposible) → guarda por capacidad instalada.
- El histórico de quoia es MUTABLE: se recalcula agregando los nodos del
  medidor en cada consulta. Caso real Valencia Or_1: todo el día se reescaló
  por 2/3 exacto entre dos lecturas separadas por 20 min — la firma de perder
  1 de 3 nodos. Por eso un déficit sostenido es señal legítima (la frontera
  mide incompleto y eso se factura), pero conviene confirmarlo por
  persistencia cuando exista el mecanismo pending→active.
"""

from datetime import timedelta

from apps.alarms.context import Unavailable
from apps.alarms.models import Severity
from integrations.solarview.schemas import as_float, parse_ts

from .base import BaseRule, RuleOutcome, register

MIN_INTERVAL_POINTS = 4
LABEL_DRIFT = timedelta(minutes=2)  # etiquetas quoia reales: "14:00:05", "15:15:04"
DUPLICATE_WINDOW = timedelta(seconds=5)  # "17:00:00" y "17:00:01" = un intervalo


def _day_window(ctx, lag_minutes: int = 5):
    """(00:00 del día, fin) donde `fin` es el cierre de la última hora completa
    que ya superó el lag de escritura del backend. A las 12:03 con lag 5 la
    hora 11→12 puede estar incompleta → fin = 11:00; desde las 12:05 → 12:00."""
    effective = ctx.now - timedelta(minutes=lag_minutes)
    end = effective.replace(minute=0, second=0, microsecond=0)
    return end.replace(hour=0), end


def _quoia_points(quoia_raw: dict, start, end) -> list[tuple]:
    """Puntos (ts, kWh) dentro de (start, end], deduplicados: el backend
    reescribe el mismo intervalo con etiquetas a un segundo de distancia
    ("17:00:00"=12.621 y "17:00:01"=12.592), lo que inflaba la suma."""
    lo, hi = start + LABEL_DRIFT, end + LABEL_DRIFT
    points = []
    for key, payload in quoia_raw.items():
        ts = parse_ts(key)
        if ts is not None and lo < ts <= hi and isinstance(payload, dict):
            value = as_float(payload.get("value"))
            unit = str(payload.get("unit") or "kWh").strip().lower()
            if value is None:
                continue
            if unit == "mwh":
                value *= 1000
            elif unit != "kwh":
                # No comparar unidades desconocidas como si fueran kWh.
                continue
            points.append((ts, value))
    points.sort()
    deduped = []
    for ts, value in points:
        if deduped and ts - deduped[-1][0] <= DUPLICATE_WINDOW:
            continue  # mismo intervalo reescrito: se conserva el primero
        deduped.append((ts, value))
    return deduped


def _frontier_energy(quoia_raw: dict, start, end, min_completeness: float = 0.9):
    """(energía, motivo) de la frontera acumulada en (start, end].

    Tres condiciones, todas aprendidas de datos reales del 2026-07-24, y
    ninguna negociable: comparar una serie incompleta contra los inversores
    fabrica un déficit del tamaño de lo que falta.

    1. COBERTURA hasta el final de la ventana, con tolerancia derivada de la
       cadencia del propio medidor (1, 15 o 60 min según proyecto), no de una
       constante. Si el medidor dejó de escribir, eso es `meter_comm_lost`
       (regla 8), no un mismatch — principio de T48. Y NO se recorta la
       ventana a lo que alcanzó a cubrir: comparar tramos parciales reintroduce
       el desfase de 15-30 min entre las series (caso real Las Piloneras:
       22.6% de "déficit" en 00:00-14:00 con los totales del día cuadrando
       dentro del 0.4%).
    2. COMPLETITUD: al menos `min_completeness` de los intervalos esperados
       para esa cadencia (El Son escribió 42 de 70 y La Paz Vallenata 25 de
       58 — sus "déficits" del 46% eran justo los intervalos ausentes).
    3. Sin HUECOS mayores a dos cadencias (huecos reales observados: hasta
       150 min en La Paz Verso).
    """
    points = _quoia_points(quoia_raw, start, end)
    if len(points) < MIN_INTERVAL_POINTS:
        return None, "quoia:pocos_puntos"

    gaps = sorted((points[i + 1][0] - points[i][0]).total_seconds() for i in range(len(points) - 1))
    # La cadencia es el percentil 10 de los intervalos, NO la mediana: los
    # huecos solo pueden agrandar la separación entre puntos, nunca acortarla,
    # así que con la mediana una serie con huecos periódicos se autodeclara de
    # cadencia lenta y pasa la prueba de completitud que debía reprobar.
    cadence = timedelta(seconds=gaps[len(gaps) // 10])
    if cadence <= timedelta(0):
        return None, "quoia:cadencia_invalida"

    if points[-1][0] < end + LABEL_DRIFT - 2 * cadence:
        return None, "quoia:no_cubre_hasta_el_final"

    expected = (end - start) / cadence
    if len(points) < min_completeness * expected:
        faltan = round((1 - len(points) / expected) * 100)
        return None, f"quoia:serie_incompleta (falta {faltan}% de los intervalos)"

    if gaps[-1] > 2 * cadence.total_seconds():
        return None, f"quoia:hueco_de_{round(gaps[-1] / 60)}min"

    return sum(value for _, value in points), ""


def _inverter_energy_between(ctx, start, end) -> tuple[float | None, str]:
    """Suma de los buckets horarios de /measurements/generation/ en [start, end).

    Descarta la ventana entera si algún bucket supera la capacidad instalada:
    es dato corrupto del backend (energía redistribuida entre horas), y
    compararlo contra la frontera fabrica mismatch en una planta sana."""
    gen = ctx.generation()
    if isinstance(gen, Unavailable):
        return None, "generation:no_disponible"
    capacity = ctx.project.installed_capacity_kw
    total, hours = 0.0, 0
    for ts, value in gen.hourly.items():
        if value is None or not (start <= ts < end):
            continue
        if capacity and value > float(capacity):
            return None, "generation:bucket_mayor_que_capacidad"
        total += value
        hours += 1
    if not hours:
        return None, "generation:sin_buckets_en_ventana"
    return total, ""


@register
class MeterNoIncrement(BaseRule):
    """Regla 9: la frontera no registró energía EN LO QUE VA DEL DÍA aunque los
    inversores sí generaron (≥ min_window_energy_kwh — la propia generación es
    la prueba de producción). Sin medidor quoia la regla no aplica.

    T50: la ventana es el día acumulado, no una hora. Caso real del 2026-07-24:
    Cedillanos y Nestlé DPA escribieron 32 y 481 puntos, TODOS en cero, con sus
    inversores generando normal — un medidor que no registra en todo el día es
    exactamente lo que esta regla debe cazar.

    NO aplica en autoconsumo (decisión del usuario 2026-07-08, T35): la
    energía se consume localmente, la frontera puede legítimamente no
    incrementar mientras los inversores generan."""

    code = "meter_no_increment"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        if ctx.project.is_self_consumption:
            return []

        quoia = ctx.quoia()
        if isinstance(quoia, Unavailable):
            if quoia.reason == "not_associated":
                return []
            return [RuleOutcome(status="not_computable", reason=f"quoia:{quoia.reason}")]

        params = ctx.params(self.code)
        day_start, day_end = _day_window(ctx, params.get("data_lag_minutes", 5))
        if day_start >= day_end:
            return [RuleOutcome(status="not_computable", reason="dia:sin_hora_completa")]

        frontier_energy, reason = _frontier_energy(
            quoia, day_start, day_end, params.get("min_completeness", 0.9)
        )
        if frontier_energy is None:
            return [RuleOutcome(status="not_computable", reason=reason)]

        inverter_energy, reason = _inverter_energy_between(ctx, day_start, day_end)
        if inverter_energy is None:
            return [RuleOutcome(status="not_computable", reason=reason)]

        min_energy = params.get("min_window_energy_kwh", 10)
        if frontier_energy <= params["delta_zero_kwh"] and inverter_energy >= min_energy:
            return [
                RuleOutcome(
                    status="firing",
                    evidence={
                        "frontier_energy_kwh": round(frontier_energy, 2),
                        "inverter_energy_kwh": round(inverter_energy, 2),
                        "window": f"{day_start:%Y-%m-%d %H:%M}-{day_end:%H:%M}",
                    },
                )
            ]
        return [RuleOutcome(status="ok")]


@register
class MeterInverterMismatch(BaseRule):
    """Regla 10: |E_inv - E_frontera| / E_inv sobre la energía ACUMULADA DEL DÍA
    (T50). La hora suelta de T45 seguía fabricando mismatch: el ruido mediano
    entre plantas sanas era 51% comparando una hora y 1.0% acumulando el día.
    Umbrales calibrados con ese censo: >10% → high; 5-10% → medium (el engine
    escala pero nunca baja severidad).

    NO aplica en autoconsumo (decisión del usuario 2026-07-08, T35): el
    mismatch inversores-vs-frontera es estructural cuando la energía se
    consume localmente."""

    code = "meter_inverter_mismatch"
    phase = 3

    def evaluate(self, ctx) -> list[RuleOutcome]:
        if ctx.project.is_self_consumption:
            return []

        quoia = ctx.quoia()
        if isinstance(quoia, Unavailable):
            if quoia.reason == "not_associated":
                return []
            return [RuleOutcome(status="not_computable", reason=f"quoia:{quoia.reason}")]

        params = ctx.params(self.code)
        day_start, day_end = _day_window(ctx, params.get("data_lag_minutes", 5))
        if day_start >= day_end:
            return [RuleOutcome(status="not_computable", reason="dia:sin_hora_completa")]

        frontier, reason = _frontier_energy(
            quoia, day_start, day_end, params.get("min_completeness", 0.9)
        )
        if frontier is None:
            return [RuleOutcome(status="not_computable", reason=reason)]

        inverter_energy, reason = _inverter_energy_between(ctx, day_start, day_end)
        if not inverter_energy:  # None o 0: sin denominador no hay ratio
            return [
                RuleOutcome(status="not_computable", reason=reason or "generation:sin_energia_inv")
            ]
        # piso de energía (T41): sin energía significativa los ratios no
        # tienen sentido físico (visto: 75-100% con centésimas de kWh)
        min_energy = params.get("min_window_energy_kwh", 10)
        if inverter_energy < min_energy:
            return [
                RuleOutcome(
                    status="not_computable",
                    reason=f"energia_ventana_insuficiente (<{min_energy} kWh)",
                )
            ]

        ratio = abs(inverter_energy - frontier) / inverter_energy
        evidence = {
            "inverter_energy_kwh": round(inverter_energy, 2),
            "frontier_energy_kwh": round(frontier, 2),
            "mismatch_ratio": round(ratio, 4),
            "mismatch_percent": round(ratio * 100, 2),
            "window": f"{day_start:%Y-%m-%d %H:%M}-{day_end:%H:%M}",
        }
        if ratio > params["high_ratio"]:
            return [RuleOutcome(status="firing", severity=Severity.HIGH, evidence=evidence)]
        if ratio > params["alert_ratio"]:
            return [RuleOutcome(status="firing", severity=Severity.MEDIUM, evidence=evidence)]
        return [RuleOutcome(status="ok")]
