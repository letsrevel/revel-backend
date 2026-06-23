"""Email logging model."""

import gzip

from django.db import models

from common.models.base import TimeStampedModel


class EmailLog(TimeStampedModel):
    to = models.EmailField(db_index=True)
    subject = models.TextField(db_index=True)
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)
    test_only = models.BooleanField(default=False, db_index=True)
    compressed_body = models.BinaryField(null=True, blank=True)
    compressed_html = models.BinaryField(null=True, blank=True)

    def set_body(self, body: str) -> None:
        """Compress and set text."""
        self.compressed_body = gzip.compress(body.encode())

    def set_html(self, html_body: str) -> None:
        """Compress and set html."""
        self.compressed_html = gzip.compress(html_body.encode())

    @property
    def body(self) -> str | None:
        """Decompress and return text."""
        if self.compressed_body:
            return gzip.decompress(self.compressed_body).decode()
        return None

    @property
    def html(self) -> str | None:
        """Decompress and return html."""
        if self.compressed_html:
            return gzip.decompress(self.compressed_html).decode()
        return None

    def __str__(self) -> str:
        return f"Email to: {self.to}"

    class Meta:
        indexes = [
            models.Index(fields=["to", "sent_at", "test_only"], name="ix_emaillog_to_sentat_testonly"),
        ]
