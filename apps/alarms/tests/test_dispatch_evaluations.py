"""T49: el fan-off del engine solo alcanza proyectos con alarmas (minigranjas)."""

from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.alarms.tasks import dispatch_evaluations
from apps.plants.models import Project


def make_project(**kwargs) -> Project:
    defaults = {
        "external_id": 146,
        "name": "Minigranja 0015 - El Son",
        "is_minifarm": True,
        "synced_at": timezone.now(),
    }
    defaults.update(kwargs)
    return Project.objects.create(**defaults)


@pytest.mark.django_db
class TestDispatchEvaluations:
    @patch("apps.alarms.tasks.evaluate_project")
    def test_excluye_autoconsumo_y_no_monitoreados(self, task):
        minifarm = make_project()
        make_project(external_id=105, name="Gimnasio San Ángelo", is_minifarm=False)
        make_project(external_id=151, name="DEPRECATED", monitoring_enabled=False)

        assert dispatch_evaluations() == 1
        task.delay.assert_called_once_with(minifarm.id, "fast")
