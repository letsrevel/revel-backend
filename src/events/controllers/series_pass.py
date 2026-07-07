"""Public series-pass controller: browse, quote, purchase, and holder self-service."""

import typing as t
from uuid import UUID

from django.conf import settings
from django.db.models import Prefetch, QuerySet
from django.http import HttpResponse, HttpResponseRedirect
from ninja import Body
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.exceptions import NotFound
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth, OptionalAuth
from common.controllers import UserAwareController
from common.signing import get_file_url
from common.throttling import WriteThrottle
from events import models, schema
from events.service import series_pass_file_service, series_pass_service
from events.service.series_pass_purchase import SeriesPassPurchaseService


class _CheckoutResult(t.TypedDict):
    """Raw payload matching ``response=schema.SeriesPassCheckoutResponseSchema``.

    Returned as a plain dict rather than a pre-built ``SeriesPassCheckoutResponseSchema``:
    ninja always re-validates whatever a route returns against its declared ``response``
    schema, wrapping it in a fresh resolver-aware getter. Handing it an already-built
    ``HeldSeriesPassSchema`` would make that revalidation pass run ``HeldSeriesPassSchema``'s
    ``series``/``*_event_count`` resolvers against the *schema* instance instead of the
    ``HeldSeriesPass`` model they expect. A raw dict keeps ``held_pass`` as the ORM instance
    all the way to that single validation pass, so the resolvers see what they expect.
    """

    checkout_url: str | None
    held_pass: models.HeldSeriesPass | None


def _apple_pass_available() -> bool:
    """Whether Apple Wallet is configured server-wide (mirrors ``Ticket.apple_pass_available``)."""
    return bool(
        settings.APPLE_WALLET_PASS_TYPE_ID
        and settings.APPLE_WALLET_TEAM_ID
        and settings.APPLE_WALLET_CERT_PATH
        and settings.APPLE_WALLET_KEY_PATH
        and settings.APPLE_WALLET_WWDR_CERT_PATH
    )


