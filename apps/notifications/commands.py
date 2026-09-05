"""Comandos de WhatsApp autorizados por el grupo de cada zona."""

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass

from django.utils import timezone

from apps.alarms.models import Alarm, Severity
from apps.alarms.services import (
    InvalidAlarmTransition,
    set_rule_excluded_for_project,
    transition_alarm,
)
from apps.plants.models import Project, Zone

from .models import NotificationChannel

ALARM_COMMAND = re.compile(
    r"^/?alarma\s+#?(?P<id>\d+)\s+"
    r"(?P<action>ver|reconocer|mantenimiento|finalizar|resolver|excluir|incluir)$",
    re.IGNORECASE,
)
SHORT_COMMAND = re.compile(
    r"^/?(?P<action>ver|reconocer|mantenimiento|finalizar|resolver|excluir|incluir)\s+#?(?P<id>\d+)$",
    re.IGNORECASE,
)
SUMMARY_PROJECT_COMMAND = re.compile(
    r"^/?resumen\s+proyecto(?:\s*:\s*|\s+)(?P<query>.+?)\s*$",
    re.IGNORECASE,
)
ACTION_STATUS = {
    "reconocer": Alarm.Status.ACKNOWLEDGED,
    "mantenimiento": Alarm.Status.IN_MAINTENANCE,
    "finalizar": Alarm.Status.RESOLVED,
    "resolver": Alarm.Status.RESOLVED,
}
HELP = (
    "Comandos disponibles:\n"
    "• `alarma <id> ver`\n"
    "• `alarma <id> reconocer`\n"
    "• `alarma <id> mantenimiento`\n"
    "• `alarma <id> finalizar`\n"
    "• `alarma <id> excluir` — deja de evaluar esa regla para el proyecto\n"
    "• `alarma <id> incluir` — vuelve a habilitarla"
)
SUMMARY_HELP = (
    "Comandos disponibles:\n"
    "• `resumen` — todos los proyectos con alarmas\n"
    "• `resumen proyecto <nombre>` — un proyecto específico"
)


@dataclass(frozen=True)
class CommandResponse:
    channel_id: int
    target_group_id: str
    text: str


def _whatsapp_channel(purpose: str):
    return (
        NotificationChannel.objects.filter(
            kind=NotificationChannel.Kind.WHATSAPP,
            purpose=purpose,
            enabled=True,
        )
        .order_by("id")
        .first()
    )


def _alarm_detail(alarm: Alarm) -> str:
    return (
        f"Alarma #{alarm.id}: *{alarm.rule.name}*\n"
        f"Proyecto: {alarm.project.name}\n"
        f"Severidad: {alarm.get_severity_display()}\n"
        f"Estado: {alarm.get_status_display()}\n"
        f"Última observación: {timezone.localtime(alarm.last_seen_at):%Y-%m-%d %H:%M}"
    )


def _normalized(value: str) -> str:
    """Normaliza nombres para permitir búsquedas parciales sin tildes."""
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value).casefold()
        if not unicodedata.combining(character)
    ).strip()


def find_projects(query: str, limit: int = 25) -> list[Project]:
    """Busca proyectos monitoreados y prioriza coincidencias más naturales."""
    needle = _normalized(query)
    if not needle:
        return []

    matches = []
    for project in Project.objects.alarmable().order_by("name", "external_id"):
        name = _normalized(project.name)
        if needle not in name and needle != str(project.external_id):
            continue
        if needle == str(project.external_id) or needle == name:
            rank = 0
        elif name.startswith(needle):
            rank = 1
        elif any(word.startswith(needle) for word in name.split()):
            rank = 2
        else:
            rank = 3
        matches.append((rank, name, project.external_id, project))
    matches.sort(key=lambda item: item[:3])
    return [item[3] for item in matches[:limit]]


def resolve_project(query: str) -> tuple[Project | None, list[Project]]:
    """Resuelve una selección única o devuelve candidatos para sugerir."""
    matches = find_projects(query)
    if len(matches) == 1:
        return matches[0], matches
    normalized_query = _normalized(query)
    exact = [
        project
        for project in matches
        if _normalized(project.name) == normalized_query
        or str(project.external_id) == normalized_query
    ]
    return (exact[0], matches) if len(exact) == 1 else (None, matches)


