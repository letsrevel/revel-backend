"""The holder of a ticket or pass whose account may be erased (#1476).

An erased account keeps only a placeholder email and no name. The holder name and email then live on
the kept row (``Ticket.guest_name``/``holder_email``, ``HeldSeriesPass.holder_name``/``holder_email``).
These helpers give the value to show staff, so no call site shows the placeholder.
"""

import typing as t

if t.TYPE_CHECKING:
    from events.models import HeldSeriesPass, Payment, Ticket


def holder_email(holding: "Ticket | HeldSeriesPass") -> str:
    """The kept holder email when the account is erased, else the account email."""
    return holding.holder_email or holding.user.email


def pass_holder_name(held_pass: "HeldSeriesPass") -> str:
    """The kept holder name of an erased pass holder, else the account's display name.

    An erased holder without a kept name falls back to ``get_display_name()`` ("Deleted user").
    """
    return held_pass.holder_name or held_pass.user.get_display_name()


def ticket_holder_name(ticket: "Ticket") -> str:
    """The name a ticket shows for its holder: the kept name for an erased account, else the account's name.

    An erased holder without a kept name falls back to ``get_display_name()`` ("Deleted user"), never "".
    """
    if ticket.user.erased_at:
        return ticket.guest_name or ticket.user.get_display_name()
    return ticket.user.get_display_name()


def stripe_payload_to_store(payment: "Payment", payload: t.Any) -> t.Any:
    """The value to store in ``Payment.raw_response``: ``payload``, or ``{}`` when the buyer's account is erased.

    A Stripe event holds the buyer's billing details (name, email, address). The erasure clears the stored
    payload of a kept payment, and a later event (a refund, a late completion) must not put it back. Every
    writer of ``Payment.raw_response`` calls this while it holds the ``Payment`` row lock: the erasure takes
    that lock too, so the account is read either before the erasure clears the payload or after it commits.
    """
    from accounts.models import RevelUser

    if RevelUser.objects.filter(pk=payment.user_id, erased_at__isnull=False).exists():
        return {}
    return payload