@api_controller("/series-passes", auth=OptionalAuth(), tags=["Series Passes"])
class SeriesPassController(UserAwareController):
    """Public endpoints for browsing, quoting, purchasing, and downloading series passes."""

    def _pass_is_visible(self, series_pass: models.SeriesPass) -> bool:
        """Pass-level visibility, given the series it belongs to is already visible.

        v1 simplification: mirrors ``TicketTier``'s tier-visibility convention (PUBLIC/UNLISTED
        visible to everyone, MEMBERS_ONLY requires an active membership, STAFF_ONLY/PRIVATE are
        staff/owner-only) without the invitation-based branch — ``SeriesPass.clean()`` already
        rejects INVITED/INVITED_AND_MEMBERS, so no invitation-linked visibility exists to replicate.
        """
        user = self.maybe_user()
        org = series_pass.event_series.organization
        if not user.is_anonymous and (user.is_superuser or user.is_staff or org.is_owner_or_staff(user)):
            return True
        if series_pass.visibility in models.SeriesPass.Visibility.publicly_accessible():
            return True
        if series_pass.visibility == models.SeriesPass.Visibility.MEMBERS_ONLY and not user.is_anonymous:
            return models.OrganizationMember.objects.for_visibility().filter(user=user, organization=org).exists()
        return False

    def _get_visible_pass(self, pass_id: UUID) -> models.SeriesPass:
        """Fetch a series pass, 404ing unless both its series and the pass itself are visible."""
        series_pass = t.cast(
            models.SeriesPass,
            self.get_object_or_exception(
                models.SeriesPass.objects.select_related("event_series", "event_series__organization"),
                pk=pass_id,
                is_active=True,
            ),
        )
        series_is_visible = (
            models.EventSeries.objects.for_user(self.maybe_user()).filter(pk=series_pass.event_series_id).exists()
        )
        if not series_is_visible:
            raise NotFound()
        if not self._pass_is_visible(series_pass):
            raise NotFound()
        return series_pass

    def _held_pass_queryset(self) -> QuerySet[models.HeldSeriesPass]:
        """Held passes with everything ``HeldSeriesPassSchema`` needs prefetched (no N+1)."""
        return models.HeldSeriesPass.objects.select_related("series_pass__event_series").prefetch_related(
            Prefetch(
                "series_pass__tier_links",
                queryset=models.SeriesPassTierLink.objects.select_related("event"),
            )
        )

    @route.get(
        "/event-series/{series_id}",
        url_name="list_series_passes",
        response=list[schema.SeriesPassSchema],
    )
    def list_series_passes(self, series_id: UUID) -> list[models.SeriesPass]:
        """List active, visible series passes for an event series.

        The series itself must be visible to the current user (organization visibility rules
        apply). Passes are further filtered by their own visibility: PUBLIC/UNLISTED passes are
        always included, MEMBERS_ONLY passes require an active membership, and STAFF_ONLY/PRIVATE
        passes are staff/owner-only.
        """
        series = t.cast(
            models.EventSeries,
            self.get_object_or_exception(models.EventSeries.objects.for_user(self.maybe_user()), pk=series_id),
        )
        passes = series.series_passes.filter(is_active=True).select_related("event_series__organization")
        return [series_pass for series_pass in passes if self._pass_is_visible(series_pass)]

    @route.get("/{pass_id}/quote", url_name="get_series_pass_quote", response=schema.SeriesPassQuoteSchema)
    def get_series_pass_quote(self, pass_id: UUID) -> schema.SeriesPassQuoteSchema:
        """Get the current pro-rata price and purchasability for a series pass."""
        series_pass = self._get_visible_pass(pass_id)
        quote = series_pass_service.get_quote(series_pass)
        return schema.SeriesPassQuoteSchema(
            price=quote.price,
            passed_events=quote.passed_events,
            remaining_events=quote.remaining_events,
            currency=quote.currency,
            purchasable=quote.purchasable,
            reason=quote.reason,
        )

    @route.post(
        "/{pass_id}/checkout",
        url_name="checkout_series_pass",
        response=schema.SeriesPassCheckoutResponseSchema,
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def checkout_series_pass(
        self,
        pass_id: UUID,
        billing_info: t.Annotated[schema.BuyerBillingInfoSchema | None, Body(None)] = None,
    ) -> _CheckoutResult:
        """Purchase a series pass.

        Free and offline passes create a `HeldSeriesPass` directly (PENDING for offline until the
        organizer confirms payment, ACTIVE for free). Online passes return a Stripe checkout URL;
        the held pass is created once payment succeeds (see the Stripe webhook).

        **Error Cases:**
        - 403: Blacklisted, or the pass is members-only and the user isn't a member.
        - 409: Not currently purchasable (sold out, outside sales window, already held).
        - 429: A covered future event's tier just sold out.
        """
        series_pass = self._get_visible_pass(pass_id)
        result = SeriesPassPurchaseService(series_pass, self.user()).purchase(billing_info=billing_info)
        if isinstance(result, str):
            return {"checkout_url": result, "held_pass": None}
        held_pass = self._held_pass_queryset().get(pk=result.pk)
        return {"checkout_url": None, "held_pass": held_pass}

    @route.get(
        "/me",
        url_name="list_my_series_passes",
        response=PaginatedResponseSchema[schema.HeldSeriesPassSchema],
        auth=I18nJWTAuth(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_my_series_passes(self) -> QuerySet[models.HeldSeriesPass]:
        """List the current user's held series passes."""
        return self._held_pass_queryset().filter(user=self.user())

    @route.get(
        "/me/{held_pass_id}/pdf",
        url_name="series_pass_pdf_download",
        summary="Download PDF series pass",
        description="Generate and download a PDF version of a held series pass. "
        "Redirects to a signed URL served by Caddy when the file is cached.",
        response={200: None, 302: None, 404: None},
        auth=I18nJWTAuth(),
    )
    def download_series_pass_pdf(self, held_pass_id: UUID) -> HttpResponse:
        """Download a PDF version of a held series pass. The user must own the pass."""
        held_pass = t.cast(
            models.HeldSeriesPass,
            self.get_object_or_exception(models.HeldSeriesPass.objects.filter(user=self.user()), pk=held_pass_id),
        )

        if series_pass_file_service.is_cache_valid(held_pass) and (signed_url := get_file_url(held_pass.pdf_file)):
            return HttpResponseRedirect(signed_url)

        pdf_bytes = series_pass_file_service.get_or_generate_pass_pdf(held_pass)

        held_pass.refresh_from_db()
        if signed_url := get_file_url(held_pass.pdf_file):
            return HttpResponseRedirect(signed_url)

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        safe_name = "series_pass_" + str(held_pass.id).split("-")[0]
        response["Content-Disposition"] = f'attachment; filename="{safe_name}.pdf"'
        return response

    @route.get(
        "/me/{held_pass_id}/pkpass",
        url_name="series_pass_apple_wallet_pass",
        summary="Download Apple Wallet pass",
        description="Generate and download an Apple Wallet pass (.pkpass) for a held series pass.",
        response={200: None, 404: None, 503: None},
        auth=I18nJWTAuth(),
    )
    def download_series_pass_pkpass(self, held_pass_id: UUID) -> HttpResponse:
        """Download an Apple Wallet pass for a held series pass. The user must own the pass."""
        held_pass = t.cast(
            models.HeldSeriesPass,
            self.get_object_or_exception(models.HeldSeriesPass.objects.filter(user=self.user()), pk=held_pass_id),
        )

        if not _apple_pass_available():
            raise HttpError(503, "Apple Wallet is not configured")

        pkpass_bytes = series_pass_file_service.get_or_generate_pass_pkpass(held_pass)

        response = HttpResponse(pkpass_bytes, content_type="application/vnd.apple.pkpass")
        safe_name = "series_pass_" + str(held_pass.id).split("-")[0]
        response["Content-Disposition"] = f'attachment; filename="{safe_name}.pkpass"'
        return response
