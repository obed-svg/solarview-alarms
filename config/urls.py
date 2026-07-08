from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from django.views.generic import RedirectView


def health(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
]
