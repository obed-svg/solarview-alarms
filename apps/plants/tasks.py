import logging

from celery import shared_task
from django.utils import timezone

from integrations.solarview.client import SolarViewClient
from integrations.solarview.exceptions import SolarViewError

from .models import Inverter, Project

logger = logging.getLogger(__name__)


@shared_task
def sync_catalog() -> dict:
    """Sincroniza las minigranjas y sus inversores desde SolarView v1.

    company-projects entrega el inventario básico y project-detail completa la
    capacidad y los metadatos. Los overrides operativos locales nunca se pisan.
    """
    client = SolarViewClient.from_settings()
    now = timezone.now()
    stats = {"projects": 0, "inverters": 0, "errors": 0, "skipped": 0}

    for info in client.list_projects():
        if not info.is_minifarm:
            stats["skipped"] += 1
            demoted = Project.objects.filter(external_id=info.id, is_minifarm=True).update(
                is_minifarm=False, synced_at=now
            )
            if demoted:
                logger.warning(
                    "sync_catalog: el proyecto %s (%s) ya no es minigranja en la API; "
                    "deja de alarmar (la fila se conserva)",
                    info.id,
                    info.name,
                )
            continue

        try:
            info = client.project_detail(info.id)
        except SolarViewError:
            # No borrar metadatos buenos por un fallo transitorio del detalle.
            logger.exception("sync_catalog: fallo detalle del proyecto %s", info.id)
            stats["errors"] += 1

        defaults = {
            "name": info.name,
            "plant_code": info.plant_code or "",
            "latitude": info.lat,
            "longitude": info.lon,
            "is_minifarm": info.is_minifarm,
            "is_self_consumption": info.is_self_consumption,
            "raw": info.raw,
            "synced_at": now,
        }
        if info.weather_plant_code is not None:
            defaults["weather_plant_code"] = info.weather_plant_code
        if info.installed_capacity is not None:
            defaults["installed_capacity_kw"] = info.installed_capacity

        project, _ = Project.objects.update_or_create(
            external_id=info.id,
            defaults=defaults,
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
