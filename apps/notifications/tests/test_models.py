import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.alarms.models import Alarm, AlarmRule, Severity
from apps.notifications.models import NotificationChannel, NotificationLog
from apps.plants.models import Project


def make_channel(**kwargs) -> NotificationChannel:
    # nombre propio de tests: la migración 0002 ya seedea "ops-discord"
    defaults = {"name": "test-channel", "kind": NotificationChannel.Kind.DISCORD}
    defaults.update(kwargs)
    return NotificationChannel.objects.create(**defaults)


def make_alarm() -> Alarm:
    project = Project.objects.create(external_id=146, name="El Son", synced_at=timezone.now())
    rule = AlarmRule.objects.get(code="weather_comm_lost")  # ya seedeada
    now = timezone.now()
    return Alarm.objects.create(
        rule=rule, project=project, component_type=rule.component_type,
        severity=rule.default_severity,
        dedup_key=Alarm.build_dedup_key(rule.code, project.external_id),
        triggered_at=now, last_seen_at=now,
    )


@pytest.mark.django_db
class TestNotificationChannel:
    def test_webhook_url_read_from_env_at_send_time(self, monkeypatch):
        channel = make_channel(env_key="webhook_discord")
        monkeypatch.setenv("webhook_discord", "https://discord.com/api/webhooks/123/abc")

        assert channel.webhook_url == "https://discord.com/api/webhooks/123/abc"

    def test_webhook_url_missing_env_returns_empty(self, monkeypatch):
        channel = make_channel(env_key="no_existe")
        monkeypatch.delenv("no_existe", raising=False)

        assert channel.webhook_url == ""

    def test_accepts_severity_at_or_above_min(self):
        channel = make_channel(min_severity=Severity.HIGH)

        assert channel.accepts(Severity.CRITICAL) is True
        assert channel.accepts(Severity.HIGH) is True
        assert channel.accepts(Severity.MEDIUM) is False

    def test_default_min_severity_is_medium(self):
        channel = make_channel()

        assert channel.min_severity == Severity.MEDIUM
        assert channel.accepts(Severity.LOW) is False


@pytest.mark.django_db
class TestNotificationLog:
    def test_same_event_not_logged_twice(self):
        alarm, channel = make_alarm(), make_channel()
        NotificationLog.objects.create(
            alarm=alarm, channel=channel, event=NotificationLog.Event.OPENED,
            target_channel_id="111222333",
        )

        with pytest.raises(IntegrityError):
            NotificationLog.objects.create(
                alarm=alarm, channel=channel, event=NotificationLog.Event.OPENED,
            )

    def test_different_events_allowed(self):
        alarm, channel = make_alarm(), make_channel()
        NotificationLog.objects.create(
            alarm=alarm, channel=channel, event=NotificationLog.Event.OPENED
        )

        NotificationLog.objects.create(
            alarm=alarm, channel=channel, event=NotificationLog.Event.RESOLVED
        )  # no debe lanzar

    def test_target_channel_id_snapshot_persists(self):
        alarm, channel = make_alarm(), make_channel(discord_channel_id="999")
        log = NotificationLog.objects.create(
            alarm=alarm, channel=channel, event=NotificationLog.Event.OPENED,
            target_channel_id=channel.discord_channel_id,
        )

        channel.discord_channel_id = "otro-canal"
        channel.save()

        log.refresh_from_db()
        assert log.target_channel_id == "999"  # trazabilidad: conserva el canal del envío
