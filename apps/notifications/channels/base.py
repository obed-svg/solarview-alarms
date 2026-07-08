"""Contrato de canal de notificación. Agregar un canal nuevo (Gmail, Slack...)
= una clase nueva registrada por kind; dispatcher y engine no cambian."""

from abc import ABC, abstractmethod
from typing import ClassVar

CHANNEL_REGISTRY: dict[str, type["BaseChannel"]] = {}


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
    def send(self, payload: dict) -> int:
        """Envía. Devuelve HTTP status. Lanza para reintento en fallo transitorio."""

    @abstractmethod
    def target_id(self) -> str:
        """Identificador del destino (channel_id de Discord) para trazabilidad."""
