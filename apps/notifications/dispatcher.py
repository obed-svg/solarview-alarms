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
    # El breach de SLA era una segunda alarma causada precisamente por no
    # reconocer la primera. Se conserva como capacidad interna, pero nunca se
    # publica como una alarma adicional.
    if alarm.rule.code == "alarm_sla_breach":
        logger.debug("Breach SLA %s omitido de notificaciones", alarm.id)
        return
    if not alarm.project.alarms_enabled:
        # T49: autoconsumo (todo lo que no es minigranja) fuera de las alarmas.
        # El fan-out ya no los evalúa; esto ataja alarmas viejas que sigan
        # abiertas (p.ej. las que resuelve o escala check_sla).
        logger.debug(
            "Proyecto %s sin alarmas (no minigranja); no notifico %s", alarm.project_id, event
        )
        return
    channels = NotificationChannel.objects.filter(
        enabled=True, purpose=NotificationChannel.Purpose.REALTIME
    )
    for channel in channels:
        # WhatsApp conserva la política anti-spam: solo publica aperturas.
        # Discord publica también las resoluciones automáticas en el hilo.
        if event != NotificationLog.Event.OPENED and not (
            event == NotificationLog.Event.RESOLVED
            and channel.kind == NotificationChannel.Kind.DISCORD
        ):
            logger.debug("Evento silencioso %s de alarma %s por %s", event, alarm.id, channel)
            continue
        if not channel.accepts(alarm.severity):
            continue
        if channel.kind == NotificationChannel.Kind.WHATSAPP:
            zone = alarm.project.zone
            if not zone or not zone.enabled or not zone.whatsapp_group_id:
                logger.warning(
                    "Proyecto %s sin zona WhatsApp habilitada; no notifico", alarm.project_id
                )
                continue
        log, created = NotificationLog.objects.get_or_create(
            alarm=alarm, channel=channel, event=event
        )
        if created:
            send_notification.delay(log.id)
        else:
            logger.debug("Evento %s de alarma %s ya notificado por %s", event, alarm.id, channel)