def execute_zone_command(group_id: str, sender_id: str, text: str) -> CommandResponse | None:
    """Ejecuta solo si el grupo corresponde a la zona de la alarma indicada."""
    try:
        zone = Zone.objects.get(whatsapp_group_id=group_id, enabled=True)
    except Zone.DoesNotExist:
        return None
    channel = _whatsapp_channel(NotificationChannel.Purpose.REALTIME)
    if channel is None:
        return None

    command = text.strip()
    if command.lower() in {"ayuda", "/ayuda"}:
        return CommandResponse(channel.id, group_id, HELP)
    match = ALARM_COMMAND.match(command) or SHORT_COMMAND.match(command)
    if not match:
        if command.lower().startswith(("alarma ", "/alarma ")):
            return CommandResponse(channel.id, group_id, HELP)
        return None

    alarm_id = int(match.group("id"))
    action = match.group("action").lower()
    try:
        alarm = Alarm.objects.select_related("rule", "project").get(id=alarm_id, project__zone=zone)
    except Alarm.DoesNotExist:
        return CommandResponse(
            channel.id,
            group_id,
            f"La alarma #{alarm_id} no existe o no pertenece a la zona {zone.name}.",
        )

    if action == "ver":
        return CommandResponse(channel.id, group_id, _alarm_detail(alarm))

    actor = f"whatsapp+{sender_id or 'desconocido'}@invalid.local"
    if action in {"excluir", "incluir"}:
        resolved = set_rule_excluded_for_project(
            alarm.rule, alarm.project, excluded=action == "excluir", actor=actor, source="whatsapp"
        )
        if action == "excluir":
            return CommandResponse(
                channel.id,
                group_id,
                f"✅ *{alarm.rule.name}* quedó excluida para {alarm.project.name}. "
                f"Alarmas abiertas cerradas: {resolved}.",
            )
        return CommandResponse(
            channel.id,
            group_id,
            f"✅ *{alarm.rule.name}* volvió a habilitarse para {alarm.project.name}.",
        )
    try:
        alarm = transition_alarm(alarm.id, ACTION_STATUS[action], actor, "whatsapp")
    except InvalidAlarmTransition as exc:
        return CommandResponse(channel.id, group_id, f"No se realizó el cambio: {exc}")
    return CommandResponse(
        channel.id,
        group_id,
        f"✅ Alarma #{alarm.id} actualizada: *{alarm.get_status_display()}*.\n"
        f"Proyecto: {alarm.project.name}",
    )


def build_general_summary(project: Project | None = None) -> str:
    """Construye el resumen de WhatsApp general o de un proyecto concreto."""
    alarms = list(
        Alarm.objects.exclude(status=Alarm.Status.RESOLVED)
        .exclude(rule__code="alarm_sla_breach")
        .filter(**({"project": project} if project else {}))
        .select_related("project", "project__zone")
        .order_by("project__name", "-severity")
    )
    if not alarms:
        if project:
            return f"📊 *Resumen de {project.name}*\n\n✅ El proyecto no tiene alarmas abiertas."
        return "📊 *Resumen general de alarmas*\n\nNo hay proyectos con alarmas abiertas."

    by_project = defaultdict(list)
    for alarm in alarms:
        by_project[alarm.project].append(alarm)
    by_level = defaultdict(list)
    for project, project_alarms in by_project.items():
        highest = max(project_alarms, key=lambda item: Severity.rank(item.severity)).severity
        by_level[highest].append((project, project_alarms))

    icons = {
        Severity.CRITICAL: "🔴",
        Severity.HIGH: "🟠",
        Severity.MEDIUM: "🟡",
        Severity.LOW: "⚪",
    }
    title = f"📊 *Resumen de {project.name}*" if project else "📊 *Resumen general de alarmas*"
    lines = [
        title,
        f"Actualizado: {timezone.localtime():%Y-%m-%d %H:%M}",
        f"Proyectos afectados: {len(by_project)} | Alarmas abiertas: {len(alarms)}",
    ]
    for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        projects = by_level.get(severity, [])
        if not projects:
            continue
        lines.append(
            f"\n{icons[severity]} *{Severity(severity).label.upper()}* ({len(projects)} proyectos)"
        )
        for project, project_alarms in sorted(projects, key=lambda item: item[0].name):
            severity_counts = Counter(item.severity for item in project_alarms)
            status_counts = Counter(item.status for item in project_alarms)
            detail = ", ".join(
                f"{count} {Severity(level).label.lower()}"
                for level, count in severity_counts.items()
            )
            states = ", ".join(
                f"{count} {Alarm.Status(status).label.lower()}"
                for status, count in status_counts.items()
            )
            zone = project.zone.name if project.zone else "Sin zona"
            lines.append(f"• {project.name} [{zone}]: {detail} | {states}")
    return "\n".join(lines)[:4096]


