"""Holder-name (``Ticket.guest_name``) operations.

Split out of ``ticket_service`` rather than added to it: that module sits at 997 of the
1000-line ceiling enforced by ``make file-length``, so it has no room left. Holder-name
rules are cohesive enough to stand alone — see issue #845.
"""

from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import Ticket


def update_guest_name(ticket: Ticket, guest_name: str) -> Ticket:
    """Rename the ticket holder. Owner and organizer surfaces share this rule set.

    Args:
        ticket: The ticket to rename.
        guest_name: The new holder name; empty clears it.

    Returns:
        The saved ticket.

    Raises:
        HttpError: 409 for checked-in/cancelled tickets; 400 when clearing the
            name on an event that requires names.
    """
    name = guest_name.strip()
    if ticket.status in (Ticket.TicketStatus.CHECKED_IN, Ticket.TicketStatus.CANCELLED):
        raise HttpError(409, str(_("This ticket can no longer be renamed.")))
    if not name and ticket.event.require_ticket_names:
        raise HttpError(400, str(_("This event requires a name on every ticket.")))
    ticket.guest_name = name
    # "updated_at" must be listed explicitly: Django only runs pre_save (and therefore
    # auto_now) for fields named in update_fields. The ticket-file cache key is built from
    # these timestamps (ticket_file_service.compute_content_hash), so omitting it leaves
    # is_cache_valid() True and the stale PDF/pkpass — both of which render guest_name —
    # would be served forever.
    ticket.save(update_fields=["guest_name", "updated_at"])
    return ticket
