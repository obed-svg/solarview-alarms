from django.contrib import admin
from django.utils import timezone

from .models import Alarm, AlarmRule, EvaluationRun, NonComputableInterval, RuleConfig


@admin.register(AlarmRule)
class AlarmRuleAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "default_severity", "rule_group", "enabled")
    list_filter = ("category", "default_severity", "rule_group", "enabled")
    search_fields = ("code", "name")


@admin.register(RuleConfig)
class RuleConfigAdmin(admin.ModelAdmin):
    list_display = ("rule", "project", "enabled")
    list_filter = ("rule", "project")


@admin.register(Alarm)
class AlarmAdmin(admin.ModelAdmin):
    list_display = (
        "dedup_key", "severity", "status", "project", "triggered_at",
        "last_seen_at", "occurrence_count",
    )
    list_filter = ("status", "severity", "rule", "project")
    search_fields = ("dedup_key",)
    date_hierarchy = "triggered_at"
    readonly_fields = ("evidence", "last_evidence", "dedup_key", "occurrence_count")
    actions = ["acknowledge", "resolve_manually"]

    @admin.action(description="Reconocer (acknowledge)")
    def acknowledge(self, request, queryset):
        queryset.filter(status=Alarm.Status.ACTIVE).update(
            status=Alarm.Status.ACKNOWLEDGED,
            acknowledged_at=timezone.now(),
            acknowledged_by=request.user.email or request.user.username,
        )

    @admin.action(description="Resolver manualmente")
    def resolve_manually(self, request, queryset):
        queryset.exclude(status=Alarm.Status.RESOLVED).update(
            status=Alarm.Status.RESOLVED,
            resolved_at=timezone.now(),
            resolution_type=Alarm.ResolutionType.MANUAL,
        )


@admin.register(NonComputableInterval)
class NonComputableIntervalAdmin(admin.ModelAdmin):
    list_display = ("project", "inverter", "metric", "interval_start", "interval_end")
    list_filter = ("metric", "project")
    date_hierarchy = "interval_start"


@admin.register(EvaluationRun)
class EvaluationRunAdmin(admin.ModelAdmin):
    list_display = ("project", "rule_group", "started_at", "finished_at", "status")
    list_filter = ("status", "rule_group", "project")
    date_hierarchy = "started_at"
