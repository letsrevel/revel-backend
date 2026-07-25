"""Batch ticket purchase package.

``BatchTicketService`` is the request-scoped workflow behind every ticket
checkout. It is assembled from four mixins, each owning one question:

- :mod:`.eligibility` — *may this buyer take this many tickets?*
- :mod:`.capacity` — *is there room for them?* (tier, event, sector)
- :mod:`.seats` — *which seats do they get?* (NONE / BEST_AVAILABLE / USER_CHOICE)
- :mod:`.tickets` — *write the rows* (``create_tickets`` + bulk_create side effects)
- :mod:`.checkout` — *what happens per payment method* (online / offline / at-the-door / free)

:mod:`.service` holds the orchestration (``create_batch``) and nothing else;
:mod:`.context` holds the constructor and the request-scoped state the mixins share.
"""

from .service import BatchTicketService

__all__ = ["BatchTicketService"]
