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

# Task execution settings
CELERY_TASK_TIME_LIMIT = 300  # Hard limit: kill task after 5 minutes
CELERY_TASK_SOFT_TIME_LIMIT = 240  # Soft limit: raise exception after 4 minutes
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # Only prefetch 1 task at a time (important for solo pool)
CELERY_TASK_ACKS_LATE = True  # Acknowledge tasks after completion (prevent task loss)
