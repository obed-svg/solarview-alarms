from unittest.mock import patch

import pytest
import requests
import responses
from django.utils import timezone

from apps.alarms.models import Alarm, AlarmRule, Severity
from apps.notifications.channels.discord import SEVERITY_COLORS, DiscordChannel
from apps.notifications.dispatcher import notify
from apps.notifications.models import NotificationChannel, NotificationLog
from apps.notifications.tasks import send_notification
from apps.plants.models import Project

WEBHOOK = "https://discord.com/api/webhooks/123/abc"


@pytest.fixture
def alarm(db):
    project = Project.objects.create(external_id=146, name="El Son", synced_at=timezone.now())
    rule = AlarmRule.objects.get(code="project_no_generation")
    now = timezone.now()
    return Alarm.objects.create(
        rule=rule, project=project, component_type=rule.component_type,
        severity=Severity.CRITICAL,
        dedup_key=Alarm.build_dedup_key(rule.code, project.external_id),
        triggered_at=now, last_seen_at=now,
        evidence={"poa_min_wm2_observed": 850.0, "power_max_kw_observed": 0.05},
    )


@pytest.fixture
def channel(db, monkeypatch):
    monkeypatch.setenv("webhook_discord", WEBHOOK)
    return NotificationChannel.objects.create(
        name="ops", kind=NotificationChannel.Kind.DISCORD, env_key="webhook_discord",
        min_severity=Severity.MEDIUM,
    )


class TestEmbed:
    def test_payload_has_severity_color_and_fields(self, alarm, channel):
        payload = DiscordChannel(channel).build_payload("opened", alarm)

        embed = payload["embeds"][0]
        assert embed["color"] == SEVERITY_COLORS[Severity.CRITICAL]
        assert "Proyecto sin generación" in embed["title"]
        field_names = {f["name"] for f in embed["fields"]}
        assert {"Proyecto", "Evento", "Evidencia"} <= field_names
        assert "El Son" in str(embed["fields"])


@pytest.mark.django_db
class TestDispatcher:
    @patch("apps.notifications.dispatcher.send_notification")
    def test_creates_pending_log_and_enqueues(self, task, alarm, channel):
        notify(alarm, "opened")

        log = NotificationLog.objects.get()
        assert log.status == NotificationLog.Status.PENDING
        assert log.event == "opened"
        task.delay.assert_called_once_with(log.id)

    @patch("apps.notifications.dispatcher.send_notification")
    def test_idempotent_same_event(self, task, alarm, channel):
        notify(alarm, "opened")
        notify(alarm, "opened")

        assert NotificationLog.objects.count() == 1
        assert task.delay.call_count == 1

    @patch("apps.notifications.dispatcher.send_notification")
    def test_respects_min_severity(self, task, alarm, channel):
        channel.min_severity = Severity.CRITICAL
        channel.save()
        alarm.severity = Severity.MEDIUM
        alarm.save()

        notify(alarm, "opened")

        assert NotificationLog.objects.count() == 0

    @patch("apps.notifications.dispatcher.send_notification")
    def test_skips_disabled_channel(self, task, alarm, channel):
        channel.enabled = False
        channel.save()

        notify(alarm, "opened")

        assert NotificationLog.objects.count() == 0


@pytest.mark.django_db
class TestSendNotification:
    @responses.activate
    def test_success_marks_sent_with_channel_snapshot(self, alarm, channel):
        responses.get(WEBHOOK, json={"id": "123", "channel_id": "555666777"})
        responses.post(WEBHOOK, status=204)
        log = NotificationLog.objects.create(
            alarm=alarm, channel=channel, event="opened"
        )

        send_notification.run(log.id)

        log.refresh_from_db()
        assert log.status == NotificationLog.Status.SENT
        assert log.target_channel_id == "555666777"  # trazabilidad pedida por el usuario
        assert log.response_status == 204
        assert log.sent_at is not None
        assert log.payload["embeds"]
        channel.refresh_from_db()
        assert channel.discord_channel_id == "555666777"  # cacheado

    @responses.activate
    def test_cached_channel_id_skips_lookup(self, alarm, channel):
        channel.discord_channel_id = "999"
        channel.save()
        responses.post(WEBHOOK, status=204)
        log = NotificationLog.objects.create(alarm=alarm, channel=channel, event="opened")

        send_notification.run(log.id)

        log.refresh_from_db()
        assert log.target_channel_id == "999"
        assert len(responses.calls) == 1  # solo el POST, sin GET

    @responses.activate
    def test_failure_records_error_and_raises_for_retry(self, alarm, channel):
        channel.discord_channel_id = "999"
        channel.save()
        responses.post(WEBHOOK, status=500, body="internal")
        log = NotificationLog.objects.create(alarm=alarm, channel=channel, event="opened")

        with pytest.raises(requests.RequestException):
            send_notification.run(log.id)

        log.refresh_from_db()
        assert log.status == NotificationLog.Status.FAILED
        assert log.attempts == 1
        assert "500" in log.last_error

    def test_missing_webhook_env_fails_without_retry(self, alarm, channel, monkeypatch):
        monkeypatch.delenv("webhook_discord", raising=False)
        log = NotificationLog.objects.create(alarm=alarm, channel=channel, event="opened")

        send_notification.run(log.id)  # no debe lanzar: error de config, no transitorio

        log.refresh_from_db()
        assert log.status == NotificationLog.Status.FAILED
        assert "webhook" in log.last_error.lower()
