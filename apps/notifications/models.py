import os

from django.db import models

from apps.alarms.models import Severity


class NotificationChannel(models.Model):
    """Canal de notificación. Los secretos nunca viven en DB: se leen del entorno
    al enviar usando el nombre de variable guardado en ``env_key``."""

    class Kind(models.TextChoices):
        DISCORD = "discord", "Discord"
        WHATSAPP = "whatsapp", "WhatsApp"
        GMAIL = "gmail", "Gmail"

    class Purpose(models.TextChoices):
        REALTIME = "realtime", "Alarmas por zona en tiempo real"
        GENERAL_SUMMARY = "general_summary", "Resumen general bajo demanda"

    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    env_key = models.CharField(
        max_length=100,
        default="webhook_discord",
        help_text="Nombre de la variable del .env con el secreto (webhook/credencial)",
    )
    purpose = models.CharField(
        max_length=20,
        choices=Purpose.choices,
        default=Purpose.REALTIME,
    )
    whatsapp_phone_number_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="ID del número de WhatsApp Business que realiza los envíos.",
    )
    destination_group_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Solo resumen general: ID del grupo de WhatsApp de destino.",
    )
    discord_channel_id = models.CharField(
        max_length=50,
        blank=True,
        default="",
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


class WhatsAppInboundMessage(models.Model):
    """Idempotencia y auditoría de comandos recibidos por webhook."""

    message_id = models.CharField(max_length=255, unique=True)
    group_id = models.CharField(max_length=255, db_index=True)
    sender_id = models.CharField(max_length=100, blank=True, default="")
    text = models.TextField(blank=True, default="")
    response_text = models.TextField(blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.group_id}: {self.text[:60]}"


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
        max_length=255,
        blank=True,
        default="",
        help_text="Snapshot del ID de destino al momento del envío (trazabilidad)",
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
