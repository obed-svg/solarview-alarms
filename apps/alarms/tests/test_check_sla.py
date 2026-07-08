from datetime import timedelta

import pytest
from django.utils import timezone

from apps.alarms.models import Alarm, AlarmRule, Severity
from apps.alarms.tasks import check_sla
from apps.plants.models import Project


@pytest.fixture
def project(db):
    return Project.objects.create(external_id=146, name="El Son", synced_at=timezone.now())


def make_source_alarm(project, age_minutes, status=Alarm.Status.ACTIVE):
    rule = AlarmRule.objects.get(code="weather_comm_lost")
    triggered = timezone.now() - timedelta(minutes=age_minutes)
    return Alarm.objects.create(
        rule=rule, project=project, component_type=rule.component_type,
        severity=rule.default_severity,
        dedup_key=Alarm.build_dedup_key(rule.code, project.external_id),
        triggered_at=triggered, last_seen_at=triggered, status=status,
    )


def breaches():
    return Alarm.objects.filter(rule__code="alarm_sla_breach")


@pytest.mark.django_db
class TestCheckSla:
    def test_stale_active_alarm_opens_breach(self, project):
        source = make_source_alarm(project, age_minutes=90)  # SLA default 60

        check_sla()

        breach = breaches().get()
        assert breach.status == Alarm.Status.ACTIVE
        assert breach.severity == Severity.HIGH
        assert breach.dedup_key == f"alarm_sla_breach:146:alarm:{source.id}"
        assert breach.evidence["source_rule"] == "weather_comm_lost"

    def test_fresh_alarm_no_breach(self, project):
        make_source_alarm(project, age_minutes=30)

        check_sla()

        assert breaches().count() == 0

    def test_acknowledged_alarm_no_breach(self, project):
        make_source_alarm(project, age_minutes=90, status=Alarm.Status.ACKNOWLEDGED)

        check_sla()

        assert breaches().count() == 0

    def test_escalates_to_critical_after_double_sla(self, project):
        make_source_alarm(project, age_minutes=150)  # > 2×60

        check_sla()

        assert breaches().get().severity == Severity.CRITICAL

    def test_idempotent_runs_keep_single_breach(self, project):
        make_source_alarm(project, age_minutes=90)

        check_sla()
        check_sla()

        assert breaches().count() == 1
        assert breaches().get().occurrence_count == 2

    def test_breach_resolves_when_source_acknowledged(self, project):
        source = make_source_alarm(project, age_minutes=90)
        check_sla()
        source.status = Alarm.Status.ACKNOWLEDGED
        source.acknowledged_at = timezone.now()
        source.save()

        check_sla()

        breach = breaches().get()
        assert breach.status == Alarm.Status.RESOLVED
        assert breach.resolution_type == Alarm.ResolutionType.AUTO

    def test_no_breach_of_breaches(self, project):
        make_source_alarm(project, age_minutes=90)
        check_sla()
        # envejecer el breach artificialmente
        breaches().update(triggered_at=timezone.now() - timedelta(minutes=300))

        check_sla()

        assert breaches().count() == 1  # sin recursión sobre sí misma
