import logging

import requests
from celery import shared_task
from django.utils import timezone

from .channels.base import CHANNEL_REGISTRY
from .channels.discord import DiscordRateLimited, WebhookNotConfigured
from .models import NotificationLog

logger = logging.getLogger(__name__)


@shared_task(
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=8,
    rate_limit="25/m",  # T43: el webhook de Discord tolera ~30/min
)
def send_notification(log_id: int) -> None:
    log = (
        NotificationLog.objects.select_related("channel", "alarm", "alarm__rule",
                                               "alarm__project", "alarm__inverter")
        .get(id=log_id)
    )
    if log.status == NotificationLog.Status.SENT:
        return  # idempotencia ante re-entregas del broker

    channel_impl = CHANNEL_REGISTRY[log.channel.kind](log.channel)
    log.attempts += 1

    try:
        target = channel_impl.target_id()
        payload = channel_impl.build_payload(log.event, log.alarm)
        status_code = channel_impl.send(payload)
    except WebhookNotConfigured as exc:
        # error de configuración: reintentar no lo arregla
        log.status = NotificationLog.Status.FAILED
        log.last_error = str(exc)
        log.save(update_fields=["status", "last_error", "attempts"])
        logger.error("Notificación %s sin webhook: %s", log_id, exc)
        return
    except DiscordRateLimited as exc:
        # T43: reintentar exactamente cuando Discord lo indica (no backoff
        # ciego — la ola del arranque agotaba los 5 reintentos y perdía 274
        # notificaciones). El rate_limit de 25/min evita volver a chocar.
        log.last_error = str(exc)
        log.save(update_fields=["last_error", "attempts"])
        raise send_notification.retry(
            exc=exc, countdown=exc.retry_after + 1, max_retries=10
        ) from exc
    except requests.RequestException as exc:
        log.status = NotificationLog.Status.FAILED
        log.last_error = str(exc)
        log.save(update_fields=["status", "last_error", "attempts"])
        raise  # autoretry con backoff

    log.status = NotificationLog.Status.SENT
    log.target_channel_id = target
    log.payload = payload
    log.response_status = status_code
    log.sent_at = timezone.now()
    log.last_error = ""
    log.save(
        update_fields=[
            "status", "target_channel_id", "payload", "response_status",
            "sent_at", "last_error", "attempts",
        ]
    )
