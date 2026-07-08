from django.db import migrations

SCHEDULES = [
    # (name, task, crontab(min, hora), kwargs, queue)
    ("alarms: evaluación fast (5 min)", "apps.alarms.tasks.dispatch_evaluations",
     ("*/5", "*"), '{"rule_group": "fast"}'),
    ("alarms: evaluación hourly", "apps.alarms.tasks.dispatch_evaluations",
     ("5", "*"), '{"rule_group": "hourly"}'),
    ("alarms: check SLA", "apps.alarms.tasks.check_sla", ("*/10", "*"), "{}"),
    ("plants: sync catálogo", "apps.plants.tasks.sync_catalog", ("15", "*"), "{}"),
]


def seed_schedules(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    for name, task, (minute, hour), kwargs in SCHEDULES:
        crontab, _ = CrontabSchedule.objects.get_or_create(
            minute=minute, hour=hour, day_of_week="*", day_of_month="*",
            month_of_year="*", timezone="America/Bogota",
        )
        PeriodicTask.objects.update_or_create(
            name=name,
            defaults={"task": task, "crontab": crontab, "kwargs": kwargs, "enabled": True},
        )


def unseed_schedules(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=[s[0] for s in SCHEDULES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("alarms", "0002_seed_rules"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(seed_schedules, unseed_schedules)]
