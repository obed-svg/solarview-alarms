import json

import pytest
from django.utils import timezone
from nacl.signing import SigningKey

from apps.alarms.models import Alarm, AlarmRule
from apps.plants.models import Project


@pytest.fixture
def signed_post(client, settings):
    key = SigningKey.generate()
    settings.DISCORD_PUBLIC_KEY = key.verify_key.encode().hex()
    settings.DISCORD_GUILD_ID = "guild-1"

    def post(payload):
        body = json.dumps(payload).encode()
        timestamp = "1700000000"
        signature = key.sign(timestamp.encode() + body).signature.hex()
        return client.post(
            "/discord/interactions/",
            data=body,
            content_type="application/json",
            HTTP_X_SIGNATURE_ED25519=signature,
            HTTP_X_SIGNATURE_TIMESTAMP=timestamp,
        )

    return post


def make_alarm():
    project = Project.objects.create(
        external_id=9991,
        name="Proyecto Discord",
        is_minifarm=True,
        discord_thread_id="thread-1",
        synced_at=timezone.now(),
    )
    rule = AlarmRule.objects.get(code="project_no_generation")
    return Alarm.objects.create(
        rule=rule,
        project=project,
        component_type=rule.component_type,
        severity=rule.default_severity,
        dedup_key="discord-test:9991",
        triggered_at=timezone.now(),
        last_seen_at=timezone.now(),
    )


def command(alarm_id, action="reconocer", channel="thread-1"):
    return {
        "type": 2,
        "guild_id": "guild-1",
        "channel_id": channel,
        "member": {"user": {"id": "user-42"}},
        "data": {
            "name": "alarma",
            "options": [{"name": action, "options": [{"name": "id", "value": alarm_id}]}],
        },
    }


def summary_command(channel="general", *, project=None):
    options = [] if project is None else [{"name": "proyecto", "value": str(project)}]
    return {
        "type": 2,
        "guild_id": "guild-1",
        "channel_id": channel,
        "data": {"name": "resumen", "options": options},
    }


def autocomplete_command(value):
    return {
        "type": 4,
        "guild_id": "guild-1",
        "channel_id": "general",
        "data": {
            "name": "resumen",
            "options": [
                {"type": 3, "name": "proyecto", "value": value, "focused": True}
            ],
        },
    }


@pytest.mark.django_db
class TestDiscordInteractions:
    def test_rejects_invalid_signature(self, client, settings):
        settings.DISCORD_PUBLIC_KEY = SigningKey.generate().verify_key.encode().hex()
        response = client.post(
            "/discord/interactions/",
            data=b"{}",
            content_type="application/json",
            HTTP_X_SIGNATURE_ED25519="00" * 64,
            HTTP_X_SIGNATURE_TIMESTAMP="1",
        )
        assert response.status_code == 400

    def test_ping(self, signed_post):
        response = signed_post({"type": 1})
        assert response.json() == {"type": 1}

    def test_general_summary(self, signed_post):
        from apps.notifications.models import NotificationChannel

        NotificationChannel.objects.create(
            name="discord-summary",
            kind=NotificationChannel.Kind.DISCORD,
            discord_channel_id="general",
            enabled=True,
        )
        response = signed_post(summary_command())
        assert response.status_code == 200
        body = response.json()
        assert body["type"] == 4
        assert "Resumen general de alarmas" in body["data"]["embeds"][0]["title"]

    def test_project_autocomplete_finds_partial_name(self, signed_post):
        project = Project.objects.create(
            external_id=9992,
            name="Minigranja Joropo del Norte",
            is_minifarm=True,
            synced_at=timezone.now(),
        )

        response = signed_post(autocomplete_command("Joropo"))

        assert response.json() == {
            "type": 8,
            "data": {
                "choices": [
                    {"name": "Minigranja Joropo del Norte", "value": str(project.external_id)}
                ]
            },
        }

    def test_summary_can_filter_by_selected_project(self, signed_post):
        from apps.notifications.models import NotificationChannel

        alarm = make_alarm()
        NotificationChannel.objects.create(
            name="discord-project-summary",
            kind=NotificationChannel.Kind.DISCORD,
            discord_channel_id="general",
            enabled=True,
        )

        response = signed_post(summary_command(project=alarm.project.external_id))
        embed = response.json()["data"]["embeds"][0]

        assert embed["title"] == "Resumen de Proyecto Discord"
        assert f"#{alarm.id}" in embed["description"]

    def test_summary_rejected_inside_thread(self, signed_post):
        from apps.notifications.models import NotificationChannel

        NotificationChannel.objects.create(
            name="discord-summary-thread-test",
            kind=NotificationChannel.Kind.DISCORD,
            discord_channel_id="general",
            enabled=True,
        )
        response = signed_post(summary_command(channel="thread-1"))
        assert "canal principal" in response.json()["data"]["content"]

    def test_acknowledges_from_project_thread(self, signed_post):
        alarm = make_alarm()
        response = signed_post(command(alarm.id))
        alarm.refresh_from_db()
        assert response.status_code == 200
        assert alarm.status == Alarm.Status.ACKNOWLEDGED
        assert alarm.acknowledged_by == "discord+user-42@invalid.local"

    def test_rejects_different_thread(self, signed_post):
        alarm = make_alarm()
        response = signed_post(command(alarm.id, channel="other-thread"))
        alarm.refresh_from_db()
        assert "hilo" in response.json()["data"]["content"]
        assert alarm.status == Alarm.Status.ACTIVE

    def test_resolves_manually(self, signed_post):
        alarm = make_alarm()
        signed_post(command(alarm.id, action="resolver"))
        alarm.refresh_from_db()
        assert alarm.status == Alarm.Status.RESOLVED
        assert alarm.resolution_type == Alarm.ResolutionType.MANUAL
