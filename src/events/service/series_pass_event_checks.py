"""Per-event checks for the series pass flows: eligibility, event capacity and blacklist.

A series pass issues one ticket per covered event, so it must honour the rules a normal
ticket purchase applies to each of those events:

- ``BlacklistGate`` (hard and fuzzy name match), ``EventStatusGate`` and ``FullProfileGate``
  are reused as-is via ``EligibilityService.check_gates``. The remaining gates are covered
  elsewhere: ``QuestionnaireGate`` by ``series_pass_questionnaires``; ``MembershipGate`` /
  ``InvitationGate`` by the pass's own ``purchasable_by`` rule (coverage requires PUBLIC
  events); ``TicketSalesGate`` and ``AvailabilityGate`` by the pass-level quote and the
  tier/event capacity assertions.
- Event capacity uses the counting rules of ``BatchTicketService.assert_event_capacity``
  (``event_seats_available``).

Lock order for every writer that uses these helpers: pass row, then tiers pk-ordered, then
events pk-ordered. This is the batch checkout's own order (tier -> event).
"""

import typing as t
from uuid import UUID

from accounts.models import RevelUser
from events.models import Event
from events.service.batch_ticket_service.capacity import event_seats_available
from events.service.event_manager import EventUserEligibility, UserIsIneligibleError
from events.service.event_manager.gates import BlacklistGate, EventStatusGate, FullProfileGate
from events.service.event_manager.service import EligibilityService

_PURCHASE_GATES = (BlacklistGate, EventStatusGate, FullProfileGate)


def check_event_eligibility(user: RevelUser, event: Event) -> EventUserEligibility | None:
    """Return the first per-event gate verdict that blocks ``user`` from ``event``, if any."""
    return EligibilityService(user, event).check_gates(_PURCHASE_GATES)


def assert_events_eligible(user: RevelUser, events: t.Iterable[Event]) -> None:
    """Assert ``user`` passes the blacklist, status and full-profile gates for every event.

    Callers exempt owners and staff themselves (the normal flow's ``PrivilegedAccessGate``).

    Raises:
        UserIsIneligibleError: With the first failing verdict (400 + eligibility payload).
    """
    # ponytail: one EligibilityService (a handful of queries) per covered event, so cost grows with the
    # number of covered events. Upgrade path: build the blacklist/profile verdicts once and only
    # re-evaluate the event-specific status gate per event.
    for event in events:
        verdict = check_event_eligibility(user, event)
        if verdict is not None:
            raise UserIsIneligibleError(str(verdict.reason), verdict)


def is_blocked_by_blacklist(user: RevelUser, event: Event) -> bool:
    """Whether the blacklist gate blocks ``user`` (hard or fuzzy, honouring member/whitelist bypass).

    The blacklist is organization-wide, so any one covered event answers for all of them.
    """
    return EligibilityService(user, event).check_gates((BlacklistGate,)) is not None


def lock_events_and_find_full(events: t.Iterable[Event], user: RevelUser) -> set[UUID]:
    """Row-lock the events pk-ordered and return the ids that cannot take one more ticket.

    Must run inside a transaction, after the caller's pass and tier locks.
    """
    locked = (
        Event.objects.select_for_update(of=("self",))
        .select_related("venue")
        .filter(pk__in=[event.pk for event in events])
        .order_by("pk")
    )
    full: set[UUID] = set()
    for event in locked:
        cap = event.effective_capacity
        if cap and event_seats_available(event, user, cap) <= 0:
            full.add(event.pk)
    return full
