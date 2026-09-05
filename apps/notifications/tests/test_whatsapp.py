import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
import responses
from django.utils import timezone

from apps.alarms.models import Alarm, AlarmRule, AlarmStatusHistory, Severity
from apps.notifications.commands import build_general_summary, execute_group_command
from apps.notifications.dispatcher import notify
from apps.notifications.models import NotificationChannel, NotificationLog, WhatsAppInboundMessage
from apps.notifications.tasks import send_notification
from apps.plants.models import Project, Zone


@pytest.fixture
def setup_alarms(db):
    sucre = Zone.objects.create(name="Sucre", slug="sucre", whatsapp_group_id="sucre@g.us")
    atlantico = Zone.objects.create(
        name="Atlántico", slug="atlantico", whatsapp_group_id="atlantico@g.us"
    )
    now = timezone.now()
    p_sucre = Project.objects.create(
        external_id=7001, name="Planta Sucre", zone=sucre, is_minifarm=True, synced_at=now
    )
    p_atlantico = Project.objects.create(
        external_id=7002,
        name="Planta Atlántico",
        zone=atlantico,
        is_minifarm=True,
        synced_at=now,
    )
    rule = AlarmRule.objects.get(code="project_no_generation")
    a_sucre = Alarm.objects.create(
        rule=rule,
        project=p_sucre,
        component_type=rule.component_type,
        severity=Severity.CRITICAL,
        dedup_key="wa:sucre",
        triggered_at=now,
        last_seen_at=now,
    )
    a_atlantico = Alarm.objects.create(
        rule=rule,
        project=p_atlantico,
        component_type=rule.component_type,
        severity=Severity.HIGH,
        dedup_key="wa:atlantico",
        triggered_at=now,
        last_seen_at=now,
    )
    realtime = NotificationChannel.objects.create(
        name="wa-realtime-test",
        kind=NotificationChannel.Kind.WHATSAPP,
        purpose=NotificationChannel.Purpose.REALTIME,
        env_key="WA_TEST_TOKEN",
        whatsapp_phone_number_id="phone-123",
        enabled=True,
    )
    general = NotificationChannel.objects.create(
        name="wa-general-test",
        kind=NotificationChannel.Kind.WHATSAPP,
        purpose=NotificationChannel.Purpose.GENERAL_SUMMARY,
        env_key="WA_TEST_TOKEN",
        whatsapp_phone_number_id="phone-123",
        destination_group_id="general@g.us",
        enabled=True,
    )
    return {
        "sucre": sucre,
        "atlantico": atlantico,
        "sucre_alarm": a_sucre,
        "atlantico_alarm": a_atlantico,
        "realtime": realtime,
        "general": general,
    }


@pytest.mark.django_db
class TestZoneCommands:
    def test_updates_alarm_from_its_own_zone_and_audits(self, setup_alarms):
        alarm = setup_alarms["sucre_alarm"]
        result = execute_group_command("sucre@g.us", "57300111", f"alarma {alarm.id} reconocer")

        alarm.refresh_from_db()
        assert alarm.status == Alarm.Status.ACKNOWLEDGED
        assert result.target_group_id == "sucre@g.us"
        history = AlarmStatusHistory.objects.get(alarm=alarm)
        assert history.source == "whatsapp"
        assert "57300111" in history.actor

    def test_rejects_alarm_from_a_different_zone(self, setup_alarms):
        alarm = setup_alarms["atlantico_alarm"]
        result = execute_group_command("sucre@g.us", "57300111", f"alarma {alarm.id} finalizar")

        alarm.refresh_from_db()
        assert alarm.status == Alarm.Status.ACTIVE
        assert "no pertenece a la zona Sucre" in result.text
        assert not AlarmStatusHistory.objects.filter(alarm=alarm).exists()

    def test_supports_maintenance_then_finish(self, setup_alarms):
        alarm = setup_alarms["sucre_alarm"]
        execute_group_command("sucre@g.us", "57300111", f"alarma {alarm.id} mantenimiento")
        alarm.refresh_from_db()
        assert alarm.status == Alarm.Status.IN_MAINTENANCE
        assert alarm.maintenance_started_at is not None

        execute_group_command("sucre@g.us", "57300111", f"alarma {alarm.id} finalizar")
        alarm.refresh_from_db()
        assert alarm.status == Alarm.Status.RESOLVED
        assert alarm.resolution_type == Alarm.ResolutionType.MANUAL

    def test_general_group_cannot_change_alarm(self, setup_alarms):
        alarm = setup_alarms["sucre_alarm"]
        result = execute_group_command("general@g.us", "57300111", f"alarma {alarm.id} finalizar")

        alarm.refresh_from_db()
        assert alarm.status == Alarm.Status.ACTIVE
        assert "solo de consulta" in result.text

    def test_summary_only_contains_projects_with_open_alarms_by_severity(self, setup_alarms):
        summary = build_general_summary()

        assert "CRÍTICA" in summary
        assert "ALTA" in summary
        assert "Planta Sucre" in summary
        assert "Planta Atlántico" in summary

    def test_summary_filters_project_with_partial_unaccented_name(self, setup_alarms):
        result = execute_group_command(
            "general@g.us", "57300111", "resumen proyecto Atlantico"
        )

        assert "Resumen de Planta Atlántico" in result.text
        assert "Planta Atlántico" in result.text
        assert "Planta Sucre" not in result.text

    def test_summary_suggests_projects_when_partial_name_is_ambiguous(self, setup_alarms):
        result = execute_group_command(
            "general@g.us", "57300111", "resumen proyecto Planta"
        )

        assert "Encontré varios proyectos" in result.text
        assert "Planta Sucre" in result.text
        assert "Planta Atlántico" in result.text

    def test_project_without_open_alarms_is_reported_as_healthy(self, setup_alarms):
        Project.objects.create(
            external_id=7003,
            name="Minigranja Joropo",
            is_minifarm=True,
            synced_at=timezone.now(),
        )

        result = execute_group_command(
            "general@g.us", "57300111", "resumen proyecto Joropo"
        )

        assert "Resumen de Minigranja Joropo" in result.text
        assert "no tiene alarmas abiertas" in result.text


