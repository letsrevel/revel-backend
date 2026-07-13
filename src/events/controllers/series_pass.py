"""Public series-pass controller: browse, quote, purchase, and holder self-service."""

import typing as t
from uuid import UUID

from django.db.models import Prefetch, QuerySet
from django.http import HttpResponse, HttpResponseRedirect
from django.utils.translation import gettext_lazy as _
from ninja import Body
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.exceptions import NotFound
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth, OptionalAuth
from common.controllers import UserAwareController
from common.schema import ResponseMessage
from common.signing import get_file_url
from common.throttling import WriteThrottle
from events import models, schema
from events.service import series_pass_file_service, series_pass_service
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.utils import apple_wallet_configured


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
    reservation_id: UUID | None
    requires_payment: bool


@api_controller("/series-passes", auth=OptionalAuth(), tags=["Series Passes"])
class SeriesPassController(UserAwareController):
    """Public endpoints for browsing, quoting, purchasing, and downloading series passes."""

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
        if not series_pass_service.pass_visible_to_user(series_pass, self.maybe_user()):
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
        passes = series.series_passes.filter(is_active=True)
        return series_pass_service.visible_passes(passes, series.organization, self.maybe_user())

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
        organizer confirms payment, ACTIVE for free).

        **Online passes:** returns `requires_payment=true` and a `reservation_id`. Call
        `POST /series-passes/reservations/{reservation_id}/checkout-session` next to obtain the
        Stripe `checkout_url`. The held pass is PENDING until payment succeeds (see the Stripe
        webhook).

        **Error Cases:**
        - 403: Blacklisted, or ``purchasable_by`` is members-only and the user isn't a member
          (the pass is visible, but not purchasable, to non-members).
        - 404: The pass itself isn't visible to the user (e.g. a MEMBERS_ONLY-*visibility*
          pass and the user isn't a member) — raised earlier, by ``_get_visible_pass``.
        - 409: Not currently purchasable (sold out, outside sales window, already held).
        - 429: A covered future event's tier just sold out.
        """
        series_pass = self._get_visible_pass(pass_id)
        result = SeriesPassPurchaseService(series_pass, self.user()).purchase(billing_info=billing_info)
        if isinstance(result, tuple):
            held_pass, reservation_id = result
            held_pass = self._held_pass_queryset().get(pk=held_pass.pk)
            return {
                "checkout_url": None,
                "held_pass": held_pass,
                "reservation_id": reservation_id,
                "requires_payment": True,
            }
        held_pass = self._held_pass_queryset().get(pk=result.pk)
        return {"checkout_url": None, "held_pass": held_pass, "reservation_id": None, "requires_payment": False}

    @route.post(
        "/reservations/{uuid:reservation_id}/checkout-session",
        url_name="series_pass_checkout_session",
        response={200: schema.CheckoutSessionResponse, 404: ResponseMessage},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def series_pass_checkout_session(self, reservation_id: UUID) -> schema.CheckoutSessionResponse:
        """Create the Stripe checkout session for a reserved series-pass purchase and return its URL.

        Second step of online checkout (#632): `checkout_series_pass` reserves the pass and
        returns a `reservation_id`; this endpoint creates the Stripe session holding no
        contended DB lock. Idempotent — repeat calls (retry or double-submit) reuse the same
        Stripe session. Returns 404 if the reservation is unknown, expired, or not owned by
        the caller.
        """
        from events.service import stripe_service

        # Ownership: create_series_pass_session scopes by reservation_id; enforce the
        # caller owns it to stop cross-user session creation.
        if not models.Payment.objects.filter(reservation_id=reservation_id, user=self.user()).exists():
            raise HttpError(404, str(_("No pending reservation found.")))
        checkout_url = stripe_service.create_series_pass_session(reservation_id=reservation_id)
        return schema.CheckoutSessionResponse(checkout_url=checkout_url)

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

        if not apple_wallet_configured():
            raise HttpError(503, "Apple Wallet is not configured")

        pkpass_bytes = series_pass_file_service.get_or_generate_pass_pkpass(held_pass)

        response = HttpResponse(pkpass_bytes, content_type="application/vnd.apple.pkpass")
        safe_name = "series_pass_" + str(held_pass.id).split("-")[0]
        response["Content-Disposition"] = f'attachment; filename="{safe_name}.pkpass"'
        return response
