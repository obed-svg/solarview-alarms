import pytest
from django_celery_beat.models import PeriodicTask

from apps.notifications.models import NotificationChannel


@pytest.mark.django_db
class TestDefaultSchedules:
    def test_beat_schedules_seeded(self):
        names = set(PeriodicTask.objects.values_list("name", flat=True))

        assert {
            "alarms: evaluación fast (5 min)",
            "alarms: evaluación hourly",
            "alarms: check SLA",
            "plants: sync catálogo",
        } <= names

    def test_fast_dispatch_config(self):
        task = PeriodicTask.objects.get(name="alarms: evaluación fast (5 min)")

        assert task.task == "apps.alarms.tasks.dispatch_evaluations"
        assert task.crontab.minute == "*/5"
        assert task.kwargs == '{"rule_group": "fast"}'


@pytest.mark.django_db
class TestDefaultChannel:
    def test_discord_channel_seeded_disabled(self):
        channel = NotificationChannel.objects.get(name="ops-discord")

        assert channel.kind == NotificationChannel.Kind.DISCORD
        assert channel.env_key == "webhook_discord"
        # nace deshabilitado: el operador lo enciende en admin cuando esté listo
        assert channel.enabled is False
