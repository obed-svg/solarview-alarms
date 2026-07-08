import pytest

from apps.alarms.catalog import CATALOG
from apps.alarms.models import AlarmRule, RuleGroup, Severity


@pytest.mark.django_db
class TestSeededCatalog:
    def test_all_20_rules_seeded(self):
        assert AlarmRule.objects.count() == 20
        assert {r.code for r in AlarmRule.objects.all()} == {e["code"] for e in CATALOG}

    def test_only_thd_disabled(self):
        # tmod_invalid se habilitó al definirse T_mod = temperature_POA (mig. 0004)
        assert AlarmRule.objects.get(code="tmod_invalid").enabled is True
        assert AlarmRule.objects.get(code="thd_abnormal").enabled is False
        assert AlarmRule.objects.filter(enabled=True).count() == 19

    def test_cox_params_present(self):
        weather = AlarmRule.objects.get(code="weather_comm_lost")
        assert weather.default_params["stale_minutes"] == 5

        string_low = AlarmRule.objects.get(code="string_low_current")
        assert string_low.default_params["low_ratio"] == 0.8
        assert string_low.default_params["poa_min_wm2"] == 100

        mismatch = AlarmRule.objects.get(code="meter_inverter_mismatch")
        assert mismatch.default_params == {
            "window_minutes": 60, "alert_ratio": 0.03, "high_ratio": 0.05,
        }

    def test_meter_rules_are_hourly(self):
        hourly = set(
            AlarmRule.objects.filter(rule_group=RuleGroup.HOURLY).values_list("code", flat=True)
        )
        assert {"meter_comm_lost", "meter_no_increment", "meter_inverter_mismatch"} <= hourly

    def test_critical_rules(self):
        criticals = set(
            AlarmRule.objects.filter(default_severity=Severity.CRITICAL).values_list(
                "code", flat=True
            )
        )
        assert {
            "project_no_generation", "inverter_unavailable", "string_zero_current",
            "dc_isolation_low", "recloser_open", "pr_inputs_missing",
        } <= criticals
