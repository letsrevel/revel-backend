from celery.schedules import crontab
from decouple import config

from .base import DEBUG, REDIS_HOST, TIME_ZONE

CELERY_REDIS_DB = config("CELERY_REDIS_DB", default=0, cast=int)

# CELERY
CELERY_BROKER_URL = f"redis://{REDIS_HOST}:6379/{CELERY_REDIS_DB}"
CELERY_ACCEPT_CONTENT = ["application/json"]
CELERY_RESULT_EXTENDED = True
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_RESULT_BACKEND = "django-db"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ALWAYS_EAGER = config("CELERY_TASK_ALWAYS_EAGER", cast=bool, default=DEBUG)
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Celery Beat Schedule
CELERY_BEAT_SCHEDULE = {
    "send-notification-digests": {
        "task": "notifications.tasks.send_notification_digests",
        "schedule": crontab(minute=0),  # Every hour
    },
    "cleanup-old-notifications": {
        "task": "notifications.tasks.cleanup_old_notifications",
        "schedule": crontab(hour=2, minute=0),  # Daily at 2 AM
    },
    "retry-failed-deliveries": {
        "task": "notifications.tasks.retry_failed_deliveries",
        "schedule": crontab(hour="*/6", minute=0),  # Every 6 hours
    },
}
