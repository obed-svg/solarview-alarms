from django.contrib import admin

from .models import Inverter, InverterStateObservation, MaintenanceWindow, Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("external_id", "name", "is_minifarm", "monitoring_enabled", "synced_at")
    list_filter = ("monitoring_enabled", "is_minifarm")
    search_fields = ("name", "external_id", "plant_code")


@admin.register(Inverter)
class InverterAdmin(admin.ModelAdmin):
    list_display = ("dev_name", "external_id", "project", "is_active", "synced_at")
    list_filter = ("is_active", "project")
    search_fields = ("dev_name", "external_id")


@admin.register(MaintenanceWindow)
class MaintenanceWindowAdmin(admin.ModelAdmin):
    list_display = ("project", "inverter", "starts_at", "ends_at", "reason", "created_by")
    list_filter = ("project",)
    date_hierarchy = "starts_at"


@admin.register(InverterStateObservation)
class InverterStateObservationAdmin(admin.ModelAdmin):
    """Vocabulario de estados observado en la flota (solo lectura: lo escribe el engine)."""

    list_display = ("state", "times_seen", "first_seen_at", "last_seen_at",
                    "first_project", "first_dev_name")
    readonly_fields = ("state", "times_seen", "first_seen_at", "last_seen_at",
                       "first_project", "first_dev_name")
    search_fields = ("state",)

    def has_add_permission(self, request):
        return False
