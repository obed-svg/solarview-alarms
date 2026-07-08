"""Helpers compartidos entre reglas de fase 3."""

from apps.alarms.context import Unavailable

MIN_WINDOW_POINTS = 3


def poa_sustained_above(ctx, params) -> bool | None:
    """POA > umbral sostenida en la ventana de persistencia.

    True/False, o None si no es verificable (POA caída o ventana escasa).
    """
    poa = ctx.poa_series()
    if isinstance(poa, Unavailable):
        return None
    window = ctx.series_window(
        poa, params["persistence_minutes"], params.get("data_lag_minutes", 5)
    )
    values = [v for v in window.values() if v is not None]
    if len(values) < MIN_WINDOW_POINTS:
        return None
    return min(values) > params["poa_min_wm2"]


def dev_name_to_external_id(inverters) -> dict[str, int]:
    """measurements-dc indexa por dev_name; las alarmas necesitan external_id."""
    return {inv.dev_name: inv.id for inv in inverters}


def window_average(ctx, series_data, minutes, lag) -> float | None:
    values = [
        v for v in ctx.series_window(series_data, minutes, lag).values() if v is not None
    ]
    if len(values) < MIN_WINDOW_POINTS:
        return None
    return sum(values) / len(values)
