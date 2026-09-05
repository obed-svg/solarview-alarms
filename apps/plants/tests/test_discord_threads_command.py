"""T49: `manage.py discord_threads` — inventario y carga de hilos por proyecto."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.plants.models import Project


@pytest.fixture
def projects(db):
    Project.objects.create(
        external_id=146, name="Minigranja 0015 - El Son", is_minifarm=True,
        synced_at=timezone.now(),
    )
    Project.objects.create(
        external_id=105, name="Gimnasio San Ángelo", is_minifarm=False,
        synced_at=timezone.now(),
    )


def run(*args) -> str:
    out = StringIO()
    call_command("discord_threads", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
class TestDiscordThreadsCommand:
    def test_lista_solo_proyectos_con_alarmas(self, projects):
        output = run()

        assert "El Son" in output
        assert "Gimnasio San Ángelo" not in output
        assert "SIN HILO" in output
        assert "1 proyectos con alarmas (1 sin hilo), 1 excluidos" in output

    def test_set_asigna_hilos(self, projects):
        run("--set", "146=1400000000000000001")

        assert Project.objects.get(external_id=146).discord_thread_id == "1400000000000000001"

    def test_set_falla_con_proyecto_inexistente(self, projects):
        with pytest.raises(CommandError):
            run("--set", "999=123")

    def test_pending_oculta_los_ya_configurados(self, projects):
        run("--set", "146=1400000000000000001")

        assert "El Son" not in run("--pending")
