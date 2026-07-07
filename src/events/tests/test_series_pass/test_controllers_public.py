"""Public series-pass controller tests: visibility, quote, checkout, /me, and file downloads."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    OrganizationMember,
    SeriesPass,
    SeriesPassTierLink,
    TicketTier,
)

pytestmark = pytest.mark.django_db


# ---- Fixture overrides: this suite exercises anonymous/stranger visibility, so the
# shared `organization`/`series_pass` fixtures (which default to PRIVATE visibility,
# per VisibilityMixin) are made PUBLIC here unless a test flips them back. ----


@pytest.fixture
def organization(organization: Organization) -> Organization:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])
    return organization


@pytest.fixture
def series_pass(series_pass: SeriesPass) -> SeriesPass:
    series_pass.visibility = SeriesPass.Visibility.PUBLIC
    series_pass.save(update_fields=["visibility"])
    return series_pass


# ---- Client fixtures ----


@pytest.fixture
def revel_user_client(revel_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(revel_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def other_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="other_holder@example.com", email="other_holder@example.com")


@pytest.fixture
def other_user_client(other_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(other_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def organization_owner_client(organization_owner_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


# ---- Model fixture helpers (mirrors test_purchase.py's local builders) ----


def _make_event(organization: Organization, event_series: EventSeries, name: str, slug: str, start: t.Any) -> Event:
    return Event.objects.create(
        organization=organization,
        name=name,
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=start,
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )


def _make_tier(event: Event, name: str) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


@pytest.fixture
def future_events(organization: Organization, event_series: EventSeries) -> list[Event]:
    now = timezone.now()
    return [
        _make_event(organization, event_series, f"Future {i}", f"future-{i}", now + timedelta(days=i))
        for i in range(1, 4)
    ]


@pytest.fixture
def future_tiers(future_events: list[Event]) -> list[TicketTier]:
    return [_make_tier(event, f"Tier for {event.name}") for event in future_events]


@pytest.fixture
def past_event(organization: Organization, event_series: EventSeries) -> Event:
    now = timezone.now()
    return _make_event(organization, event_series, "Past", "past", now - timedelta(days=1))


@pytest.fixture
def past_tier(past_event: Event) -> TicketTier:
    return _make_tier(past_event, "Past Tier")


@pytest.fixture
def purchasable_free_pass(
    event_series: EventSeries,
    past_event: Event,
    past_tier: TicketTier,
    future_events: list[Event],
    future_tiers: list[TicketTier],
) -> SeriesPass:
    """Public, free pass covering 1 past + 3 future events (passed=1, remaining=3, purchasable)."""
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Free Season Pass",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=SeriesPass.Visibility.PUBLIC,
    )
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=past_event, tier=past_tier)
    for event, tier in zip(future_events, future_tiers):
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return series_pass


@pytest.fixture
def purchasable_online_pass(
    event_series: EventSeries,
    past_event: Event,
    past_tier: TicketTier,
    future_events: list[Event],
    future_tiers: list[TicketTier],
) -> SeriesPass:
    """Public, online-payment pass covering 1 past + 3 future events."""
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Online Season Pass",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        visibility=SeriesPass.Visibility.PUBLIC,
    )
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=past_event, tier=past_tier)
    for event, tier in zip(future_events, future_tiers):
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return series_pass


@pytest.fixture
def under_min_pass(organization: Organization, event_series: EventSeries) -> SeriesPass:
    """Public pass with 2 past + 1 future event -> remaining=1, not purchasable (409)."""
    now = timezone.now()
    p1 = _make_event(organization, event_series, "P1", "p1", now - timedelta(days=2))
    p2 = _make_event(organization, event_series, "P2", "p2", now - timedelta(days=1))
    f1 = _make_event(organization, event_series, "F1", "f1", now + timedelta(days=1))
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Under Min Pass",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=SeriesPass.Visibility.PUBLIC,
    )
    for event in (p1, p2, f1):
        tier = _make_tier(event, f"Tier for {event.name}")
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return series_pass


@pytest.fixture
def held_pass(series_pass: SeriesPass, revel_user: RevelUser) -> HeldSeriesPass:
    return HeldSeriesPass.objects.create(
        series_pass=series_pass,
        user=revel_user,
        status=HeldSeriesPass.Status.ACTIVE,
        price_paid=series_pass.price,
    )


# ---- Auth ----


class TestAuth:
    def test_checkout_requires_auth(self, series_pass: SeriesPass) -> None:
        client = Client()
        url = reverse("api:checkout_series_pass", kwargs={"pass_id": series_pass.id})
        response = client.post(url, data=b"", content_type="application/json")
        assert response.status_code == 401

    def test_me_requires_auth(self) -> None:
        client = Client()
        response = client.get(reverse("api:list_my_series_passes"))
        assert response.status_code == 401


# ---- Visibility ----


class TestVisibility:
    def test_private_org_series_hidden_from_stranger(
        self, organization: Organization, event_series: EventSeries, series_pass: SeriesPass
    ) -> None:
        organization.visibility = Organization.Visibility.PRIVATE
        organization.save(update_fields=["visibility"])
        client = Client()

        list_url = reverse("api:list_series_passes", kwargs={"series_id": event_series.id})
        assert client.get(list_url).status_code == 404

        quote_url = reverse("api:get_series_pass_quote", kwargs={"pass_id": series_pass.id})
        assert client.get(quote_url).status_code == 404

    def test_public_org_series_pass_listed_and_quotable(
        self, event_series: EventSeries, series_pass: SeriesPass
    ) -> None:
        client = Client()

        list_url = reverse("api:list_series_passes", kwargs={"series_id": event_series.id})
        response = client.get(list_url)
        assert response.status_code == 200
        assert {item["id"] for item in response.json()} == {str(series_pass.id)}

        quote_url = reverse("api:get_series_pass_quote", kwargs={"pass_id": series_pass.id})
        assert client.get(quote_url).status_code == 200

    def test_staff_only_pass_hidden_from_stranger_visible_to_owner(
        self, organization_owner_client: Client, series_pass: SeriesPass, event_series: EventSeries
    ) -> None:
        series_pass.visibility = SeriesPass.Visibility.STAFF_ONLY
        series_pass.save(update_fields=["visibility"])
        client = Client()

        list_url = reverse("api:list_series_passes", kwargs={"series_id": event_series.id})
        assert client.get(list_url).json() == []
        assert organization_owner_client.get(list_url).json() != []

        quote_url = reverse("api:get_series_pass_quote", kwargs={"pass_id": series_pass.id})
        assert client.get(quote_url).status_code == 404
        assert organization_owner_client.get(quote_url).status_code == 200

    def test_members_only_pass_visible_to_member_hidden_from_non_member(
        self,
        organization: Organization,
        event_series: EventSeries,
        series_pass: SeriesPass,
        revel_user: RevelUser,
        revel_user_client: Client,
        other_user_client: Client,
    ) -> None:
        series_pass.visibility = SeriesPass.Visibility.MEMBERS_ONLY
        series_pass.save(update_fields=["visibility"])
        OrganizationMember.objects.create(organization=organization, user=revel_user)

        list_url = reverse("api:list_series_passes", kwargs={"series_id": event_series.id})

        member_response = revel_user_client.get(list_url)
        assert member_response.status_code == 200
        assert {item["id"] for item in member_response.json()} == {str(series_pass.id)}

        non_member_response = other_user_client.get(list_url)
        assert non_member_response.status_code == 200
        assert non_member_response.json() == []


# ---- Quote math ----


def test_quote_reflects_passed_and_remaining_events(purchasable_free_pass: SeriesPass) -> None:
    client = Client()
    url = reverse("api:get_series_pass_quote", kwargs={"pass_id": purchasable_free_pass.id})
    response = client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["passed_events"] == 1
    assert data["remaining_events"] == 3
    assert data["price"] == "25.00"  # 30.00 - 1 * 5.00
    assert data["currency"] == "EUR"
    assert data["purchasable"] is True
    assert data["reason"] is None


# ---- Checkout ----


class TestCheckout:
    def test_free_pass_checkout_returns_active_held_pass(
        self, revel_user_client: Client, purchasable_free_pass: SeriesPass, revel_user: RevelUser
    ) -> None:
        url = reverse("api:checkout_series_pass", kwargs={"pass_id": purchasable_free_pass.id})
        response = revel_user_client.post(url, data=b"", content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["checkout_url"] is None
        assert data["held_pass"]["status"] == "active"
        held_pass = HeldSeriesPass.objects.get(series_pass=purchasable_free_pass, user=revel_user)
        assert data["held_pass"]["id"] == str(held_pass.id)
        assert data["held_pass"]["total_event_count"] == 4
        assert data["held_pass"]["remaining_event_count"] == 3

    def test_online_pass_checkout_returns_checkout_url(
        self, revel_user_client: Client, purchasable_online_pass: SeriesPass
    ) -> None:
        url = reverse("api:checkout_series_pass", kwargs={"pass_id": purchasable_online_pass.id})
        with patch(
            "events.service.stripe_service.create_series_pass_checkout_session",
            return_value="https://checkout.stripe.com/session/xyz",
        ):
            response = revel_user_client.post(url, data=b"", content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["checkout_url"] == "https://checkout.stripe.com/session/xyz"
        assert data["held_pass"] is None

    def test_not_purchasable_returns_409(self, revel_user_client: Client, under_min_pass: SeriesPass) -> None:
        url = reverse("api:checkout_series_pass", kwargs={"pass_id": under_min_pass.id})
        response = revel_user_client.post(url, data=b"", content_type="application/json")
        assert response.status_code == 409

    def test_sold_out_future_tier_returns_429(
        self, revel_user_client: Client, purchasable_free_pass: SeriesPass, future_tiers: list[TicketTier]
    ) -> None:
        sold_out_tier = future_tiers[1]
        sold_out_tier.total_quantity = 1
        sold_out_tier.quantity_sold = 1
        sold_out_tier.save(update_fields=["total_quantity", "quantity_sold"])

        url = reverse("api:checkout_series_pass", kwargs={"pass_id": purchasable_free_pass.id})
        response = revel_user_client.post(url, data=b"", content_type="application/json")
        assert response.status_code == 429


# ---- /me ----


class TestMyPasses:
    def test_lists_own_passes_only(
        self,
        revel_user_client: Client,
        revel_user: RevelUser,
        other_user: RevelUser,
        series_pass: SeriesPass,
    ) -> None:
        own = HeldSeriesPass.objects.create(
            series_pass=series_pass, user=revel_user, status=HeldSeriesPass.Status.ACTIVE, price_paid=series_pass.price
        )
        HeldSeriesPass.objects.create(
            series_pass=series_pass, user=other_user, status=HeldSeriesPass.Status.ACTIVE, price_paid=series_pass.price
        )

        response = revel_user_client.get(reverse("api:list_my_series_passes"))

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["id"] == str(own.id)

    def test_query_count_does_not_grow_with_pass_count(
        self, revel_user_client: Client, revel_user: RevelUser, event_series: EventSeries
    ) -> None:
        """Serializing 2 held passes must cost about the same per-row as serializing 4.

        Compares two list sizes rather than pinning an absolute count, mirroring
        ``test_annotation.py``'s rationale (Silk profiling adds nondeterministic
        bookkeeping queries in this dev/test environment).
        """

        def _make_held_pass(suffix: str) -> HeldSeriesPass:
            pass_ = SeriesPass.objects.create(
                event_series=event_series,
                name=f"Pass {suffix}",
                price=Decimal("30.00"),
                pro_rata_discount=Decimal("5.00"),
                currency="EUR",
                payment_method=TicketTier.PaymentMethod.FREE,
                visibility=SeriesPass.Visibility.PUBLIC,
            )
            evt = _make_event(
                event_series.organization,
                event_series,
                f"Evt {suffix}",
                f"evt-{suffix}",
                timezone.now() + timedelta(days=1),
            )
            tier = _make_tier(evt, f"Tier {suffix}")
            SeriesPassTierLink.objects.create(series_pass=pass_, event=evt, tier=tier)
            return HeldSeriesPass.objects.create(
                series_pass=pass_, user=revel_user, status=HeldSeriesPass.Status.ACTIVE, price_paid=pass_.price
            )

        _make_held_pass("a")
        _make_held_pass("b")
        url = reverse("api:list_my_series_passes")

        with CaptureQueriesContext(connection) as baseline_ctx:
            baseline_response = revel_user_client.get(url)
        assert baseline_response.status_code == 200
        assert baseline_response.json()["count"] == 2
        baseline_count = len(baseline_ctx.captured_queries)

        _make_held_pass("c")
        _make_held_pass("d")

        with CaptureQueriesContext(connection) as scaled_ctx:
            scaled_response = revel_user_client.get(url)
        assert scaled_response.status_code == 200
        assert scaled_response.json()["count"] == 4
        scaled_count = len(scaled_ctx.captured_queries)

        additional_per_pass = (scaled_count - baseline_count) / 2
        assert additional_per_pass < 2, (
            f"Query count scaled with held-pass count: {baseline_count} queries for 2, "
            f"{scaled_count} for 4 ({additional_per_pass:.1f} per extra pass)."
        )


# ---- File downloads ----


class TestFileDownloads:
    def test_pdf_download_owner_gets_bytes(self, revel_user_client: Client, held_pass: HeldSeriesPass) -> None:
        url = reverse("api:series_pass_pdf_download", kwargs={"held_pass_id": held_pass.id})
        with patch("events.service.series_pass_file_service.get_or_generate_pass_pdf", return_value=b"%PDF-mock"):
            response = revel_user_client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert "Content-Disposition" in response
        assert ".pdf" in response["Content-Disposition"]
        assert response.content == b"%PDF-mock"

    def test_pdf_download_non_owner_404(self, other_user_client: Client, held_pass: HeldSeriesPass) -> None:
        url = reverse("api:series_pass_pdf_download", kwargs={"held_pass_id": held_pass.id})
        response = other_user_client.get(url)
        assert response.status_code == 404

    def test_pdf_download_cached_redirects_without_regenerating(
        self, revel_user_client: Client, held_pass: HeldSeriesPass
    ) -> None:
        url = reverse("api:series_pass_pdf_download", kwargs={"held_pass_id": held_pass.id})
        with (
            patch("events.service.series_pass_file_service.is_cache_valid", return_value=True),
            patch(
                "events.controllers.series_pass.get_file_url",
                return_value="https://cdn.example.com/signed/pass.pdf",
            ),
            patch("events.service.series_pass_file_service.get_or_generate_pass_pdf") as mock_generate,
        ):
            response = revel_user_client.get(url)

        assert response.status_code == 302
        assert response["Location"] == "https://cdn.example.com/signed/pass.pdf"
        mock_generate.assert_not_called()

    def test_pdf_download_redirects_after_generating_when_signed_url_available(
        self, revel_user_client: Client, held_pass: HeldSeriesPass
    ) -> None:
        url = reverse("api:series_pass_pdf_download", kwargs={"held_pass_id": held_pass.id})
        with (
            patch("events.service.series_pass_file_service.is_cache_valid", return_value=False),
            patch("events.service.series_pass_file_service.get_or_generate_pass_pdf", return_value=b"%PDF-mock"),
            patch(
                "events.controllers.series_pass.get_file_url",
                return_value="https://cdn.example.com/signed/pass.pdf",
            ),
        ):
            response = revel_user_client.get(url)

        assert response.status_code == 302
        assert response["Location"] == "https://cdn.example.com/signed/pass.pdf"

    def test_pkpass_download_owner_gets_bytes(
        self, revel_user_client: Client, held_pass: HeldSeriesPass, settings: t.Any
    ) -> None:
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.example.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"
        settings.APPLE_WALLET_CERT_PATH = "/path/cert.pem"
        settings.APPLE_WALLET_KEY_PATH = "/path/key.pem"
        settings.APPLE_WALLET_WWDR_CERT_PATH = "/path/wwdr.pem"

        url = reverse("api:series_pass_apple_wallet_pass", kwargs={"held_pass_id": held_pass.id})
        with patch("events.service.series_pass_file_service.get_or_generate_pass_pkpass", return_value=b"PK-mock"):
            response = revel_user_client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/vnd.apple.pkpass"
        assert "Content-Disposition" in response
        assert ".pkpass" in response["Content-Disposition"]
        assert response.content == b"PK-mock"

    def test_pkpass_unconfigured_returns_503(
        self, revel_user_client: Client, held_pass: HeldSeriesPass, settings: t.Any
    ) -> None:
        settings.APPLE_WALLET_PASS_TYPE_ID = ""
        settings.APPLE_WALLET_TEAM_ID = ""
        settings.APPLE_WALLET_CERT_PATH = ""
        settings.APPLE_WALLET_KEY_PATH = ""
        settings.APPLE_WALLET_WWDR_CERT_PATH = ""

        url = reverse("api:series_pass_apple_wallet_pass", kwargs={"held_pass_id": held_pass.id})
        response = revel_user_client.get(url)

        assert response.status_code == 503

    def test_pkpass_non_owner_404(self, other_user_client: Client, held_pass: HeldSeriesPass, settings: t.Any) -> None:
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.example.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"
        settings.APPLE_WALLET_CERT_PATH = "/path/cert.pem"
        settings.APPLE_WALLET_KEY_PATH = "/path/key.pem"
        settings.APPLE_WALLET_WWDR_CERT_PATH = "/path/wwdr.pem"

        url = reverse("api:series_pass_apple_wallet_pass", kwargs={"held_pass_id": held_pass.id})
        response = other_user_client.get(url)

        assert response.status_code == 404
