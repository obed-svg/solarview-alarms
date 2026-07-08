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
    """Dashboard operativo: ¿está corriendo bien la evaluación por proyecto?"""

    list_display = (
        "project", "rule_group", "started_at", "duration", "status",
        "opened", "resolved", "errors",
    )
    list_filter = ("status", "rule_group", "project")
    date_hierarchy = "started_at"
    readonly_fields = ("stats",)

    @admin.display(description="Duración")
    def duration(self, run):
        if not run.finished_at:
            return "—"
        return f"{(run.finished_at - run.started_at).total_seconds():.1f}s"

    @admin.display(description="Abiertas")
    def opened(self, run):
        return run.stats.get("opened", 0)

    @admin.display(description="Resueltas")
    def resolved(self, run):
        return run.stats.get("resolved", 0)

    @admin.display(description="Errores")
    def errors(self, run):
        return ", ".join(run.stats.get("errors", {})) or "—"
