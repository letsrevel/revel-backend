from django.db import migrations


def create_close_polls_periodic_task(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = IntervalSchedule.objects.get_or_create(every=1, period="minutes")
    PeriodicTask.objects.update_or_create(
        name="Close polls due",
        defaults={
            "task": "polls.tasks.close_polls_due",
            "interval": schedule,
            "enabled": True,
        },
    )


def delete_close_polls_periodic_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    try:
        task = PeriodicTask.objects.get(name="Close polls due")
        task.delete()
    except PeriodicTask.DoesNotExist:
        pass  # Task already gone, nothing to do


class Migration(migrations.Migration):

    dependencies = [
        ("polls", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_close_polls_periodic_task, reverse_code=delete_close_polls_periodic_task),
    ]
