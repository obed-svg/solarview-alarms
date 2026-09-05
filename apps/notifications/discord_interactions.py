"""Discord interaction endpoint for alarm actions."""

import json

from django.conf import settings
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from apps.alarms.models import Alarm
from apps.alarms.services import set_rule_excluded_for_project
from apps.notifications.commands import build_discord_summary, find_projects, resolve_project
from apps.notifications.models import NotificationChannel

EPHEMERAL = 64


def reply(content, *, ephemeral=True):
    data = {"content": content}
    if ephemeral:
        data["flags"] = EPHEMERAL
    return JsonResponse({"type": 4, "data": data})


def summary_reply(summary, project=None):
    title = f"Resumen de {project.name}" if project else "Resumen general de alarmas"
    return JsonResponse(
        {
            "type": 4,
            "data": {
                "embeds": [{"title": title, "description": summary}],
            },
        }
    )


def parse_command(data):
    options = data.get("options", [])
    if not options:
        return "", {}
    subcommand = options[0]
    values = {item["name"]: item.get("value") for item in subcommand.get("options", [])}
    return subcommand.get("name", ""), values


def parse_summary_page(data):
    """Lee pagina tanto de una opción directa como de una subcommand anidada."""
    options = data.get("options", [])
    if options and options[0].get("type") == 1:
        options = options[0].get("options", [])
    value = next((item.get("value", 1) for item in options if item.get("name") == "pagina"), 1)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_summary_project(data):
    """Lee el valor de proyecto de las opciones directas de /resumen."""
    options = data.get("options", [])
    if options and options[0].get("type") == 1:
        options = options[0].get("options", [])
    return next(
        (str(item.get("value", "")).strip() for item in options if item.get("name") == "proyecto"),
        "",
    )


def autocomplete_reply(data):
    options = data.get("options", [])
    focused = next((item for item in options if item.get("focused")), {})
    if focused.get("name") != "proyecto":
        return JsonResponse({"type": 8, "data": {"choices": []}})
    choices = [
        {"name": project.name[:100], "value": str(project.external_id)}
        for project in find_projects(str(focused.get("value", "")))
    ]
    return JsonResponse({"type": 8, "data": {"choices": choices}})


def verify_request(request):
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    if not settings.DISCORD_PUBLIC_KEY or not signature or not timestamp:
        return False
    try:
        VerifyKey(bytes.fromhex(settings.DISCORD_PUBLIC_KEY)).verify(
            timestamp.encode() + request.body, bytes.fromhex(signature)
        )
    except (BadSignatureError, ValueError):
        return False
    return True


@csrf_exempt
@require_POST
def interactions(request):
    if not verify_request(request):
        return HttpResponseBadRequest("Firma de Discord inválida")
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("JSON inválido")
    if payload.get("type") == 1:
        return JsonResponse({"type": 1})
    interaction_type = payload.get("type")
    command_name = payload.get("data", {}).get("name")
    if interaction_type not in {2, 4} or command_name not in {"alarma", "resumen"}:
        return reply("Comando no reconocido.")
    if settings.DISCORD_GUILD_ID and payload.get("guild_id") != settings.DISCORD_GUILD_ID:
        return reply("Comando no autorizado en este servidor.")

    if interaction_type == 4:
        return autocomplete_reply(payload["data"])

    if command_name == "resumen":
        channel = (
            NotificationChannel.objects.filter(
                kind=NotificationChannel.Kind.DISCORD,
                purpose=NotificationChannel.Purpose.REALTIME,
                enabled=True,
            )
            .order_by("id")
            .first()
        )
        if not channel or str(payload.get("channel_id")) != channel.discord_channel_id:
            return reply("El resumen debe solicitarse en el canal principal, no dentro de un hilo.")
        page = parse_summary_page(payload["data"])
        if page < 1:
            return reply("La página debe ser un número entero positivo.")
        project_query = parse_summary_project(payload["data"])
        project = None
        if project_query:
            project, _ = resolve_project(project_query)
            if project is None:
                return reply(
                    "No encontré un único proyecto con ese valor. "
                    "Selecciona una de las sugerencias de Discord."
                )
        return summary_reply(build_discord_summary(page, project), project)

    action, options = parse_command(payload["data"])
    alarm_id = options.get("id")
    if not isinstance(alarm_id, int):
        return reply("Debes indicar un ID de alarma válido.")
    try:
        with transaction.atomic():
            alarm = (
                Alarm.objects.select_for_update().select_related("project", "rule").get(id=alarm_id)
            )
            if (
                not alarm.project.discord_thread_id
                or payload.get("channel_id") != alarm.project.discord_thread_id
            ):
                return reply("Esta alarma solo puede operarse desde el hilo de su proyecto.")
            if action == "ver":
                return reply(
                    f"Alarma #{alarm.id}: **{alarm.rule.name}** — "
                    f"{alarm.get_status_display()}\nÚltima observación: {alarm.last_seen_at}"
                )
            user_id = payload.get("member", {}).get("user", {}).get("id", "desconocido")
            actor = f"discord+{user_id}@invalid.local"
            if action in {"excluir", "incluir"}:
                resolved = set_rule_excluded_for_project(
                    alarm.rule,
                    alarm.project,
                    excluded=action == "excluir",
                    actor=actor,
                    source="discord",
                )
                if action == "excluir":
                    return reply(
                        f"{alarm.rule.name} quedó excluida para {alarm.project.name}. "
                        f"Alarmas abiertas cerradas: {resolved}."
                    )
                return reply(f"{alarm.rule.name} volvió a habilitarse para {alarm.project.name}.")

            if action == "reconocer":
                if alarm.status != Alarm.Status.ACTIVE:
                    status = alarm.get_status_display().lower()
                    return reply(f"La alarma #{alarm.id} ya está {status}.")
                alarm.status = Alarm.Status.ACKNOWLEDGED
                alarm.acknowledged_at = timezone.now()
                alarm.acknowledged_by = actor
                alarm.save(update_fields=["status", "acknowledged_at", "acknowledged_by"])
                return reply(f"Alarma #{alarm.id} reconocida.")
            if action == "resolver":
                if alarm.status == Alarm.Status.RESOLVED:
                    return reply(f"La alarma #{alarm.id} ya estaba resuelta.")
                alarm.status = Alarm.Status.RESOLVED
                alarm.resolved_at = timezone.now()
                alarm.resolution_type = Alarm.ResolutionType.MANUAL
                alarm.save(update_fields=["status", "resolved_at", "resolution_type"])
                return reply(f"Alarma #{alarm.id} resuelta manualmente.")
    except Alarm.DoesNotExist:
        return reply(f"No existe la alarma #{alarm_id}.")
    return reply("Acción no reconocida.")
