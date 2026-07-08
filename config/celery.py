import os

from celery import Celery
from kombu import Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("solarview_alarms")
app.config_from_object("django.conf:settings", namespace="CELERY")

app.conf.task_default_queue = "default"
app.conf.task_queues = (
    Queue("default"),
    Queue("evaluation"),
    Queue("notifications"),
    Queue("sync"),
)
app.conf.task_routes = {
    "apps.alarms.tasks.*": {"queue": "evaluation"},
    "apps.notifications.tasks.*": {"queue": "notifications"},
    "apps.plants.tasks.*": {"queue": "sync"},
}

app.autodiscover_tasks()
