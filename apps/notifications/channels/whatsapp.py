"""Canal oficial de WhatsApp Cloud API con destinos de Groups API."""

import os

import requests
from django.conf import settings

from .base import BaseChannel, display_evidence_items, register_channel

TIMEOUT = 10
EVENT_LABELS = {
    "opened": "🚨 ALARMA ACTIVADA",
    "escalated": "⚠️ ALARMA ESCALADA",
    "resolved": "✅ ALARMA RESUELTA",
    "sla_reminder": "⏰ SLA VENCIDO SIN ATENCIÓN",
}
SEVERITY_ICONS = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "⚪",
}


class WhatsAppNotConfigured(Exception):
    """Falta una credencial o un destino necesario para enviar."""


@register_channel
class WhatsAppChannel(BaseChannel):
    kind = "whatsapp"

    def _access_token(self) -> str:
        token = os.environ.get(self.channel.env_key, "")
        if not token:
            raise WhatsAppNotConfigured(
                f"env var {self.channel.env_key!r} sin access token de WhatsApp"
            )
        return token

    def _url(self) -> str:
        phone_number_id = self.channel.whatsapp_phone_number_id or settings.WHATSAPP_PHONE_NUMBER_ID
        if not phone_number_id:
            raise WhatsAppNotConfigured("Canal sin whatsapp_phone_number_id")
        version = settings.WHATSAPP_API_VERSION
        return f"https://graph.facebook.com/{version}/{phone_number_id}/messages"

    def target_id(self, alarm) -> str:
        if self.channel.purpose == self.channel.Purpose.GENERAL_SUMMARY:
            target = self.channel.destination_group_id.strip()
        else:
            zone = getattr(alarm.project, "zone", None) if alarm else None
            target = (zone.whatsapp_group_id or "").strip() if zone and zone.enabled else ""
        if not target:
            raise WhatsAppNotConfigured("No hay group_id de WhatsApp para el destino")
        return target

    @staticmethod
    def text_payload(target: str, body: str) -> dict:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "group",
            "to": target,
            "type": "text",
            "text": {"preview_url": False, "body": body[:4096]},
        }

    def build_payload(self, event: str, alarm) -> dict:
        evidence = alarm.last_evidence or alarm.evidence
        evidence_lines = "\n".join(
            f"• {key}: {value}" for key, value in display_evidence_items(evidence)[:8]
        )
        component = alarm.component_id or alarm.get_component_type_display()
        if alarm.inverter:
            component = f"{alarm.inverter.dev_name} {alarm.component_id}".strip()
        icon = SEVERITY_ICONS.get(alarm.severity, "⚪")
        body = (
            f"{EVENT_LABELS.get(event, event)}\n"
            f"{icon} *{alarm.get_severity_display()}* — {alarm.rule.name}\n"
            f"Alarma: #{alarm.id}\nProyecto: {alarm.project.name}\n"
            f"Componente: {component or 'proyecto'}\n"
            f"Estado: {alarm.get_status_display()}\nDisparada: {alarm.triggered_at}\n"
            f"\nEvidencia:\n{evidence_lines or '• Sin datos'}\n\n"
            f"Comandos: `alarma {alarm.id} ver|reconocer|mantenimiento|finalizar`"
        )
        return self.text_payload(self.target_id(alarm), body)

    def send(self, payload: dict, alarm=None) -> int:
        response = requests.post(
            self._url(),
            json=payload,
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.status_code

    def send_text(self, target: str, body: str) -> int:
        return self.send(self.text_payload(target, body))
