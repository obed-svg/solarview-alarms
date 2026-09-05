from urllib.parse import quote

import requests

from apps.alarms.models import Severity

from .base import BaseChannel, display_evidence_items, register_channel

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


class DiscordRateLimited(requests.RequestException):
    """429 del webhook (límite ~30 msg/min). Lleva el retry_after que Discord
    indica para reintentar exactamente cuando toca (T43)."""

    def __init__(self, retry_after: float):
        super().__init__(f"Discord rate limit: reintentar en {retry_after:.1f}s")
        self.retry_after = retry_after


class WebhookNotConfigured(Exception):
    """La env var del canal no existe: error de configuración, no reintentar."""


@register_channel
class DiscordChannel(BaseChannel):
    kind = "discord"

    def _url(self, alarm=None) -> str:
        """Webhook del canal, con `?thread_id=` del proyecto cuando lo tiene (T49).

        Un solo webhook/canal para toda la flota; el hilo lo elige la alarma.
        Sin hilo configurado el mensaje cae al canal raíz: perder la alarma
        sería peor que publicarla fuera de su hilo.
        """
        url = self.channel.webhook_url
        if not url:
            raise WebhookNotConfigured(
                f"env var {self.channel.env_key!r} sin webhook configurado"
            )
        thread_id = self._thread_id(alarm)
        if thread_id:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}thread_id={quote(thread_id, safe='')}"
        return url

    @staticmethod
    def _thread_id(alarm) -> str:
        if alarm is None:
            return ""
        return (alarm.project.discord_thread_id or "").strip()

    def build_payload(self, event: str, alarm) -> dict:
        evidence = alarm.last_evidence or alarm.evidence
        evidence_lines = "\n".join(
            f"- {key}: {value}" for key, value in display_evidence_items(evidence)[:8]
        )
        component = alarm.component_id or alarm.get_component_type_display()
        if alarm.inverter:
            component = f"{alarm.inverter.dev_name} {alarm.component_id}".strip()

        embed = {
            "title": f"[{alarm.get_severity_display()}] {alarm.rule.name}",
            "description": alarm.rule.description[:300],
            "color": SEVERITY_COLORS.get(alarm.severity, 0x95A5A6),
            "fields": [
                {"name": "Evento", "value": EVENT_LABELS.get(event, event), "inline": True},
                {"name": "Alarma", "value": f"#{alarm.id}", "inline": True},
                {"name": "Estado", "value": alarm.get_status_display(), "inline": True},
                {"name": "Proyecto", "value": str(alarm.project.name), "inline": True},
                {"name": "Componente", "value": component or "proyecto", "inline": True},
                {"name": "Disparada", "value": str(alarm.triggered_at), "inline": True},
                {"name": "Evidencia", "value": evidence_lines or "(sin datos)", "inline": False},
            ],
        }
        return {"embeds": [embed]}

    def send(self, payload: dict, alarm) -> int:
        response = requests.post(self._url(alarm), json=payload, timeout=TIMEOUT)
        if response.status_code == 429:
            try:
                retry_after = float(response.json().get("retry_after", 5.0))
            except (ValueError, AttributeError):
                retry_after = float(response.headers.get("Retry-After", 5.0))
            raise DiscordRateLimited(retry_after)
        response.raise_for_status()
        return response.status_code

    def target_id(self, alarm) -> str:
        """Destino real del mensaje: el hilo del proyecto si lo hay (T49), si no
        el channel_id del webhook, cacheado en el modelo. Un GET al webhook URL
        (sin thread_id) devuelve metadata del webhook incluyendo su canal."""
        thread_id = self._thread_id(alarm)
        if thread_id:
            return thread_id
        if self.channel.discord_channel_id:
            return self.channel.discord_channel_id
        response = requests.get(self._url(), timeout=TIMEOUT)
        response.raise_for_status()
        channel_id = str(response.json().get("channel_id", ""))
        if channel_id:
            self.channel.discord_channel_id = channel_id
            self.channel.save(update_fields=["discord_channel_id"])
        return channel_id
