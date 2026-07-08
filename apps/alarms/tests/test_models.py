import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.alarms.models import (
    Alarm,
    AlarmRule,
    Category,
    ComponentType,
    NonComputableInterval,
    RuleConfig,
    Severity,
)
from apps.plants.models import Project


def make_rule(**kwargs) -> AlarmRule:
    defaults = {
        "code": "test_rule",  # código propio: el seed de la migración 0002 ya crea los reales
        "name": "Regla de prueba",
        "description": "Solo para tests de modelo.",
        "category": Category.WEATHER,
        "component_type": ComponentType.WEATHER_STATION,
        "default_severity": Severity.HIGH,
        "default_params": {"stale_minutes": 5, "poa_min_wm2": 100},
    }
    defaults.update(kwargs)
    return AlarmRule.objects.create(**defaults)


def make_project(**kwargs) -> Project:
    defaults = {"external_id": 146, "name": "El Son", "synced_at": timezone.now()}
    defaults.update(kwargs)
    return Project.objects.create(**defaults)


def make_alarm(rule, project, **kwargs) -> Alarm:
    now = timezone.now()
    defaults = {
        "component_type": rule.component_type,
        "severity": rule.default_severity,
        "dedup_key": Alarm.build_dedup_key(rule.code, project.external_id),
        "triggered_at": now,
        "last_seen_at": now,
        "evidence": {"poa": 640},
        "last_evidence": {"poa": 640},
    }
    defaults.update(kwargs)
    return Alarm.objects.create(rule=rule, project=project, **defaults)


@pytest.mark.django_db
class TestParamsResolution:
    def test_without_config_uses_defaults(self):
        rule, project = make_rule(), make_project()

        assert rule.params_for(project) == {"stale_minutes": 5, "poa_min_wm2": 100}

    def test_config_overrides_only_declared_keys(self):
        rule, project = make_rule(), make_project()
        RuleConfig.objects.create(rule=rule, project=project, params={"stale_minutes": 45})

        assert rule.params_for(project) == {"stale_minutes": 45, "poa_min_wm2": 100}

    def test_enabled_inheritance(self):
        rule, project = make_rule(), make_project()
        assert rule.is_enabled_for(project) is True

        config = RuleConfig.objects.create(rule=rule, project=project, enabled=None)
        assert rule.is_enabled_for(project) is True  # None hereda del catálogo

        config.enabled = False
        config.save()
        assert rule.is_enabled_for(project) is False

    def test_config_unique_per_rule_and_project(self):
        rule, project = make_rule(), make_project()
        RuleConfig.objects.create(rule=rule, project=project)

        with pytest.raises(IntegrityError):
            RuleConfig.objects.create(rule=rule, project=project)


@pytest.mark.django_db
class TestAlarmDedup:
    def test_build_dedup_key(self):
        assert Alarm.build_dedup_key("string_zero_current", 146, "inv:1571", "pv3") == (
            "string_zero_current:146:inv:1571:pv3"
        )
        assert Alarm.build_dedup_key("recloser_open", 146) == "recloser_open:146"

    def test_two_open_alarms_same_key_forbidden(self):
        rule, project = make_rule(), make_project()
        make_alarm(rule, project)

        with pytest.raises(IntegrityError):
            make_alarm(rule, project)

    def test_new_alarm_allowed_after_resolution(self):
        rule, project = make_rule(), make_project()
        first = make_alarm(rule, project)
        first.status = Alarm.Status.RESOLVED
        first.resolved_at = timezone.now()
        first.save()

        second = make_alarm(rule, project)  # no debe lanzar

        assert Alarm.objects.filter(dedup_key=second.dedup_key).count() == 2

    def test_acknowledged_still_counts_as_open(self):
        rule, project = make_rule(), make_project()
        alarm = make_alarm(rule, project)
        alarm.status = Alarm.Status.ACKNOWLEDGED
        alarm.save()

        with pytest.raises(IntegrityError):
            make_alarm(rule, project)


@pytest.mark.django_db
class TestSeverityRank:
    def test_ordering_for_escalation(self):
        assert Severity.rank(Severity.CRITICAL) > Severity.rank(Severity.HIGH)
        assert Severity.rank(Severity.HIGH) > Severity.rank(Severity.MEDIUM)
        assert Severity.rank(Severity.MEDIUM) > Severity.rank(Severity.LOW)


@pytest.mark.django_db
class TestNonComputableInterval:
    def test_unique_interval(self):
        project = make_project()
        start, end = timezone.now(), timezone.now()
        NonComputableInterval.objects.create(
            project=project, metric="pr", interval_start=start, interval_end=end,
            missing_inputs=["poa"],
        )

        with pytest.raises(IntegrityError):
            NonComputableInterval.objects.create(
                project=project, metric="pr", interval_start=start, interval_end=end,
            )
