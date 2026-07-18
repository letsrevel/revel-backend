from django.db import migrations


def create_cleanup_expired_seat_holds_task(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="*/5",
        hour="*",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name="Cleanup expired seat holds",
        defaults={
            "task": "events.cleanup_expired_seat_holds",
            "crontab": schedule,
            "enabled": True,
        },
    )


def delete_cleanup_expired_seat_holds_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    try:
        task = PeriodicTask.objects.get(name="Cleanup expired seat holds")
        task.delete()
    except PeriodicTask.DoesNotExist:
        pass  # Task already gone, nothing to do


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0099_map_legacy_random_seat_assignment_mode"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(
            create_cleanup_expired_seat_holds_task, reverse_code=delete_cleanup_expired_seat_holds_task
        ),
    ]
