from django.db import migrations


def create_task(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="3",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )
    PeriodicTask.objects.update_or_create(
        name="Sweep food items for blocklist matches",
        defaults={
            "task": "moderation.tasks.sweep_food_items_for_blocklist",
            "crontab": schedule,
            "enabled": True,
        },
    )


def remove_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Sweep food items for blocklist matches").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("moderation", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]
    operations = [migrations.RunPython(create_task, remove_task)]
