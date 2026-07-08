from django.conf import settings

from config.celery import app


def test_celery_app_configured():
    assert app.main == "solarview_alarms"
    assert app.conf.task_default_queue == "default"


def test_task_queues_defined():
    queue_names = {q.name for q in app.conf.task_queues}
    assert {"default", "evaluation", "notifications", "sync"} <= queue_names


def test_beat_uses_database_scheduler():
    assert settings.CELERY_BEAT_SCHEDULER == "django_celery_beat.schedulers:DatabaseScheduler"


def test_broker_from_settings():
    assert app.conf.broker_url == settings.REDIS_URL
