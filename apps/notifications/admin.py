from django.contrib import admin

from .models import NotificationChannel, NotificationLog


@admin.register(NotificationChannel)
class NotificationChannelAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "env_key", "discord_channel_id", "min_severity", "enabled")
    list_filter = ("kind", "enabled")


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = (
        "alarm", "channel", "event", "status", "target_channel_id",
        "attempts", "created_at", "sent_at",
    )
    list_filter = ("status", "event", "channel")
    date_hierarchy = "created_at"
    readonly_fields = ("payload", "target_channel_id", "response_status", "last_error")
