import requests

from apps.alarms.models import Severity

from .base import BaseChannel, register_channel

SEVERITY_COLORS = {
    Severity.CRITICAL: 0xE74C3C,  # rojo
    Severity.HIGH: 0xE67E22,      # naranja
    Severity.MEDIUM: 0xF1C40F,    # amarillo
    Severity.LOW: 0x95A5A6,       # gris
}

EVENT_LABELS = {
    "opened": "🔴 Alarma ACTIVADA",
    "escalated": "⚠️ Alarma ESCALADA",
    "resolved": "✅ Alarma RESUELTA",
    "sla_reminder": "⏰ SLA vencido sin atención",
}

TIMEOUT = 10


class WebhookNotConfigured(Exception):
    """La env var del canal no existe: error de configuración, no reintentar."""


@register_channel
class DiscordChannel(BaseChannel):
    kind = "discord"

    def _url(self) -> str:
        url = self.channel.webhook_url
        if not url:
            raise WebhookNotConfigured(
                f"env var {self.channel.env_key!r} sin webhook configurado"
            )
        return url

    def build_payload(self, event: str, alarm) -> dict:
        evidence = alarm.last_evidence or alarm.evidence
        evidence_lines = "\n".join(f"- {k}: {v}" for k, v in list(evidence.items())[:8])
        component = alarm.component_id or alarm.get_component_type_display()
        if alarm.inverter:
            component = f"{alarm.inverter.dev_name} {alarm.component_id}".strip()

        embed = {
            "title": f"[{alarm.get_severity_display()}] {alarm.rule.name}",
            "description": alarm.rule.description[:300],
            "color": SEVERITY_COLORS.get(alarm.severity, 0x95A5A6),
            "fields": [
                {"name": "Evento", "value": EVENT_LABELS.get(event, event), "inline": True},
                {"name": "Proyecto", "value": str(alarm.project.name), "inline": True},
                {"name": "Componente", "value": component or "proyecto", "inline": True},
                {"name": "Disparada", "value": str(alarm.triggered_at), "inline": True},
                {"name": "Evidencia", "value": evidence_lines or "(sin datos)", "inline": False},
            ],
        }
        return {"embeds": [embed]}

    def send(self, payload: dict) -> int:
        response = requests.post(self._url(), json=payload, timeout=TIMEOUT)
        response.raise_for_status()
        return response.status_code

    def target_id(self) -> str:
        """channel_id de Discord, cacheado en el modelo. Un GET al webhook URL
        devuelve metadata del webhook incluyendo el canal al que apunta."""
        if self.channel.discord_channel_id:
            return self.channel.discord_channel_id
        response = requests.get(self._url(), timeout=TIMEOUT)
        response.raise_for_status()
        channel_id = str(response.json().get("channel_id", ""))
        if channel_id:
            self.channel.discord_channel_id = channel_id
            self.channel.save(update_fields=["discord_channel_id"])
        return channel_id
