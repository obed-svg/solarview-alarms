"""Webhook de WhatsApp Cloud API: verificación y comandos de grupos."""

import hashlib
import hmac
import json

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .commands import execute_group_command
from .models import WhatsAppInboundMessage
from .tasks import send_whatsapp_text


def _valid_signature(request) -> bool:
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not settings.WHATSAPP_APP_SECRET or not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(), request.body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


def _group_id(value: dict, message: dict) -> str:
    context = message.get("context") or {}
    candidate = (
        message.get("group_id")
        or context.get("group_id")
        or value.get("group_id")
        or (context.get("from") if str(context.get("from", "")).endswith("@g.us") else "")
    )
    return str(candidate or "")


def _messages(payload: dict):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            for message in value.get("messages", []):
                yield value, message


@csrf_exempt
def whatsapp_webhook(request):
    if request.method == "GET":
        if (
            request.GET.get("hub.mode") == "subscribe"
            and hmac.compare_digest(
                request.GET.get("hub.verify_token", ""), settings.WHATSAPP_VERIFY_TOKEN
            )
        ):
            return HttpResponse(request.GET.get("hub.challenge", ""))
        return HttpResponseForbidden("Token de verificación inválido")
    if request.method != "POST":
        return HttpResponse(status=405)
    if not _valid_signature(request):
        return HttpResponseForbidden("Firma de WhatsApp inválida")
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON inválido"}, status=400)

    for value, message in _messages(payload):
        if message.get("type") != "text":
            continue
        message_id = str(message.get("id", ""))
        group_id = _group_id(value, message)
        if not message_id or not group_id:
            continue
        inbound, created = WhatsAppInboundMessage.objects.get_or_create(
            message_id=message_id,
            defaults={
                "group_id": group_id,
                "sender_id": str(message.get("from", "")),
                "text": str((message.get("text") or {}).get("body", "")),
            },
        )
        if not created:
            continue
        result = execute_group_command(inbound.group_id, inbound.sender_id, inbound.text)
        if result:
            inbound.response_text = result.text
            send_whatsapp_text.delay(result.channel_id, result.target_group_id, result.text)
        inbound.processed_at = timezone.now()
        inbound.save(update_fields=["response_text", "processed_at"])
    return JsonResponse({"status": "received"})