def build_discord_summary(page: int = 1, project: Project | None = None) -> str:
    """Resumen detallado y paginado para Discord, con IDs de alarma."""
    alarms = list(
        Alarm.objects.exclude(status=Alarm.Status.RESOLVED)
        .exclude(rule__code="alarm_sla_breach")
        .filter(**({"project": project} if project else {}))
        .select_related("project", "project__zone", "rule")
        .order_by("project__name", "-severity", "id")
    )
    if not alarms:
        if project:
            return f"📊 **Resumen de {project.name}**\n\n✅ El proyecto no tiene alarmas abiertas."
        return "📊 **Resumen general de alarmas**\n\nNo hay proyectos con alarmas abiertas."

    by_project = defaultdict(list)
    for alarm in alarms:
        by_project[alarm.project].append(alarm)

    icons = {
        Severity.CRITICAL: "🔴",
        Severity.HIGH: "🟠",
        Severity.MEDIUM: "🟡",
        Severity.LOW: "⚪",
    }
    projects = []
    for project, project_alarms in by_project.items():
        highest = max(project_alarms, key=lambda item: Severity.rank(item.severity)).severity
        projects.append((highest, project, project_alarms))
    projects.sort(key=lambda item: (-Severity.rank(item[0]), item[1].name))

    blocks = []
    for highest, project, project_alarms in projects:
        zone = project.zone.name if project.zone else "Sin zona"
        details = "; ".join(
            f"#{alarm.id} — {alarm.rule.name} ({alarm.get_severity_display().lower()}, "
            f"{alarm.get_status_display().lower()})"
            for alarm in project_alarms
        )
        blocks.append(f"{icons[highest]} **{project.name}** [{zone}]\n{details}")

    title = f"📊 **Resumen de {project.name}**" if project else "📊 **Resumen general de alarmas**"
    prefix = (
        f"{title}\n"
        f"Actualizado: {timezone.localtime():%Y-%m-%d %H:%M}\n"
        f"Proyectos afectados: {len(by_project)} | Alarmas abiertas: {len(alarms)}\n\n"
    )
    pages = []
    current = prefix
    for block in blocks:
        candidate = f"{current}{block}\n\n"
        if len(candidate) > 3900 and current != prefix:
            pages.append(current.rstrip())
            current = prefix + block + "\n\n"
        else:
            current = candidate
    pages.append(current.rstrip())

    page = max(1, min(page, len(pages)))
    next_page = min(page + 1, len(pages))
    project_hint = f" proyecto:{project.name}" if project else ""
    continuation = (
        f"\n\n_Página {page}/{len(pages)} — usa `/resumen{project_hint} "
        f"pagina:{next_page}` para continuar._"
        if page < len(pages)
        else f"\n\n_Página {page}/{len(pages)}._"
    )
    return pages[page - 1] + continuation


def execute_group_command(group_id: str, sender_id: str, text: str) -> CommandResponse | None:
    general = NotificationChannel.objects.filter(
        kind=NotificationChannel.Kind.WHATSAPP,
        purpose=NotificationChannel.Purpose.GENERAL_SUMMARY,
        destination_group_id=group_id,
        enabled=True,
    ).first()
    if general:
        command = text.strip()
        if command.lower() in {"resumen", "/resumen"}:
            response = build_general_summary()
        elif match := SUMMARY_PROJECT_COMMAND.match(command):
            query = match.group("query")
            project, suggestions = resolve_project(query)
            if project:
                response = build_general_summary(project)
            elif suggestions:
                names = "\n".join(f"• {item.name}" for item in suggestions[:10])
                response = (
                    f"Encontré varios proyectos para *{query}*. "
                    f"Escribe uno de los nombres completos:\n{names}"
                )
            else:
                response = f'No encontré un proyecto monitoreado que contenga "{query}".'
        elif command.lower() in {"resumen proyecto", "/resumen proyecto"}:
            response = SUMMARY_HELP
        elif ALARM_COMMAND.match(command) or SHORT_COMMAND.match(command):
            response = (
                "Este grupo es solo de consulta. Los estados se cambian desde el grupo "
                "de la zona correspondiente."
            )
        else:
            return None
        return CommandResponse(general.id, group_id, response)
    return execute_zone_command(group_id, sender_id, text)
