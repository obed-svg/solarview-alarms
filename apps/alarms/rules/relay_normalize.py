"""Normalización heurística de las mediciones del reconectador.

La API no expone marca/modelo y cada reconectador reporta con unidades
distintas (visto: kw=5832.96 en una planta de 1 MW → era W). Sin catálogo de
equipos, se normaliza por PLAUSIBILIDAD FÍSICA con dos anclas independientes:

1. Capacidad instalada del proyecto: la potencia en frontera no puede superar
   ~1.2× la capacidad.
2. Potencia total de inversores (fuente independiente): de día, la frontera
   debe parecerse a la generación.

Regla de oro: si la ambigüedad no se resuelve con las anclas, el valor queda
None y las reglas reportan not_computable — nunca se adivina.
"""

from dataclasses import dataclass

KW_SCALES = (1.0, 0.001)  # candidatos: ya en kW, o en W → ÷1000
CAPACITY_TOLERANCE = 1.2
INVERTER_MATCH_RATIO = 0.5  # |frontera - inversores| / inversores aceptable de día


@dataclass
class NormalizedRelay:
    kw: float | None  # None = unidad no resoluble
    pf: float | None
    kw_scale: float | None = None  # escala aplicada (evidencia/auditoría)
    notes: list[str] | None = None


def normalize_pf(raw_pf: float | None) -> float | None:
    if raw_pf is None:
        return None
    value = abs(raw_pf)
    if value <= 1:
        return value
    if value <= 100:
        return value / 100  # venía en porcentaje
    return None  # sin interpretación física


def normalize_kw(
    raw_kw: float | None,
    capacity_kw: float | None,
    inverter_total_kw: float | None = None,
) -> tuple[float | None, float | None]:
    """(kw_normalizado, escala_aplicada). None si la unidad es ambigua."""
    if raw_kw is None:
        return None, None
    if raw_kw == 0:
        return 0.0, 1.0

    candidates = list(KW_SCALES)
    if capacity_kw:
        candidates = [
            s for s in candidates if 0 <= raw_kw * s <= capacity_kw * CAPACITY_TOLERANCE
        ]
    if len(candidates) == 1:
        return raw_kw * candidates[0], candidates[0]

    # varias escalas plausibles (o capacidad desconocida): desempatar contra
    # la potencia de inversores si hay generación significativa
    if inverter_total_kw and inverter_total_kw > 1:
        best = min(
            candidates or KW_SCALES,
            key=lambda s: abs(raw_kw * s - inverter_total_kw),
        )
        if abs(raw_kw * best - inverter_total_kw) <= inverter_total_kw * INVERTER_MATCH_RATIO:
            return raw_kw * best, best

    return None, None  # ambigüedad irresoluble: no adivinar


def normalize_relay(relay, capacity_kw=None, inverter_total_kw=None) -> NormalizedRelay:
    kw, scale = normalize_kw(relay.kw, capacity_kw, inverter_total_kw)
    notes = []
    if scale == 0.001:
        notes.append("kw venía en W (÷1000)")
    if relay.pf is not None and abs(relay.pf) > 1:
        notes.append("pf venía en % (÷100)")
    return NormalizedRelay(
        kw=kw, pf=normalize_pf(relay.pf), kw_scale=scale, notes=notes or None
    )
