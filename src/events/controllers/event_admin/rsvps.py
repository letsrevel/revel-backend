import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import Query
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from accounts.models import RevelUser
from common.authentication import ScopedJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import EventPermission
from events.service import update_db_instance
from events.service.holder_visibility import annotate_holder_known, require_known_holder
from events.service.waitlist_service import enqueue_waitlist_processing
from oauth.permissions import RequireScope

from .base import EventAdminBaseController


@api_controller(
    "/event-admin/{event_id}",
    auth=ScopedJWTAuth(),
    permissions=[RequireScope("org:read"), EventPermission("invite_to_event")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminRSVPsController(EventAdminBaseController):
    """Event RSVP management endpoints."""

    @staticmethod
    def _rsvps(event: models.Event) -> QuerySet[models.EventRSVP]:
        """The event's RSVPs with the user and the ``holder_known`` annotation the response masks on."""
        return annotate_holder_known(
            models.EventRSVP.objects.select_related("user").filter(event=event), event.organization_id
        )

    @route.get(
        "/rsvps",
        url_name="list_rsvps",
        response=PaginatedResponseSchema[schema.RSVPDetailSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__email"])
    def list_rsvps(
        self,
        event_id: UUID,
        params: t.Annotated[filters.RSVPFilterSchema, Query(...)],
    ) -> QuerySet[models.EventRSVP]:
        """List all RSVPs for an event.

        Shows all users who have RSVPed to the event with their status.
        Use this to see who is attending, not attending, or maybe attending.
        Supports filtering by status and user_id.
        """
        event = self.get_one(event_id)
        qs = annotate_holder_known(
            models.EventRSVP.objects.with_user().with_org_membership(event.organization_id).filter(event=event),
            event.organization_id,
        )
        # "-id" is a stable tiebreaker so pagination stays deterministic across equal sort keys.
        # No .distinct(): all joins are to-one (#880).
        return params.filter(qs).order_by("-created_at", "-id")

    @route.get(
        "/rsvps/{rsvp_id}",
        url_name="get_rsvp",
        response=schema.RSVPDetailSchema,
        throttle=UserDefaultThrottle(),
    )
    def get_rsvp(self, event_id: UUID, rsvp_id: UUID) -> models.EventRSVP:
        """Get details of a specific RSVP."""
        event = self.get_one(event_id)
        return get_object_or_404(self._rsvps(event), pk=rsvp_id)

    @route.post(
        "/rsvps",
        url_name="create_rsvp",
        response={200: schema.RSVPDetailSchema, 400: schema.InvitationRequiredErrorSchema},
    )
    def create_rsvp(self, event_id: UUID, payload: schema.RSVPCreateSchema) -> models.EventRSVP:
        """Create an RSVP on behalf of a user.

        Use this when a user contacts the organization to RSVP outside the platform
        (e.g., via text, email, or in person). The account must already be known to the
        organization through its own acts (member, staff, applicant, own ticket, RSVP,
        pass, waitlist entry...; box-office tickets and staff RSVPs do not count);
        otherwise 400 ``invitation_required``.
        """
        event = self.get_one(event_id)

        user = get_object_or_404(RevelUser, pk=payload.user_id, is_active=True)
        require_known_holder(event.organization, user)

        # Capture prior status (if any) before the upsert so we can detect a
        # YES -> non-YES transition that frees a seat.
        prior_status = models.EventRSVP.objects.filter(event=event, user=user).values_list("status", flat=True).first()

        # Create or update RSVP (due to unique constraint on event+user)
        # A new row is staff-made; an existing row keeps its origin.
        rsvp, created = models.EventRSVP.objects.update_or_create(
            event=event,
            user=user,
            defaults={"status": payload.status, "note": payload.note},
            create_defaults={"status": payload.status, "note": payload.note, "source": models.EventRSVP.Source.STAFF},
        )

        if prior_status == models.EventRSVP.RsvpStatus.YES and payload.status != models.EventRSVP.RsvpStatus.YES:
            enqueue_waitlist_processing(event.id)

        return get_object_or_404(self._rsvps(event), pk=rsvp.pk)

    @route.put(
        "/rsvps/{rsvp_id}",
        url_name="update_rsvp",
        response=schema.RSVPDetailSchema,
    )
    def update_rsvp(self, event_id: UUID, rsvp_id: UUID, payload: schema.RSVPUpdateSchema) -> models.EventRSVP:
        """Update an existing RSVP.

        Use this to change a user's RSVP status when they contact you to update their response.
        """
        event = self.get_one(event_id)
        rsvp = get_object_or_404(models.EventRSVP, pk=rsvp_id, event=event)
        prior_status = rsvp.status
        updated = update_db_instance(rsvp, payload)
        if prior_status == models.EventRSVP.RsvpStatus.YES and updated.status != models.EventRSVP.RsvpStatus.YES:
            enqueue_waitlist_processing(event.id)
        return get_object_or_404(self._rsvps(event), pk=updated.pk)

    @route.delete(
        "/rsvps/{rsvp_id}",
        url_name="delete_rsvp",
        response={204: None},
    )
    def delete_rsvp(self, event_id: UUID, rsvp_id: UUID) -> tuple[int, None]:
        """Delete an RSVP.

        Use this to remove a user's RSVP entirely from the event.
        Note: This is different from setting status to "no" - it completely removes the RSVP record.
        """
        event = self.get_one(event_id)
        rsvp = get_object_or_404(models.EventRSVP, pk=rsvp_id, event=event)
        # Capture status before delete: deleting a YES RSVP frees a seat and
        # must wake the waitlist (consistent with create_rsvp / update_rsvp).
        was_yes = rsvp.status == models.EventRSVP.RsvpStatus.YES
        rsvp.delete()
        if was_yes:
            enqueue_waitlist_processing(event.id)
        return 204, None
