import logging

from celery import shared_task
from django.utils import timezone

from integrations.solarview.client import SolarViewClient
from integrations.solarview.exceptions import SolarViewError

from .models import Inverter, Project

logger = logging.getLogger(__name__)


@shared_task
def sync_catalog() -> dict:
    """Upsert de proyectos e inversores desde la API por external_id.

    - No toca `monitoring_enabled` (override operativo local).
    - Inversores que desaparecen de la API se marcan is_active=False, no se borran.
    - Un proyecto que falla no aborta el sync de los demás.
    """
    client = SolarViewClient.from_settings()
    now = timezone.now()
    stats = {"projects": 0, "inverters": 0, "errors": 0}

    for info in client.list_projects():
        project, _ = Project.objects.update_or_create(
            external_id=info.id,
            defaults={
                "name": info.name,
                "plant_code": info.plant_code or "",
                "weather_plant_code": info.weather_plant_code or "",
                "installed_capacity_kw": info.installed_capacity,
                "latitude": info.lat,
                "longitude": info.lon,
                "is_minifarm": info.is_minifarm,
                "is_self_consumption": info.is_self_consumption,
                "raw": info.raw,
                "synced_at": now,
            },
        )
        stats["projects"] += 1

        try:
            inverters = client.project_inverters(info.id)
        except SolarViewError:
            logger.exception("sync_catalog: fallo listando inversores del proyecto %s", info.id)
            stats["errors"] += 1
            continue

        seen_ids = []
        for inv in inverters:
            Inverter.objects.update_or_create(
                project=project,
                external_id=inv.id,
                defaults={
                    "dev_name": inv.dev_name,
                    "is_active": True,
                    "raw": inv.raw,
                    "synced_at": now,
                },
            )
            seen_ids.append(inv.id)
            stats["inverters"] += 1

        Inverter.objects.filter(project=project).exclude(external_id__in=seen_ids).update(
            is_active=False
        )

    return stats
