"""Google Wallet save-link service (function-based, stateless)."""

from events.models import HeldSeriesPass, Ticket
from wallet.google.builder import build_series_pass_payload, build_ticket_payload
from wallet.google.signer import GooglePassSigner


def ticket_save_url(ticket: Ticket) -> str:
    """Build a signed Google Wallet save link for a ticket."""
    return GooglePassSigner().save_url(build_ticket_payload(ticket))


def series_pass_save_url(held_pass: HeldSeriesPass) -> str:
    """Build a signed Google Wallet save link for a held series pass."""
    return GooglePassSigner().save_url(build_series_pass_payload(held_pass))
