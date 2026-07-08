import logging

from celery import shared_task
from django.core.cache import cache

from apps.notifications.dispatcher import notify
from apps.plants.models import Project

from .engine import evaluate_project as run_evaluation
from .models import EvaluationRun

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
