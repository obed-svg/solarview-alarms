import os

from django.db import models

from apps.alarms.models import Severity


class NotificationChannel(models.Model):
    """Canal de notificación. El secreto (webhook URL) NUNCA vive en DB: se lee
    del .env al momento del envío usando el nombre de variable en env_key."""

    class Kind(models.TextChoices):
        DISCORD = "discord", "Discord"
        GMAIL = "gmail", "Gmail"

    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    env_key = models.CharField(
        max_length=100,
        default="webhook_discord",
        help_text="Nombre de la variable del .env con el secreto (webhook/credencial)",
    )
    discord_channel_id = models.CharField(
        max_length=50, blank=True, default="",
        help_text="Cacheado con GET al webhook; snapshot en cada NotificationLog",
    )
    recipients = models.TextField(
        blank=True, default="", help_text="Solo gmail: correos separados por coma"
    )
    min_severity = models.CharField(
        max_length=10, choices=Severity.choices, default=Severity.MEDIUM
    )
    enabled = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.kind})"

    @property
    def webhook_url(self) -> str:
        """Secreto leído del entorno en runtime (cargado del .env por django-environ)."""
        return os.environ.get(self.env_key, "")

    def accepts(self, severity: str) -> bool:
        return Severity.rank(severity) >= Severity.rank(self.min_severity)


class NotificationLog(models.Model):
    """Registro de cada notificación. La unicidad (alarm, channel, event) hace
    idempotente el despacho: un evento de una alarma se notifica UNA vez por canal."""

    class Event(models.TextChoices):
        OPENED = "opened", "Abierta"
        ESCALATED = "escalated", "Escalada"
        RESOLVED = "resolved", "Resuelta"
        SLA_REMINDER = "sla_reminder", "Recordatorio SLA"

    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        SENT = "sent", "Enviada"
        FAILED = "failed", "Fallida"

    alarm = models.ForeignKey(
        "alarms.Alarm", on_delete=models.CASCADE, related_name="notifications"
    )
    channel = models.ForeignKey(NotificationChannel, on_delete=models.PROTECT)
    event = models.CharField(max_length=15, choices=Event.choices)
    target_channel_id = models.CharField(
        max_length=50, blank=True, default="",
        help_text="Snapshot del channel ID de Discord al momento del envío (trazabilidad)",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    payload = models.JSONField(default=dict, blank=True)
    response_status = models.PositiveIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["alarm", "channel", "event"],
                condition=~models.Q(event="sla_reminder"),
                name="uniq_notification_per_event",
            ),
        ]

    def __str__(self):
        return f"{self.event} de alarma {self.alarm_id} por {self.channel} ({self.status})"
