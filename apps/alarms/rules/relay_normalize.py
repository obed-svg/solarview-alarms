"""Normalización del factor de potencia del reconectador.

Única normalización que se conserva del relay: pf puede venir en fracción
(0.95) o en porcentaje (95) según el equipo/firmware → se lleva siempre a
fracción absoluta. La normalización heurística de `kw` (T27) fue retirada en
T35 por decisión del usuario: NUNCA usar `relay.kw` en lógica — cada
reconectador reporta la potencia en unidades distintas y los de firmware
desactualizado además la leen mal (enteros sin sentido). El gate de carga de
la regla 18 es por corriente (historia completa en git: T27 → T35).
"""


def normalize_pf(raw_pf: float | None) -> float | None:
    if raw_pf is None:
        return None
    value = abs(raw_pf)
    if value <= 1:
        return value
    if value <= 100:
        return value / 100  # venía en porcentaje
    return None  # sin interpretación física
