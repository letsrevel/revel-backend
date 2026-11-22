"""Model that records post-deploy task runs (#1508)."""

from django.db import models

from common.models.base import TimeStampedModel


class PostDeployTaskRun(TimeStampedModel):
    """The latest run of one registered post-deploy task.

    Rows are created by ``common.post_deploy.run_pending``. Only a ``SUCCEEDED`` row is terminal.
    """

    # ponytail: one row per task (latest attempt only); upgrade path = a child table of attempts

    class Status(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    task_id = models.CharField(max_length=200, unique=True)
    release = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.FAILED, db_default=Status.FAILED)
    attempts = models.PositiveIntegerField(default=0, db_default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    output = models.TextField(blank=True, default="", db_default="")

    def __str__(self) -> str:
        return f"{self.task_id} ({self.status})"
