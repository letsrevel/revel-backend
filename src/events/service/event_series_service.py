"""Service for generic EventSeries lifecycle operations.

Recurring-specific series operations (pause/resume/generate/propagate) live in
``recurrence_service``; this module is for operations that apply to any
EventSeries, whether or not it has a recurrence rule.
"""

from django.db import transaction
from django.db.models import ProtectedError
from django.utils.translation import gettext_lazy as _

from events.exceptions import SeriesPassHasHoldersError
from events.models import EventSeries


def delete_event_series(series: EventSeries) -> None:
    """Permanently delete an EventSeries, refusing when a series pass has holders.

    ``EventSeries -> SeriesPass`` cascades (``CASCADE``), but ``HeldSeriesPass.series_pass``
    is ``PROTECT`` to preserve the purchase/attendance audit trail — so a series with ANY
    held pass, even a CANCELLED one, raises ``ProtectedError`` deep in the cascade. Surfaced
    here as the same 409 ``series_pass_service.delete_series_pass`` raises for the
    single-pass case, instead of an unhandled 500.

    Args:
        series: The EventSeries to delete.

    Raises:
        SeriesPassHasHoldersError: If deleting would violate a protected FK from one of
            the series' passes' historical holder records.
    """
    try:
        # EventSeries.delete() nulls its PROTECT-ing FKs in a separate UPDATE before the
        # cascade; atomic() keeps both as one unit so a ProtectedError mid-cascade can't
        # leave the series stripped of template_event/recurrence_rule outside ATOMIC_REQUESTS.
        with transaction.atomic():
            series.delete()
    except ProtectedError as exc:
        raise SeriesPassHasHoldersError(
            str(_("Cannot delete a series with sold series passes; cancel or delete its passes first."))
        ) from exc
