"""Public seating endpoints: chart, availability, and TTL seat holds."""

import typing as t
from uuid import UUID

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja import Body
from ninja.errors import HttpError
from ninja_extra import api_controller, route

from accounts.models import RevelUser
from common.authentication import OptionalAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.service.guest_hold_session import (
    GUEST_HOLD_COOKIE,
    issue_guest_hold_token,
    resolve_guest_session,
)
from events.service.seating import availability as availability_service
from events.service.seating import chart as chart_service
from events.service.seating import holds as holds_service
from events.service.seating import pick as pick_service

from .base import EventPublicBaseController

_GUEST_COOKIE_MAX_AGE = 86400


@api_controller(
    "/events",
    tags=["Event Seating"],
    auth=OptionalAuth(),
    throttle=UserDefaultThrottle(),
)
class EventPublicSeatingController(EventPublicBaseController):
    """Chart, availability, and seat holds for one event."""

    def _optional_user(self) -> RevelUser | None:
        """Current authenticated user, or None for anonymous requests."""
        user = self.maybe_user()
        return user if user.is_authenticated else None

    def _resolve_guest_session(self) -> str | None:
        """Resolve an existing guest-hold cookie, if present and valid."""
        return resolve_guest_session(self.context.request.COOKIES.get(GUEST_HOLD_COOKIE))  # type: ignore[union-attr]

    def _ensure_guest_session_if_anonymous(self, user: RevelUser | None) -> str | None:
        """For anonymous holds, resolve or mint a signed guest session and set the cookie."""
        if user is not None:
            return None
        existing = self._resolve_guest_session()
        if existing is not None:
            return existing
        session_id, token = issue_guest_hold_token()
        self.context.response.set_cookie(  # type: ignore[union-attr]
            GUEST_HOLD_COOKIE,
            token,
            max_age=_GUEST_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
            secure=not settings.DEBUG,
        )
        return session_id

    @route.get(
        "/{uuid:event_id}/seating/chart",
        url_name="event_seating_chart",
        response=schema.VenueChartSchema,
    )
    def get_chart(self, event_id: UUID) -> schema.VenueChartSchema:
        """Return the render-ready seating chart (sectors, seats, price categories) for the event's venue."""
        event = self.get_one(event_id)
        if not event.venue_id:
            raise HttpError(404, "This event has no venue.")
        # build_chart does its own prefetch (sectors, seats, price categories) — pass a plain venue.
        venue = models.Venue.objects.get(pk=event.venue_id)
        return chart_service.build_chart(venue)

    @route.get(
        "/{uuid:event_id}/seating/availability",
        url_name="event_seating_availability",
        response=schema.SeatingAvailabilitySchema,
    )
    def get_availability(self, event_id: UUID) -> schema.SeatingAvailabilitySchema:
        """Return the sparse per-seat availability plus standing counts and the caller's own holds."""
        event = self.get_one(event_id)
        return availability_service.build_availability(
            event, user=self._optional_user(), guest_session=self._resolve_guest_session()
        )

    @route.post(
        "/{uuid:event_id}/seating/holds",
        url_name="event_seating_hold",
        response={200: schema.HoldResponseSchema, 409: schema.HoldResponseSchema},
        throttle=WriteThrottle(),
    )
    def hold_seats(self, event_id: UUID, payload: schema.HoldSeatsRequest) -> tuple[int, schema.HoldResponseSchema]:
        """Acquire TTL holds on the requested seats (all-or-nothing); 409 with conflicts if any are taken.

        # ponytail: guest tokens are free to mint, so the per-identity hold cap is only
        # advisory against token churn — the WriteThrottle here is the real griefing
        # ceiling. Upgrade path: a stricter per-IP/per-endpoint throttle if abuse appears.
        """
        event = self.get_one(event_id)
        user = self._optional_user()
        guest = self._ensure_guest_session_if_anonymous(user)
        result = holds_service.acquire_seats(event, payload.seat_ids, user=user, guest_session=guest)
        status = 409 if result.conflicts else 200
        return status, schema.HoldResponseSchema(
            held_seat_ids=[h.seat_id for h in result.held],
            conflicts=result.conflicts,
            conflict_reason=result.conflict_reason,
            expires_at=result.expires_at,
        )

    @route.post(
        "/{uuid:event_id}/seating/holds/best-available",
        url_name="event_seating_hold_best",
        response={200: schema.HoldResponseSchema, 409: schema.HoldResponseSchema},
        throttle=WriteThrottle(),
    )
    def hold_best_available(
        self, event_id: UUID, payload: schema.BestAvailableHoldRequest
    ) -> tuple[int, schema.HoldResponseSchema]:
        """Optimistically hold the best adjacent block for the tier's category; 409 if none fits.

        Same griefing surface as the plain hold route — WriteThrottle is the real ceiling
        (see the ponytail note on ``hold_seats``).
        """
        event = self.get_one(event_id)
        tier = get_object_or_404(models.TicketTier, pk=payload.tier_id, event=event)
        user = self._optional_user()
        guest = self._ensure_guest_session_if_anonymous(user)
        result = pick_service.hold_best_available(
            event,
            tier,
            payload.quantity,
            user=user,
            guest_session=guest,
            accessible_required=payload.accessible_required,
        )
        if not result.held and not result.conflicts:
            raise HttpError(409, str(_("Not enough adjacent seats — pick manually or reduce quantity.")))
        status = 409 if result.conflicts else 200
        return status, schema.HoldResponseSchema(
            held_seat_ids=[h.seat_id for h in result.held],
            conflicts=result.conflicts,
            conflict_reason=result.conflict_reason,
            expires_at=result.expires_at,
        )

    @route.delete(
        "/{uuid:event_id}/seating/holds",
        url_name="event_seating_release",
        response=schema.HoldResponseSchema,
        throttle=WriteThrottle(),
    )
    def release_seats(
        self,
        event_id: UUID,
        payload: t.Annotated[schema.ReleaseSeatsRequest | None, Body(None)] = None,
    ) -> schema.HoldResponseSchema:
        """Release the caller's holds on this event (a subset when seat_ids is given, else all)."""
        event = self.get_one(event_id)
        user = self._optional_user()
        guest = self._resolve_guest_session()
        holds_service.release_seats(event, payload.seat_ids if payload else None, user=user, guest_session=guest)
        return schema.HoldResponseSchema()
