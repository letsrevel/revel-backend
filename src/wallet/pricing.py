"""Shared price resolution for wallet passes (Apple and Google rails).

The resolver lives in :mod:`events.service.ticket_price` so the ticket PDF's compliance
fields (``events.compliance``) can use it too; re-exported here for the wallet rails.
"""

from events.service.ticket_price import resolve_ticket_price

__all__ = ["resolve_ticket_price"]
