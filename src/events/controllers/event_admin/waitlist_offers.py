"""Admin endpoints for advanced waitlist configuration and offers."""

from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import EventPermission
from events.service.waitlist_service import enqueue_waitlist_processing, revoke_all_pending_offers

from .base import EventAdminBaseController


@api_controller(
    "/event-admin/{event_id}",
    auth=I18nJWTAuth(),
    permissions=[EventPermission("manage_event")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminWaitlistOffersController(EventAdminBaseController):
    """Advanced waitlist configuration and offer management."""

    @route.get(
        "/waitlist-settings",
        url_name="get_waitlist_settings",
        response=schema.WaitlistSettingsSchema,
        throttle=UserDefaultThrottle(),
    )
    def get_waitlist_settings(self, event_id: UUID) -> models.Event:
        """Return the current waitlist configuration for the event."""
        return self.get_one(event_id)

    @route.patch(
        "/waitlist-settings",
        url_name="update_waitlist_settings",
        response=schema.WaitlistSettingsSchema,
    )
    def update_waitlist_settings(self, event_id: UUID, payload: schema.WaitlistSettingsUpdateSchema) -> models.Event:
        """Partially update the waitlist configuration.

        Closing the waitlist (``waitlist_open`` True -> False) revokes any
        pending offers so seats are immediately returned to the public pool.
        """
        event = self.get_one(event_id)
        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            return event
        was_open = event.waitlist_open
        for field, value in update_data.items():
            setattr(event, field, value)
        event.full_clean()
        event.save(update_fields=list(update_data.keys()))
        if was_open and event.waitlist_open is False:
            revoke_all_pending_offers(event.id)
        return event

    @route.get(
        "/waitlist-offers",
        url_name="list_waitlist_offers",
        response=PaginatedResponseSchema[schema.WaitlistOfferSchema],
        permissions=[EventPermission("invite_to_event")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_waitlist_offers(
        self, event_id: UUID, status: models.WaitlistOffer.Status | None = None
    ) -> QuerySet[models.WaitlistOffer]:
        """List waitlist offers for this event, optionally filtered by status."""
        event = self.get_one(event_id)
        qs = models.WaitlistOffer.objects.select_related("user").filter(event=event)
        if status is not None:
            qs = qs.filter(status=status)
        return qs

    @route.post(
        "/waitlist-offers/{offer_id}/revoke",
        url_name="revoke_waitlist_offer",
        response={200: schema.WaitlistOfferSchema},
    )
    def revoke_waitlist_offer(self, event_id: UUID, offer_id: UUID) -> models.WaitlistOffer:
        """Revoke a pending offer and enqueue a fresh processing pass."""
        event = self.get_one(event_id)
        offer = get_object_or_404(
            models.WaitlistOffer,
            pk=offer_id,
            event=event,
            status=models.WaitlistOffer.Status.PENDING,
        )
        offer.status = models.WaitlistOffer.Status.REVOKED
        offer.save(update_fields=["status"])
        enqueue_waitlist_processing(event.id)
        return offer