@pytest.mark.django_db
class TestWhatsAppDispatch:
    def test_dispatches_realtime_only_to_project_zone(self, setup_alarms):
        alarm = setup_alarms["sucre_alarm"]
        with patch("apps.notifications.dispatcher.send_notification") as task:
            notify(alarm, NotificationLog.Event.OPENED)

        logs = NotificationLog.objects.filter(alarm=alarm)
        assert logs.count() == 1
        assert logs.get().channel == setup_alarms["realtime"]
        task.delay.assert_called_once()

    def test_escalation_and_auto_resolution_are_silent(self, setup_alarms):
        alarm = setup_alarms["sucre_alarm"]
        with patch("apps.notifications.dispatcher.send_notification") as task:
            notify(alarm, NotificationLog.Event.ESCALATED)
            notify(alarm, NotificationLog.Event.RESOLVED)

        assert not NotificationLog.objects.filter(alarm=alarm).exists()
        task.delay.assert_not_called()

    def test_sla_breach_is_never_published(self, setup_alarms):
        source = setup_alarms["sucre_alarm"]
        rule = AlarmRule.objects.get(code="alarm_sla_breach")
        breach = Alarm.objects.create(
            rule=rule,
            project=source.project,
            component_type=rule.component_type,
            severity=Severity.HIGH,
            dedup_key=f"sla-noise:{source.id}",
            triggered_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        with patch("apps.notifications.dispatcher.send_notification") as task:
            notify(breach, NotificationLog.Event.OPENED)

        assert not NotificationLog.objects.filter(alarm=breach).exists()
        task.delay.assert_not_called()

    @responses.activate
    def test_sends_group_payload_through_cloud_api(self, setup_alarms, monkeypatch, settings):
        settings.WHATSAPP_API_VERSION = "v23.0"
        monkeypatch.setenv("WA_TEST_TOKEN", "secret-token")
        url = "https://graph.facebook.com/v23.0/phone-123/messages"
        responses.post(url, json={"messages": [{"id": "wamid.1"}]}, status=200)
        log = NotificationLog.objects.create(
            alarm=setup_alarms["sucre_alarm"],
            channel=setup_alarms["realtime"],
            event=NotificationLog.Event.OPENED,
        )

        send_notification.run(log.id)

        log.refresh_from_db()
        assert log.status == NotificationLog.Status.SENT
        assert log.target_channel_id == "sucre@g.us"
        payload = json.loads(responses.calls[0].request.body)
        assert payload["recipient_type"] == "group"
        assert payload["to"] == "sucre@g.us"


@pytest.mark.django_db
class TestWhatsAppWebhook:
    def test_signed_command_is_idempotent(self, client, settings, setup_alarms):
        settings.WHATSAPP_APP_SECRET = "app-secret"
        alarm = setup_alarms["sucre_alarm"]
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.command-1",
                                        "from": "57300111",
                                        "type": "text",
                                        "context": {"group_id": "sucre@g.us"},
                                        "text": {"body": f"alarma {alarm.id} reconocer"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()

        with patch("apps.notifications.whatsapp_webhook.send_whatsapp_text") as task:
            first = client.post(
                "/whatsapp/webhook/",
                data=body,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=signature,
            )
            second = client.post(
                "/whatsapp/webhook/",
                data=body,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=signature,
            )

        assert first.status_code == second.status_code == 200
        alarm.refresh_from_db()
        assert alarm.status == Alarm.Status.ACKNOWLEDGED
        assert WhatsAppInboundMessage.objects.filter(message_id="wamid.command-1").count() == 1
        task.delay.assert_called_once()

    def test_webhook_verification(self, client, settings):
        settings.WHATSAPP_VERIFY_TOKEN = "verify-me"
        response = client.get(
            "/whatsapp/webhook/",
            {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"},
        )
        assert response.status_code == 200
        assert response.content == b"42"
