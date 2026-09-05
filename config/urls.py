from django.contrib import admin
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.urls import path
from django.views.generic import RedirectView

from apps.notifications.discord_interactions import interactions
from apps.notifications.whatsapp_webhook import whatsapp_webhook


def health(request):
    try:
        connection.ensure_connection()
        cache_key = "healthcheck:redis"
        cache.set(cache_key, "ok", timeout=10)
        if cache.get(cache_key) != "ok":
            raise RuntimeError("cache no disponible")
    except Exception:
        return JsonResponse({"status": "error"}, status=503)
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("discord/interactions/", interactions, name="discord-interactions"),
    path("whatsapp/webhook/", whatsapp_webhook, name="whatsapp-webhook"),
]
