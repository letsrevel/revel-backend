"""API models."""

from django.conf import settings
from django.db import models


def get_version() -> str:
    """Get the current version of the application."""
    return settings.VERSION


class Error(models.Model):
    md5 = models.CharField(max_length=32, unique=True)
    path = models.CharField(max_length=2048, db_index=True)
    server_version = models.CharField(max_length=32, default=get_version, db_index=True)
    traceback = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    issue_url = models.URLField(null=True, blank=True)
    issue_solved = models.BooleanField(default=False)
    payload = models.BinaryField(null=True, blank=True)
    json_payload = models.JSONField(null=True, blank=True)
    request_metadata = models.JSONField(null=True, blank=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.path} ({self.created_at.strftime('%Y-%m-%d %H:%M:%S')})"

    class Meta:
        ordering = ["-created_at"]


class ErrorOccurrence(models.Model):
    signature = models.ForeignKey(Error, on_delete=models.CASCADE)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.signature.path} ({self.timestamp.strftime('%Y-%m-%d %H:%M:%S')})"
