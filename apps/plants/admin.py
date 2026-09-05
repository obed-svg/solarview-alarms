from django.contrib import admin

from .models import Inverter, InverterStateObservation, MaintenanceWindow, Project, Zone


class ProjectInline(admin.TabularInline):
    model = Project
    fields = ("external_id", "name", "monitoring_enabled")
    readonly_fields = ("external_id", "name")
    extra = 0


@admin.register(Zone)
class ZoneAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "whatsapp_group_id", "enabled", "project_count")
    list_filter = ("enabled",)
    search_fields = ("name", "slug", "whatsapp_group_id")
    prepopulated_fields = {"slug": ("name",)}
    inlines = (ProjectInline,)

    @admin.display(description="Proyectos")
    def project_count(self, zone):
        return zone.projects.count()


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("external_id", "name", "is_minifarm", "is_self_consumption",
                    "ignore_weather_station", "monitoring_enabled", "zone",
                    "synced_at")
    list_editable = ("zone",)
    list_filter = ("monitoring_enabled", "is_minifarm", "is_self_consumption",
                   "ignore_weather_station", "zone")
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
