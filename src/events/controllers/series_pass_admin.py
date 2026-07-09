"""Admin series-pass controller: create/update/delete passes, manage coverage and holders."""

import typing as t
from uuid import UUID

from django.db.models import Count, Prefetch, Q, QuerySet
from django.shortcuts import get_object_or_404
from ninja import Body
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.schema import ValidationErrorResponse
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.service import series_pass_service, update_db_instance

from .permissions import EventSeriesPermission


@api_controller(
    "/event-series-admin/{series_id}/passes",
    auth=I18nJWTAuth(),
    permissions=[EventSeriesPermission("edit_event_series")],
    tags=["Event Series Admin"],
    throttle=WriteThrottle(),
)
class SeriesPassAdminController(UserAwareController):
    def get_queryset(self) -> QuerySet[models.EventSeries]:
        """Get the queryset of event series visible to the current user."""
        return models.EventSeries.objects.for_user(self.user())

    def get_one(self, series_id: UUID) -> models.EventSeries:
        """Fetch the event series, 404/403ing per ``EventSeriesPermission``."""
        return t.cast(models.EventSeries, self.get_object_or_exception(self.get_queryset(), pk=series_id))

    def get_pass(self, series: models.EventSeries, pass_id: UUID) -> models.SeriesPass:
        """Fetch a series pass scoped to ``series``."""
        return get_object_or_404(models.SeriesPass, pk=pass_id, event_series=series)

    def get_passes_queryset(self, series: models.EventSeries) -> QuerySet[models.SeriesPass]:
        """Admin pass queryset: coverage prefetched and active+pending holders annotated.

        Satisfies ``SeriesPassAdminSchema``'s query expectations, so lists and re-fetched
        single instances serialize without N+1s.
        """
        return (
            models.SeriesPass.objects.filter(event_series=series)
            .prefetch_related(
                Prefetch(
                    "tier_links",
                    queryset=models.SeriesPassTierLink.objects.select_related("event", "tier").order_by("event__start"),
                )
            )
            .annotate(
                holder_count=Count(
                    "held_passes",
                    filter=Q(
                        held_passes__status__in=[
                            models.HeldSeriesPass.HeldSeriesPassStatus.PENDING,
                            models.HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
                        ]
                    ),
                )
            )
        )

    @route.get(
        "/",
        url_name="list_series_passes",
        response=list[schema.SeriesPassAdminSchema],
        throttle=UserDefaultThrottle(),
    )
    def list_series_passes(self, series_id: UUID) -> QuerySet[models.SeriesPass]:
        """List a series' passes with management state, coverage, and holder counts (admin only).

        ``holder_count`` counts ACTIVE and PENDING holders; ``tier_links`` lists the
        covered events (by start date) with the tier backing each. Requires
        'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        return self.get_passes_queryset(series)

    @route.post(
        "/",
        url_name="create_series_pass",
        response={200: schema.SeriesPassAdminSchema, 400: ValidationErrorResponse},
    )
    def create_series_pass(self, series_id: UUID, payload: schema.SeriesPassCreateSchema) -> models.SeriesPass:
        """Create a series pass and its initial coverage (admin only).

        Every event named in ``tier_links`` must pass the enable-time coverage gate
        (open, ticketed, non-invitation-only event on a non-recurring series, not gated
        by an admission questionnaire) — 400 otherwise. Requires 'edit_event_series'
        permission.
        """
        series = self.get_one(series_id)
        created = series_pass_service.create_series_pass(series, payload)
        return self.get_passes_queryset(series).get(pk=created.pk)

    @route.patch(
        "/{pass_id}",
        url_name="update_series_pass",
        response={200: schema.SeriesPassAdminSchema, 400: ValidationErrorResponse},
    )
    def update_series_pass(
        self, series_id: UUID, pass_id: UUID, payload: schema.SeriesPassUpdateSchema
    ) -> models.SeriesPass:
        """Update a series pass (admin only).

        Only unset fields are left untouched (``exclude_unset``). Price/discount changes
        affect future quotes only — already-issued held passes keep their locked-in
        ``price_paid``. Requires 'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        series_pass = self.get_pass(series, pass_id)
        update_db_instance(series_pass, payload)
        return self.get_passes_queryset(series).get(pk=pass_id)

    @route.delete("/{pass_id}", url_name="delete_series_pass", response={204: None})
    def delete_series_pass(self, series_id: UUID, pass_id: UUID) -> tuple[int, None]:
        """Permanently delete a series pass (admin only).

        409s if any non-cancelled held pass exists — cancel every holder first.
        Requires 'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        series_pass = self.get_pass(series, pass_id)
        series_pass_service.delete_series_pass(series_pass)
        return 204, None

    @route.post(
        "/{pass_id}/tier-links",
        url_name="add_series_pass_tier_links",
        response=list[schema.SeriesPassTierLinkInputSchema],
    )
    def add_series_pass_tier_links(
        self, series_id: UUID, pass_id: UUID, payload: list[schema.SeriesPassTierLinkInputSchema]
    ) -> list[models.SeriesPassTierLink]:
        """Extend a series pass's coverage with new (event, tier) pairs (admin only).

        Existing ACTIVE holders are granted free tickets for the newly-covered events via
        Celery materialization, dispatched after commit. Response: the created links, as
        (event_id, tier_id) pairs, in input order. Requires 'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        series_pass = self.get_pass(series, pass_id)
        links: list[series_pass_service.TierLinkInput] = [
            {"event_id": link.event_id, "tier_id": link.tier_id} for link in payload
        ]
        return series_pass_service.add_tier_links(series_pass, links, materialize=True)

    @route.delete(
        "/{pass_id}/tier-links/{event_id}",
        url_name="remove_series_pass_tier_link",
        response={204: None},
    )
    def remove_series_pass_tier_link(self, series_id: UUID, pass_id: UUID, event_id: UUID) -> tuple[int, None]:
        """Remove a covered event from a series pass (admin only).

        409s if the pass has any non-cancelled holder — removing coverage from a sold
        pass would orphan that holder's materialized ticket, so it's unsupported in v1.
        Requires 'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        series_pass = self.get_pass(series, pass_id)
        tier_link = get_object_or_404(models.SeriesPassTierLink, series_pass=series_pass, event_id=event_id)
        series_pass_service.remove_tier_link(series_pass, tier_link)
        return 204, None

    @route.get(
        "/{pass_id}/holders",
        url_name="list_series_pass_holders",
        response=PaginatedResponseSchema[schema.HeldSeriesPassAdminSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__email", "user__first_name", "user__last_name"])
    def list_series_pass_holders(self, series_id: UUID, pass_id: UUID) -> QuerySet[models.HeldSeriesPass]:
        """List holders of a series pass, searchable by holder name/email (admin only).

        Requires 'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        series_pass = self.get_pass(series, pass_id)
        return models.HeldSeriesPass.objects.filter(series_pass=series_pass).select_related("user")

    @route.post(
        "/held/{held_pass_id}/confirm-payment",
        url_name="confirm_series_pass_payment",
        response={200: schema.HeldSeriesPassAdminSchema, 400: ValidationErrorResponse},
    )
    def confirm_series_pass_payment(self, series_id: UUID, held_pass_id: UUID) -> models.HeldSeriesPass:
        """Confirm offline payment for a pending held series pass (admin only).

        Flips the held pass PENDING -> ACTIVE and its materialized PENDING tickets ->
        ACTIVE, then notifies the holder and org staff/owners (dispatched after commit).
        400 if the pass isn't paid OFFLINE or the held pass isn't PENDING. Requires
        'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        held_pass = get_object_or_404(
            models.HeldSeriesPass.objects.select_related("series_pass", "user"),
            pk=held_pass_id,
            series_pass__event_series=series,
        )
        return series_pass_service.confirm_held_pass_payment(held_pass)

    @route.post(
        "/held/{held_pass_id}/cancel",
        url_name="cancel_series_pass",
        response=schema.HeldSeriesPassAdminSchema,
    )
    def cancel_series_pass(
        self,
        series_id: UUID,
        held_pass_id: UUID,
        payload: t.Annotated[schema.HeldSeriesPassCancelSchema | None, Body(None)] = None,
    ) -> models.HeldSeriesPass:
        """Cancel a holder's series pass (admin only).

        Cancels every future, non-checked-in ticket materialized from the pass —
        refunding any successfully-paid online ticket — then marks the pass CANCELLED.
        Requires 'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        held_pass = get_object_or_404(
            models.HeldSeriesPass.objects.select_related("series_pass", "user"),
            pk=held_pass_id,
            series_pass__event_series=series,
        )
        reason = payload.reason if payload else None
        return series_pass_service.cancel_held_pass(held_pass, cancelled_by=self.user(), reason=reason)
