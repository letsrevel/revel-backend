"""Celery setup for FHIR Engine."""

import os

from celery import Celery

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "revel.settings")

app = Celery("revel")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django apps.
app.autodiscover_tasks()


# run:
# celery -A revel worker -l INFO
# celery -A revel beat -l INFO --scheduler django_celery_beat.schedulers:DatabaseScheduler
