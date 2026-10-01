"""Per-organization fiscal ticket numbering (EU layer 1, #1060/#1061/#1064)."""

import typing as t

from django.db import models

_NUMBER_FIELDS = frozenset({"ticket_series", "ticket_number", "issued_at"})


class PreserveTicketNumberMixin(models.Model):
    """Keep a full ``save()`` of a stale instance from wiping the ticket's fiscal number.

    Numbers are written by ``ticket_number_service`` with ``bulk_update``, so an instance
    loaded before numbering still holds ``ticket_number=None``; a bare ``save()`` (the
    Django/Unfold admin's ``save_model``) would write that ``None`` back. When an existing
    row is fully saved while its in-memory number is unset, every concrete non-pk field
    except the number fields (and except deferred fields) is saved instead.
    """

    class Meta:
        abstract = True

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Save, leaving the number fields alone on a full save of an unnumbered instance."""
        full_save = not args and kwargs.get("update_fields") is None and not kwargs.get("force_insert")
        if full_save and not self._state.adding and getattr(self, "ticket_number", None) is None:
            deferred = self.get_deferred_fields()
            kwargs["update_fields"] = [
                f.name
                for f in self._meta.concrete_fields
                if not f.primary_key and f.name not in _NUMBER_FIELDS and f.attname not in deferred
            ]
        super().save(*args, **kwargs)


class TicketNumberSequence(models.Model):
    """The ticket-number counter of one organization.

    One row per organization, locked with ``SELECT ... FOR UPDATE`` while numbers are
    handed out (``events.service.ticket_number_service``), so numbers are unique, assigned
    without gaps (an assignment rolls back together with the counter) and never reused.
    Holes appear only if numbered tickets are deleted (event / tier / user cascade, #1068).
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
