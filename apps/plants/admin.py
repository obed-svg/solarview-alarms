from django.contrib import admin

from .models import Inverter, MaintenanceWindow, Project


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
