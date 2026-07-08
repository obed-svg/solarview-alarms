from datetime import datetime, timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.plants.models import Inverter, MaintenanceWindow, Project


def make_project(**kwargs) -> Project:
    defaults = {
        "external_id": 146,
        "name": "Minigranja 0015 - El Son",
        "synced_at": timezone.now(),
    }
    defaults.update(kwargs)
    return Project.objects.create(**defaults)


@pytest.mark.django_db
class TestProject:
    def test_external_id_is_unique(self):
        make_project()

        with pytest.raises(IntegrityError):
            make_project(name="duplicado")

    def test_defaults(self):
        project = make_project()

        assert project.monitoring_enabled is True
        assert project.timezone == "America/Bogota"
        assert project.raw == {}


@pytest.mark.django_db
class TestInverter:
    def test_unique_per_project(self):
        project = make_project()
        Inverter.objects.create(
            project=project, external_id=1571, dev_name="300KTL-Inversor1",
            synced_at=timezone.now(),
        )

        with pytest.raises(IntegrityError):
            Inverter.objects.create(
                project=project, external_id=1571, dev_name="otro",
                synced_at=timezone.now(),
            )

    def test_same_external_id_allowed_in_other_project(self):
        p1 = make_project()
        p2 = make_project(external_id=121, name="Laureles")
        Inverter.objects.create(
            project=p1, external_id=1571, dev_name="inv", synced_at=timezone.now()
        )

        Inverter.objects.create(
            project=p2, external_id=1571, dev_name="inv", synced_at=timezone.now()
        )  # no debe lanzar


@pytest.mark.django_db
class TestMaintenanceWindow:
    def setup_method(self, method):
        self.now = timezone.make_aware(datetime(2026, 7, 8, 12, 0))

    def _window(self, project, start_offset_h, end_offset_h, inverter=None):
        return MaintenanceWindow.objects.create(
            project=project,
            inverter=inverter,
            starts_at=self.now + timedelta(hours=start_offset_h),
            ends_at=self.now + timedelta(hours=end_offset_h),
            reason="test",
        )

    def test_active_at_finds_covering_window(self):
        project = make_project()
        self._window(project, -1, +1)

        assert MaintenanceWindow.objects.active_at(project, self.now).exists()

    def test_active_at_ignores_past_and_future_windows(self):
        project = make_project()
        self._window(project, -3, -2)
        self._window(project, +2, +3)

        assert not MaintenanceWindow.objects.active_at(project, self.now).exists()

    def test_active_at_filters_by_inverter(self):
        project = make_project()
        inv = Inverter.objects.create(
            project=project, external_id=1, dev_name="inv1", synced_at=timezone.now()
        )
        self._window(project, -1, +1, inverter=inv)

        # ventana de UN inversor no aplica a consulta de proyecto completo
        assert not MaintenanceWindow.objects.active_at(project, self.now).exists()
        assert MaintenanceWindow.objects.active_at(project, self.now, inverter=inv).exists()

    def test_project_wide_window_covers_any_inverter(self):
        project = make_project()
        inv = Inverter.objects.create(
            project=project, external_id=1, dev_name="inv1", synced_at=timezone.now()
        )
        self._window(project, -1, +1)  # sin inverter = todo el proyecto

        assert MaintenanceWindow.objects.active_at(project, self.now, inverter=inv).exists()
