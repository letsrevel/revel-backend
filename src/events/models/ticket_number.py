"""Per-organization fiscal ticket numbering (EU layer 1, #1060/#1061/#1064)."""

from django.db import models


class TicketNumberSequence(models.Model):
    """The gap-free ticket-number counter of one organization.

    One row per organization, locked with ``SELECT ... FOR UPDATE`` while numbers are
    handed out (``events.service.ticket_number_service``), so numbers are unique and
    gap-free within the series: an assignment rolls back together with the counter.
    ``series`` is fixed when the row is created and never follows later slug renames,
    so a printed number never changes meaning.
    """

    organization = models.OneToOneField(
        "events.Organization",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="ticket_number_sequence",
    )
    series = models.CharField(max_length=16)
    last_number = models.PositiveBigIntegerField(default=0)

    def __str__(self) -> str:  # pragma: no cover
        """Series and last number handed out."""
        return f"{self.series} @ {self.last_number}"
