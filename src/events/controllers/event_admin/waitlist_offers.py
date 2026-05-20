"""Admin endpoints for advanced waitlist configuration and offers."""

from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Body
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import EventPermission
from events.service.waitlist_service import (
    create_admin_offer,
    enqueue_waitlist_processing,
    reactivate_admin_offer,
    revoke_all_pending_offers,
)

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
        self, event_id: UUID, status: models.WaitlistOffer.WaitlistOfferStatus | None = None
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
            status=models.WaitlistOffer.WaitlistOfferStatus.PENDING,
        )
        offer.status = models.WaitlistOffer.WaitlistOfferStatus.REVOKED
        offer.save(update_fields=["status"])
        enqueue_waitlist_processing(event.id)
        return offer

    @route.post(
        "/waitlist-offers/{offer_id}/reactivate",
        url_name="reactivate_waitlist_offer",
        response={200: schema.WaitlistOfferSchema},
    )
    def reactivate_waitlist_offer(
        self,
        event_id: UUID,
        offer_id: UUID,
        payload: schema.WaitlistOfferReactivateSchema | None = Body(None),  # type: ignore[type-arg]
    ) -> models.WaitlistOffer:
        """Re-open a previously expired or revoked offer for the same user.

        The offer is reset to ``PENDING`` with a fresh ``expires_at`` (the body's
        value if provided, otherwise ``now + event.waitlist_time_window``).
        ``claimed_at`` and ``notified_at`` are cleared, and a new notification is
        dispatched. Returns 404 if the offer is already PENDING, 400 when the
        event has no ``waitlist_time_window`` configured, and 409 when the user
        already has a different PENDING offer for the event.
        """
        from events.tasks import send_waitlist_offer_notification_task

        event = self.get_one(event_id)
        offer = get_object_or_404(models.WaitlistOffer, pk=offer_id, event=event)
        if offer.status not in {
            models.WaitlistOffer.WaitlistOfferStatus.EXPIRED,
            models.WaitlistOffer.WaitlistOfferStatus.REVOKED,
        }:
            raise HttpError(404, "Offer not found.")
        if event.waitlist_time_window is None:
            raise HttpError(400, "Waitlist time window is not configured for this event.")
        conflict = (
            models.WaitlistOffer.objects.filter(
                event=event,
                user_id=offer.user_id,
                status=models.WaitlistOffer.WaitlistOfferStatus.PENDING,
            )
            .exclude(pk=offer.pk)
            .exists()
        )
        if conflict:
            raise HttpError(409, "User already has a pending offer for this event.")

        expires_at = (
            payload.expires_at if payload and payload.expires_at else (timezone.now() + event.waitlist_time_window)
        )
        try:
            offer = reactivate_admin_offer(event_id=event.id, offer_id=offer.pk, expires_at=expires_at)
        except ValueError as exc:
            if str(exc) == "capacity":
                raise HttpError(
                    409,
                    "Event is at capacity. Revoke an existing pending offer to make room.",
                ) from exc
            raise
        except IntegrityError as exc:
            # Lost the race: another writer landed a PENDING offer for this
            # (event, user) between the conflict check above and the save.
            raise HttpError(409, "User already has a pending offer for this event.") from exc
        offer_id_str = str(offer.id)
        transaction.on_commit(lambda: send_waitlist_offer_notification_task.delay(offer_id_str))
        return offer

    @route.post(
        "/waitlist-offers",
        url_name="create_waitlist_offer",
        response={201: schema.WaitlistOfferSchema},
    )
    def create_waitlist_offer(
        self,
        event_id: UUID,
        payload: schema.WaitlistOfferCreateSchema,
    ) -> tuple[int, models.WaitlistOffer]:
        """Manually create a PENDING offer for a user already on the waitlist.

        The offer is its own single-row batch (``batch_id`` is freshly generated,
        ``is_cutoff_batch`` is False). Returns 404 when the waitlist entry does
        not belong to this event, 400 when ``waitlist_time_window`` is unset, and
        409 when the user already has a PENDING offer for this event. The
        notification task is dispatched after the row is persisted.
        """
        from events.tasks import send_waitlist_offer_notification_task

        event = self.get_one(event_id)
        if event.waitlist_time_window is None:
            raise HttpError(400, "Waitlist time window is not configured for this event.")
        entry = get_object_or_404(models.EventWaitList, pk=payload.waitlist_entry_id, event=event)
        already_pending = models.WaitlistOffer.objects.filter(
            event=event,
            user_id=entry.user_id,
            status=models.WaitlistOffer.WaitlistOfferStatus.PENDING,
        ).exists()
        if already_pending:
            raise HttpError(409, "User already has a pending offer for this event.")

        expires_at = payload.expires_at or (timezone.now() + event.waitlist_time_window)
        try:
            offer = create_admin_offer(event_id=event.id, user_id=entry.user_id, expires_at=expires_at)
        except ValueError as exc:
            if str(exc) == "capacity":
                raise HttpError(
                    409,
                    "Event is at capacity. Revoke an existing pending offer to make room.",
                ) from exc
            raise
        except IntegrityError as exc:
            # Lost the race: another writer (manual create, periodic processor)
            # landed a PENDING offer between the existence check and our create.
            raise HttpError(409, "User already has a pending offer for this event.") from exc
        offer_id_str = str(offer.id)
        transaction.on_commit(lambda: send_waitlist_offer_notification_task.delay(offer_id_str))
        return 201, offer
