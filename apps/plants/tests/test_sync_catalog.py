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
        "id": pid, "name": name, "lon": -74.1, "lat": 4.7,
        "plant_code": "PLANT-1", "weather_plant_code": None,
        "is_minifarm": True, "is_self_consumption": False,
        "installed_capacity": 999.5, "location": "Cesar", "raw": {"id": pid},
    }
    defaults.update(kwargs)
    return ProjectInfo(**defaults)


def inverter_live(iid=1571, dev_name="300KTL-Inversor1") -> InverterLive:
    return InverterLive(
        id=iid, dev_name=dev_name, state="Grid-connected", power=100.0,
        efficiency=98.0, temperature=60.0, time=datetime(2026, 7, 8, 16, 0), raw={"id": iid},
    )


def fake_client(projects, inverters_by_project):
    client = MagicMock()
    client.list_projects.return_value = projects
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
        assert project.inverters.get(external_id=1571).dev_name == "300KTL-Inversor1"
        assert stats == {"projects": 1, "inverters": 1, "errors": 0}

    @patch("apps.plants.tasks.SolarViewClient")
    def test_updates_existing_without_duplicating(self, client_cls):
        Project.objects.create(external_id=146, name="viejo", synced_at=timezone.now())
        client_cls.from_settings.return_value = fake_client(
            [project_info(name="nuevo")], {146: []}
        )

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
        client.project_inverters.side_effect = inverters
        client_cls.from_settings.return_value = client

        stats = sync_catalog()

        assert Project.objects.count() == 2
        assert Inverter.objects.filter(project__external_id=121).count() == 1
        assert stats["errors"] == 1
