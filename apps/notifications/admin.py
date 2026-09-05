from django.contrib import admin

from .models import NotificationChannel, NotificationLog, WhatsAppInboundMessage


@admin.register(NotificationChannel)
class NotificationChannelAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "kind",
        "purpose",
        "whatsapp_phone_number_id",
        "destination_group_id",
        "min_severity",
        "enabled",
    )
    list_filter = ("kind", "purpose", "enabled")


@admin.register(WhatsAppInboundMessage)
class WhatsAppInboundMessageAdmin(admin.ModelAdmin):
    list_display = ("message_id", "group_id", "sender_id", "text", "received_at", "processed_at")
    search_fields = ("message_id", "group_id", "sender_id", "text")
    readonly_fields = (
        "message_id",
        "group_id",
        "sender_id",
        "text",
        "response_text",
        "received_at",
        "processed_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = (
        "alarm",
        "channel",
        "event",
        "status",
        "target_channel_id",
        "attempts",
        "created_at",
        "sent_at",
    )
    list_filter = ("status", "event", "channel")
    date_hierarchy = "created_at"
    readonly_fields = ("payload", "target_channel_id", "response_status", "last_error")
