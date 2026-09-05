"""Contrato de canal de notificación. Agregar un canal nuevo (Gmail, Slack...)
= una clase nueva registrada por kind; dispatcher y engine no cambian."""

from abc import ABC, abstractmethod
from typing import ClassVar

CHANNEL_REGISTRY: dict[str, type["BaseChannel"]] = {}


def display_evidence_items(evidence: dict) -> list[tuple[str, object]]:
    """Convierte evidencia técnica a una presentación legible en los canales.

    ``mismatch_ratio`` se conserva en la evidencia almacenada para cálculos y
    auditoría, pero es redundante para una persona: ``0.5245`` equivale a
    ``52.45 %``. Por eso solo se publica su representación porcentual.
    """
    items = []
    for key, value in evidence.items():
        if key == "mismatch_ratio":
            continue
        if key == "mismatch_percent":
            try:
                value = f"{float(value):.2f}%"
            except (TypeError, ValueError):
                value = f"{value}%"
            key = "diferencia_absoluta"
        items.append((key, value))
    return items


def register_channel(cls: type["BaseChannel"]) -> type["BaseChannel"]:
    CHANNEL_REGISTRY[cls.kind] = cls
    return cls


class BaseChannel(ABC):
    kind: ClassVar[str]

    def __init__(self, channel_model):
        self.channel = channel_model  # fila NotificationChannel

    @abstractmethod
    def build_payload(self, event: str, alarm) -> dict:
        """Payload exacto a enviar (se guarda en NotificationLog.payload)."""

    @abstractmethod
    def send(self, payload: dict, alarm) -> int:
        """Envía. Devuelve HTTP status. Lanza para reintento en fallo transitorio.

        Recibe la alarma porque el destino depende de ella: desde T49 cada
        proyecto tiene su propio hilo dentro del mismo canal.
        """

    @abstractmethod
    def target_id(self, alarm) -> str:
        """Identificador del destino (hilo o canal de Discord) para trazabilidad."""
