import logging

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

from apps.notifications.dispatcher import notify
from apps.plants.models import Project

from .engine import _process_outcome
from .engine import evaluate_project as run_evaluation
from .models import Alarm, AlarmRule, EvaluationRun, Severity
from .rules.base import RuleOutcome

logger = logging.getLogger(__name__)

LOCK_TTL = 270  # < soft_time_limit: el lock muere antes que el siguiente tick de 5 min


@shared_task
def dispatch_evaluations(rule_group: str = "fast") -> int:
    """Fan-out: una tarea evaluate_project por proyecto monitoreado."""
    project_ids = list(
        Project.objects.filter(monitoring_enabled=True).values_list("id", flat=True)
    )
    for project_id in project_ids:
        evaluate_project.delay(project_id, rule_group)
    return len(project_ids)


@shared_task(soft_time_limit=240)
def evaluate_project(project_id: int, rule_group: str = "fast") -> str:
    """Corre el engine para UN proyecto. Lock no-bloqueante: si el tick anterior
    de este proyecto sigue corriendo, este se salta (nunca solapar)."""
    lock_key = f"evaluate:{project_id}:{rule_group}"
    if not cache.add(lock_key, "1", timeout=LOCK_TTL):
        logger.warning("Tick anterior de proyecto %s aún corre; salto", project_id)
        return "skipped:locked"
    try:
        project = Project.objects.get(id=project_id)
        run: EvaluationRun = run_evaluation(project, rule_group=rule_group, notifier=notify)
        return f"{run.status}:{run.stats.get('opened', 0)} abiertas"
    finally:
        cache.delete(lock_key)


@shared_task
def check_sla() -> dict:
    """Regla 20 (alarm_sla_breach): alarma sobre alarmas — ACTIVE sin reconocer
    más allá del SLA abre un breach; al doble del SLA escala a crítica; se
    resuelve solo cuando la alarma origen es atendida (ack o resuelta)."""
    rule = AlarmRule.objects.get(code="alarm_sla_breach")
    stats = {"opened": 0, "updated": 0, "resolved": 0, "not_computable": [], "errors": {}}
    if not rule.enabled:
        return stats
    now = timezone.now()

    overdue_ids = set()
    open_alarms = (
        Alarm.objects.filter(status=Alarm.Status.ACTIVE)
        .exclude(rule=rule)  # sin recursión sobre los propios breaches
        .select_related("project", "rule")
    )
    for alarm in open_alarms:
        if not rule.is_enabled_for(alarm.project):
            continue
        params = rule.params_for(alarm.project)
        sla_minutes = params["sla_ack_minutes"]
        age_minutes = (now - alarm.triggered_at).total_seconds() / 60
        if age_minutes <= sla_minutes:
            continue

        overdue_ids.add(alarm.id)
        escalated = age_minutes > sla_minutes * params.get("escalate_after_multiplier", 2)
        outcome = RuleOutcome(
            status="firing",
            dedup_suffix=f"alarm:{alarm.id}",
            severity=Severity.CRITICAL if escalated else rule.default_severity,
            evidence={
                "source_alarm_id": alarm.id,
                "source_rule": alarm.rule.code,
                "source_dedup_key": alarm.dedup_key,
                "source_triggered_at": str(alarm.triggered_at),
                "age_minutes": round(age_minutes),
                "sla_minutes": sla_minutes,
            },
        )
        _process_outcome(rule, alarm.project, outcome, None, stats, notify)

    # resolver breaches cuya alarma origen ya fue atendida
    for breach in Alarm.objects.filter(rule=rule).exclude(status=Alarm.Status.RESOLVED):
        source_id = int(breach.dedup_key.rsplit(":", 1)[1])
        if source_id not in overdue_ids:
            outcome = RuleOutcome(status="ok", dedup_suffix=f"alarm:{source_id}")
            _process_outcome(rule, breach.project, outcome, None, stats, notify)

    return stats
