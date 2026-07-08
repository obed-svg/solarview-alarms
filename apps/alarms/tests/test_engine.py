from datetime import datetime
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from apps.alarms.engine import evaluate_project
from apps.alarms.models import Alarm, AlarmRule, EvaluationRun, Severity
from apps.alarms.rules.base import RULES, BaseRule, RuleOutcome, register
from apps.plants.models import Project

NOW = datetime(2026, 7, 8, 12, 0)


@pytest.fixture(autouse=True)
def isolated_rules():
    """Estos tests prueban el ENGINE con reglas sintéticas: se vacía el registro
    de reglas reales para que no corran con clients MagicMock."""
    saved = dict(RULES)
    RULES.clear()
    yield
    RULES.clear()
    RULES.update(saved)


@pytest.fixture
def project(db):
    return Project.objects.create(external_id=146, name="El Son", synced_at=timezone.now())


@pytest.fixture
def scripted_rule(db):
    """Regla de prueba cuyo resultado se programa por test. Se registra y limpia."""
    AlarmRule.objects.create(
        code="scripted", name="Scripted", category="project", component_type="project",
        default_severity=Severity.HIGH, default_params={},
    )

    @register
    class ScriptedRule(BaseRule):
        code = "scripted"
        phase = 3
        outcomes: list[RuleOutcome] = []

        def evaluate(self, ctx):
            return list(type(self).outcomes)

    yield ScriptedRule
    RULES.pop("scripted", None)


def run(project, **kwargs):
    return evaluate_project(project, client=MagicMock(), now=NOW, **kwargs)


@pytest.mark.django_db
class TestFiring:
    def test_creates_alarm_with_evidence(self, project, scripted_rule):
        scripted_rule.outcomes = [
            RuleOutcome(status="firing", evidence={"poa": 640, "power": 0.0})
        ]

        run(project)

        alarm = Alarm.objects.get()
        assert alarm.rule.code == "scripted"
        assert alarm.status == Alarm.Status.ACTIVE
        assert alarm.evidence == {"poa": 640, "power": 0.0}
        assert alarm.dedup_key == "scripted:146"
        assert alarm.occurrence_count == 1

    def test_persisting_condition_updates_not_duplicates(self, project, scripted_rule):
        scripted_rule.outcomes = [RuleOutcome(status="firing", evidence={"n": 1})]
        run(project)
        scripted_rule.outcomes = [RuleOutcome(status="firing", evidence={"n": 2})]

        run(project)

        alarm = Alarm.objects.get()  # sigue habiendo UNA
        assert alarm.occurrence_count == 2
        assert alarm.evidence == {"n": 1}  # snapshot original inmutable
        assert alarm.last_evidence == {"n": 2}

    def test_severity_escalates_but_never_downgrades(self, project, scripted_rule):
        scripted_rule.outcomes = [RuleOutcome(status="firing", severity=Severity.HIGH)]
        run(project)
        scripted_rule.outcomes = [RuleOutcome(status="firing", severity=Severity.CRITICAL)]
        run(project)

        alarm = Alarm.objects.get()
        assert alarm.severity == Severity.CRITICAL

        scripted_rule.outcomes = [RuleOutcome(status="firing", severity=Severity.MEDIUM)]
        run(project)
        alarm.refresh_from_db()
        assert alarm.severity == Severity.CRITICAL  # no baja


@pytest.mark.django_db
class TestOkResolution:
    def test_ok_resolves_immediately(self, project, scripted_rule):
        scripted_rule.outcomes = [RuleOutcome(status="firing")]
        run(project)
        scripted_rule.outcomes = [RuleOutcome(status="ok")]

        run(project)

        alarm = Alarm.objects.get()
        assert alarm.status == Alarm.Status.RESOLVED
        assert alarm.resolution_type == Alarm.ResolutionType.AUTO
        assert alarm.resolved_at is not None

    def test_ok_without_open_alarm_does_nothing(self, project, scripted_rule):
        scripted_rule.outcomes = [RuleOutcome(status="ok")]

        run(project)

        assert Alarm.objects.count() == 0

    def test_auto_resolve_false_keeps_alarm_open(self, project, scripted_rule):
        AlarmRule.objects.filter(code="scripted").update(auto_resolve=False)
        scripted_rule.outcomes = [RuleOutcome(status="firing")]
        run(project)
        scripted_rule.outcomes = [RuleOutcome(status="ok")]

        run(project)

        assert Alarm.objects.get().status == Alarm.Status.ACTIVE


@pytest.mark.django_db
class TestNotComputable:
    def test_does_not_touch_open_alarm(self, project, scripted_rule):
        scripted_rule.outcomes = [RuleOutcome(status="firing")]
        run(project)
        scripted_rule.outcomes = [
            RuleOutcome(status="not_computable", reason="api_caida")
        ]

        run(project)

        alarm = Alarm.objects.get()
        assert alarm.status == Alarm.Status.ACTIVE
        assert alarm.occurrence_count == 1  # no cuenta como visto

    def test_does_not_create_alarm(self, project, scripted_rule):
        scripted_rule.outcomes = [RuleOutcome(status="not_computable", reason="sin_poa")]

        run(project)

        assert Alarm.objects.count() == 0


@pytest.mark.django_db
class TestRunBookkeeping:
    def test_success_run_recorded(self, project, scripted_rule):
        scripted_rule.outcomes = [RuleOutcome(status="firing")]

        result = run(project)

        assert result.status == EvaluationRun.Status.SUCCESS
        assert result.stats["opened"] == 1
        assert result.finished_at is not None

    def test_crashing_rule_marks_partial_and_others_still_run(self, project, scripted_rule):
        AlarmRule.objects.create(
            code="broken", name="Broken", category="project", component_type="project",
            default_severity=Severity.HIGH,
        )

        @register
        class BrokenRule(BaseRule):
            code = "broken"
            phase = 1

            def evaluate(self, ctx):
                raise RuntimeError("boom")

        scripted_rule.outcomes = [RuleOutcome(status="firing")]
        try:
            result = run(project)
        finally:
            RULES.pop("broken", None)

        assert result.status == EvaluationRun.Status.PARTIAL
        assert "broken" in result.stats["errors"]
        assert Alarm.objects.filter(rule__code="scripted").exists()  # la otra sí corrió

    def test_rule_disabled_for_project_is_skipped(self, project, scripted_rule):
        from apps.alarms.models import RuleConfig

        RuleConfig.objects.create(
            rule=AlarmRule.objects.get(code="scripted"), project=project, enabled=False
        )
        scripted_rule.outcomes = [RuleOutcome(status="firing")]

        run(project)

        assert Alarm.objects.count() == 0


@pytest.mark.django_db
class TestPhases:
    def test_phase1_firing_visible_to_phase3_via_flags(self, project):
        AlarmRule.objects.create(
            code="p1", name="P1", category="inverter", component_type="inverter",
            default_severity=Severity.HIGH,
        )
        AlarmRule.objects.create(
            code="p3", name="P3", category="project", component_type="project",
            default_severity=Severity.HIGH,
        )
        seen = {}

        @register
        class Phase1(BaseRule):
            code = "p1"
            phase = 1

            def evaluate(self, ctx):
                return [RuleOutcome(status="firing", dedup_suffix="inv:5")]

        @register
        class Phase3(BaseRule):
            code = "p3"
            phase = 3

            def evaluate(self, ctx):
                seen["p1_firing"] = ctx.flag_active("p1", "inv:5")
                return [RuleOutcome(status="ok")]

        try:
            run(project)
        finally:
            RULES.pop("p1", None)
            RULES.pop("p3", None)

        assert seen["p1_firing"] is True
