from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.plants.models import Inverter, Project
from apps.plants.tasks import sync_catalog
from integrations.solarview.exceptions import SolarViewAPIError
from integrations.solarview.schemas import InverterLive, ProjectInfo


def project_info(pid=146, name="El Son", **kwargs) -> ProjectInfo:
    defaults = {
        "id": pid,
        "name": name,
        "lon": -74.1,
        "lat": 4.7,
        "plant_code": "PLANT-1",
        "weather_plant_code": None,
        "is_minifarm": True,
        "is_self_consumption": False,
        "installed_capacity": 999.5,
        "location": "Cesar",
        "raw": {"id": pid},
    }
    defaults.update(kwargs)
    return ProjectInfo(**defaults)


def inverter_live(iid=1571, dev_name="300KTL-Inversor1") -> InverterLive:
    return InverterLive(
        id=iid,
        dev_name=dev_name,
        state="Grid-connected",
        power=100.0,
        efficiency=98.0,
        temperature=60.0,
        time=datetime(2026, 7, 8, 16, 0),
        raw={"id": iid},
    )


def fake_client(projects, inverters_by_project):
    client = MagicMock()
    client.list_projects.return_value = projects
    details = {project.id: project for project in projects}
    client.project_detail.side_effect = lambda pid: details[pid]
    client.project_inverters.side_effect = lambda pid: inverters_by_project[pid]
    return client


@pytest.mark.django_db
class TestSyncCatalog:
    @patch("apps.plants.tasks.SolarViewClient")
    def test_creates_projects_and_inverters(self, client_cls):
        client_cls.from_settings.return_value = fake_client(
            [project_info()], {146: [inverter_live()]}
        )

        stats = sync_catalog()

        project = Project.objects.get(external_id=146)
        assert project.name == "El Son"
        assert float(project.latitude) == 4.7
        assert project.is_minifarm is True
        assert project.is_self_consumption is False
        assert project.inverters.get(external_id=1571).dev_name == "300KTL-Inversor1"
        assert stats == {"projects": 1, "inverters": 1, "errors": 0, "skipped": 0}

    @patch("apps.plants.tasks.SolarViewClient")
    def test_solo_espeja_minigranjas(self, client_cls):
        # T49: el autoconsumo no alarma → ni siquiera se guarda (admin sin ruido)
        client_cls.from_settings.return_value = fake_client(
            [project_info(), project_info(pid=105, name="Gimnasio", is_minifarm=False)],
            {146: [inverter_live()], 105: []},
        )

        stats = sync_catalog()

        assert list(Project.objects.values_list("external_id", flat=True)) == [146]
        assert stats["projects"] == 1 and stats["skipped"] == 1

    @patch("apps.plants.tasks.SolarViewClient")
    def test_proyecto_degradado_deja_de_alarmar_sin_perder_la_fila(self, client_cls):
        # era minigranja y la API deja de marcarlo: la fila se conserva (histórico
        # de alarmas) pero sale del alcance de las evaluaciones
        Project.objects.create(
            external_id=146, name="El Son", is_minifarm=True, synced_at=timezone.now()
        )
        client_cls.from_settings.return_value = fake_client(
            [project_info(is_minifarm=False)], {146: []}
        )

        sync_catalog()

        project = Project.objects.get(external_id=146)
        assert project.is_minifarm is False
        assert Project.objects.alarmable().count() == 0

    @patch("apps.plants.tasks.SolarViewClient")
    def test_no_pisa_el_hilo_de_discord(self, client_cls):
        # T49: discord_thread_id es override local, como monitoring_enabled
        Project.objects.create(
            external_id=146,
            name="x",
            discord_thread_id="1400000000000000001",
            synced_at=timezone.now(),
        )
        client_cls.from_settings.return_value = fake_client([project_info()], {146: []})

        sync_catalog()

        assert Project.objects.get(external_id=146).discord_thread_id == "1400000000000000001"

    @patch("apps.plants.tasks.SolarViewClient")
    def test_self_consumption_flag_is_persisted(self, client_cls):
        # T35: el flag gobierna el skip de las reglas 9/10/18
        client_cls.from_settings.return_value = fake_client(
            [project_info(is_self_consumption=True)], {146: []}
        )

        sync_catalog()

        assert Project.objects.get(external_id=146).is_self_consumption is True

    @patch("apps.plants.tasks.SolarViewClient")
    def test_updates_existing_without_duplicating(self, client_cls):
        Project.objects.create(external_id=146, name="viejo", synced_at=timezone.now())
        client_cls.from_settings.return_value = fake_client([project_info(name="nuevo")], {146: []})

        sync_catalog()

        assert Project.objects.count() == 1
        assert Project.objects.get(external_id=146).name == "nuevo"

    @patch("apps.plants.tasks.SolarViewClient")
    def test_preserves_monitoring_enabled_override(self, client_cls):
        Project.objects.create(
            external_id=146, name="x", monitoring_enabled=False, synced_at=timezone.now()
        )
        client_cls.from_settings.return_value = fake_client([project_info()], {146: []})

        sync_catalog()

        assert Project.objects.get(external_id=146).monitoring_enabled is False

    @patch("apps.plants.tasks.SolarViewClient")
    def test_marks_missing_inverters_inactive(self, client_cls):
        client_cls.from_settings.return_value = fake_client(
            [project_info()], {146: [inverter_live(iid=1571)]}
        )
        sync_catalog()
        # segundo sync: el inversor 1571 ya no viene, aparece otro
        client_cls.from_settings.return_value = fake_client(
            [project_info()], {146: [inverter_live(iid=9999, dev_name="nuevo")]}
        )

        sync_catalog()

        assert Inverter.objects.get(external_id=1571).is_active is False
        assert Inverter.objects.get(external_id=9999).is_active is True

    @patch("apps.plants.tasks.SolarViewClient")
    def test_one_project_failing_does_not_abort_the_rest(self, client_cls):
        def inverters(pid):
            if pid == 146:
                raise SolarViewAPIError("boom", path="x")
            return [inverter_live()]

        client = MagicMock()
        client.list_projects.return_value = [project_info(), project_info(pid=121, name="Laureles")]
        details = {info.id: info for info in client.list_projects.return_value}
        client.project_detail.side_effect = lambda pid: details[pid]
        client.project_inverters.side_effect = inverters
        client_cls.from_settings.return_value = client

        stats = sync_catalog()

        assert Project.objects.count() == 2
        assert Inverter.objects.filter(project__external_id=121).count() == 1
        assert stats["errors"] == 1
