"""Contrato que cumple toda regla de alarma.

Cada alarma del catálogo es una clase BaseRule registrada con @register y unida
a su fila AlarmRule por `code`. El engine no conoce POA ni inversores: solo
recorre las clases registradas y procesa sus RuleOutcome.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Literal

Status = Literal["ok", "firing", "not_computable"]


@dataclass
class RuleOutcome:
    """Resultado de evaluar UNA regla sobre UN componente.

    - ok: condición verificada y normal → resuelve alarma abierta de inmediato.
    - firing: condición anormal verificada → upsert de alarma.
    - not_computable: no se pudo verificar → NO tocar alarmas (ni crear ni resolver).
    """

    status: Status = "ok"
    dedup_suffix: str = ""  # "" = proyecto; "inv:1571"; "inv:1571:pv3"
    severity: str | None = None  # None = usa default_severity de la regla
    evidence: dict = field(default_factory=dict)
    reason: str = ""  # exclusión aplicada o input faltante
    inverter_external_id: int | None = None  # para el FK Alarm.inverter
    component_id: str = ""  # "pv3", código de medidor, ...


RULES: dict[str, type["BaseRule"]] = {}


def register(cls: type["BaseRule"]) -> type["BaseRule"]:
    RULES[cls.code] = cls
    return cls


class BaseRule(ABC):
    """Una regla por tipo de alarma. `phase` define el orden dentro del tick:

    1 = comunicación (inverter_comm_lost, meter_comm_lost, weather_comm_lost)
    2 = calidad de datos (pr/availability_inputs_missing, data_frozen, poa_invalid)
    3 = eléctricas/operativas (el resto)

    Las fases posteriores consultan ctx.flag_active() para sus exclusiones.
    """

    code: ClassVar[str]
    phase: ClassVar[int] = 3

    @abstractmethod
    def evaluate(self, ctx) -> list[RuleOutcome]:
        """Evalúa la regla para todos sus componentes usando datos de ctx."""
