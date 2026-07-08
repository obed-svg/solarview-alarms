"""Dispatcher: decide qué canales notifican un evento de alarma y encola envíos.

Es el `notifier` que se inyecta a engine.evaluate_project. La unicidad
(alarm, channel, event) en NotificationLog hace el despacho idempotente:
re-ejecutar un tick no duplica notificaciones.
"""

import logging

from .models import NotificationChannel, NotificationLog
from .tasks import send_notification

logger = logging.getLogger(__name__)


def notify(alarm, event: str) -> None:
    channels = NotificationChannel.objects.filter(enabled=True)
    for channel in channels:
        if not channel.accepts(alarm.severity):
            continue
        log, created = NotificationLog.objects.get_or_create(
            alarm=alarm, channel=channel, event=event
        )
        if created:
            send_notification.delay(log.id)
        else:
            logger.debug(
                "Evento %s de alarma %s ya notificado por %s", event, alarm.id, channel
            )
