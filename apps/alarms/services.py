"""Transiciones operativas de alarmas con validación y auditoría."""

from django.db import transaction
from django.utils import timezone

from .models import Alarm, AlarmRule, AlarmStatusHistory, RuleConfig


class InvalidAlarmTransition(ValueError):
    pass


ALLOWED_TRANSITIONS = {
    Alarm.Status.ACTIVE: {
        Alarm.Status.ACKNOWLEDGED,
        Alarm.Status.IN_MAINTENANCE,
        Alarm.Status.RESOLVED,
    },
    Alarm.Status.ACKNOWLEDGED: {Alarm.Status.IN_MAINTENANCE, Alarm.Status.RESOLVED},
    Alarm.Status.IN_MAINTENANCE: {Alarm.Status.RESOLVED},
    Alarm.Status.RESOLVED: set(),
}


@transaction.atomic
def transition_alarm(alarm_id: int, target_status: str, actor: str, source: str) -> Alarm:
    alarm = Alarm.objects.select_for_update().get(id=alarm_id)
    if alarm.status == target_status:
        return alarm
    if target_status not in ALLOWED_TRANSITIONS.get(alarm.status, set()):
        raise InvalidAlarmTransition(
            f"No se puede pasar de {alarm.get_status_display()} a {target_status}."
        )

    previous = alarm.status
    now = timezone.now()
    fields = ["status"]
    alarm.status = target_status
    if target_status == Alarm.Status.ACKNOWLEDGED:
        alarm.acknowledged_at = now
        alarm.acknowledged_by = actor
        fields.extend(["acknowledged_at", "acknowledged_by"])
    elif target_status == Alarm.Status.IN_MAINTENANCE:
        if not alarm.acknowledged_at:
            alarm.acknowledged_at = now
            alarm.acknowledged_by = actor
            fields.extend(["acknowledged_at", "acknowledged_by"])
        alarm.maintenance_started_at = now
        alarm.maintenance_started_by = actor
        fields.extend(["maintenance_started_at", "maintenance_started_by"])
    elif target_status == Alarm.Status.RESOLVED:
        alarm.resolved_at = now
        alarm.resolution_type = Alarm.ResolutionType.MANUAL
        fields.extend(["resolved_at", "resolution_type"])
    alarm.save(update_fields=fields)
    AlarmStatusHistory.objects.create(
        alarm=alarm,
        from_status=previous,
        to_status=target_status,
        actor=actor,
        source=source,
    )
    return alarm


@transaction.atomic
def set_rule_excluded_for_project(
    rule: AlarmRule,
    project,
    *,
    excluded: bool,
    actor: str,
    source: str,
) -> int:
    """Activa/desactiva una regla para un proyecto y cierra sus alarmas abiertas."""
    config, _ = RuleConfig.objects.get_or_create(rule=rule, project=project)
    config.enabled = False if excluded else None
    config.save(update_fields=["enabled"])

    if not excluded:
        return 0

    open_alarm_ids = list(
        Alarm.objects.select_for_update()
        .filter(rule=rule, project=project)
        .exclude(status=Alarm.Status.RESOLVED)
        .values_list("id", flat=True)
    )
    for alarm_id in open_alarm_ids:
        transition_alarm(alarm_id, Alarm.Status.RESOLVED, actor, source)
    return len(open_alarm_ids)
