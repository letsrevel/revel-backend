"""Admin series-pass controller tests: permissions, CRUD, tier-link extend, holders, offline confirm, cancel."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import orjson
import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db


# ---- Client fixtures: this suite lives outside test_controllers/, so the shared
# organization_owner_client/organization_staff_client fixtures aren't inherited. ----


@pytest.fixture
def organization_owner_client(organization_owner_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def organization_staff_client(organization_staff_user: RevelUser, staff_member: OrganizationStaff) -> Client:
    refresh = RefreshToken.for_user(organization_staff_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def stranger_client(revel_user_factory: RevelUserFactory) -> Client:
    """An authenticated user with no relationship to ``organization`` at all."""
    stranger = revel_user_factory(username="stranger@example.com", email="stranger@example.com")
    refresh = RefreshToken.for_user(stranger)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def another_org_staff_client(revel_user_factory: RevelUserFactory) -> Client:
    """Staff member of a *different* organization — no relationship to ``organization``."""
    another_owner = revel_user_factory(username="another_owner@example.com", email="another_owner@example.com")
    another_org = Organization.objects.create(name="Another Org", slug="another-org", owner=another_owner)
    staff_user = revel_user_factory(username="another_staff@example.com", email="another_staff@example.com")
    OrganizationStaff.objects.create(
        organization=another_org,
        user=staff_user,
        permissions=PermissionsSchema(default=PermissionMap(edit_event_series=True)).model_dump(mode="json"),
    )
    refresh = RefreshToken.for_user(staff_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


# ---- Extra model fixtures ----


@pytest.fixture
def offline_series_pass(event_series: EventSeries) -> SeriesPass:
    """A series pass paid OFFLINE, distinct from the FREE ``series_pass`` fixture."""
    return SeriesPass.objects.create(
        event_series=event_series,
        name="Offline Season Ticket",
        price=Decimal("50.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )


def _create_payload(**overrides: t.Any) -> dict[str, t.Any]:
    payload: dict[str, t.Any] = {
        "name": "Season Ticket",
        "price": "100.00",
        "pro_rata_discount": "10.00",
        "payment_method": "online",
        "purchasable_by": "public",
        "tier_links": [],
    }
    payload.update(overrides)
    return payload


def _post_json(client: Client, url: str, payload: t.Any) -> t.Any:
    return client.post(url, data=orjson.dumps(payload), content_type="application/json")


def _patch_json(client: Client, url: str, payload: t.Any) -> t.Any:
    return client.patch(url, data=orjson.dumps(payload), content_type="application/json")


# ---- Permissions ----


def test_create_series_pass_by_stranger_returns_404(stranger_client: Client, event_series: EventSeries) -> None:
    """A user with no relationship to the org gets 404 (private org filtered out of for_user)."""
    url = reverse("api:create_series_pass", kwargs={"series_id": event_series.pk})
    response = _post_json(stranger_client, url, _create_payload())
    assert response.status_code == 404


def test_create_series_pass_by_staff_of_another_org_returns_404(
    another_org_staff_client: Client, event_series: EventSeries
) -> None:
    """Staff of an unrelated organization can't see this series either."""
    url = reverse("api:create_series_pass", kwargs={"series_id": event_series.pk})
    response = _post_json(another_org_staff_client, url, _create_payload())
    assert response.status_code == 404


def test_create_series_pass_by_staff_without_permission_returns_403(
    organization_staff_client: Client, staff_member: OrganizationStaff, event_series: EventSeries
) -> None:
    """A staff member of the SAME org without 'edit_event_series' gets 403."""
    perms = staff_member.permissions
    perms["default"]["edit_event_series"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_series_pass", kwargs={"series_id": event_series.pk})
    response = _post_json(organization_staff_client, url, _create_payload())
    assert response.status_code == 403


def test_create_series_pass_by_staff_with_permission_succeeds(
    organization_staff_client: Client, staff_member: OrganizationStaff, event_series: EventSeries
) -> None:
    """A staff member of the SAME org WITH 'edit_event_series' can create."""
    perms = staff_member.permissions
    perms["default"]["edit_event_series"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_series_pass", kwargs={"series_id": event_series.pk})
    response = _post_json(organization_staff_client, url, _create_payload())
    assert response.status_code == 200


# ---- Create: coverage gate ----


def test_create_series_pass_by_owner_creates_pass_and_links(
    organization_owner_client: Client, event_series: EventSeries, event: Event, ticket_tier: TicketTier
) -> None:
    payload = _create_payload(tier_links=[{"event_id": str(event.id), "tier_id": str(ticket_tier.id)}])
    url = reverse("api:create_series_pass", kwargs={"series_id": event_series.pk})
    response = _post_json(organization_owner_client, url, payload)

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Season Ticket"
    series_pass = SeriesPass.objects.get(pk=data["id"])
    assert SeriesPassTierLink.objects.filter(series_pass=series_pass, event=event, tier=ticket_tier).exists()


def test_create_series_pass_on_recurring_series_returns_400(
    organization_owner_client: Client, recurring_series: EventSeries
) -> None:
    """The enable-time gate rejects series passes on recurring series."""
    url = reverse("api:create_series_pass", kwargs={"series_id": recurring_series.pk})
    response = _post_json(organization_owner_client, url, _create_payload())

    assert response.status_code == 400
    assert not SeriesPass.objects.filter(event_series=recurring_series).exists()


def test_create_series_pass_with_event_from_another_series_returns_400(
    organization_owner_client: Client, event_series: EventSeries, foreign_event: Event
) -> None:
    """An event belonging to a different series fails the coverage gate."""
    foreign_tier = TicketTier.objects.create(
        event=foreign_event,
        name="Foreign Tier",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    payload = _create_payload(tier_links=[{"event_id": str(foreign_event.id), "tier_id": str(foreign_tier.id)}])
    url = reverse("api:create_series_pass", kwargs={"series_id": event_series.pk})
    response = _post_json(organization_owner_client, url, payload)

    assert response.status_code == 400
    assert not SeriesPass.objects.filter(event_series=event_series).exists()


# ---- Update ----


def test_update_series_pass_updates_price_exclude_unset_honored(
    organization_owner_client: Client, series_pass: SeriesPass
) -> None:
    url = reverse(
        "api:update_series_pass", kwargs={"series_id": series_pass.event_series_id, "pass_id": series_pass.pk}
    )
    response = _patch_json(organization_owner_client, url, {"price": "42.00"})

    assert response.status_code == 200
    series_pass.refresh_from_db()
    assert series_pass.price == Decimal("42.00")
    # Untouched fields keep their original values (exclude_unset, not a null-out).
    assert series_pass.pro_rata_discount == Decimal("6.00")
    assert series_pass.name == "Season Ticket"


# ---- Delete ----


def test_delete_series_pass_without_holders_deletes(organization_owner_client: Client, series_pass: SeriesPass) -> None:
    url = reverse(
        "api:delete_series_pass", kwargs={"series_id": series_pass.event_series_id, "pass_id": series_pass.pk}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not SeriesPass.objects.filter(pk=series_pass.pk).exists()


def test_delete_series_pass_with_active_holder_returns_409(
    organization_owner_client: Client, series_pass: SeriesPass, revel_user: RevelUser
) -> None:
    HeldSeriesPass.objects.create(
        series_pass=series_pass, user=revel_user, status=HeldSeriesPass.Status.ACTIVE, price_paid=series_pass.price
    )
    url = reverse(
        "api:delete_series_pass", kwargs={"series_id": series_pass.event_series_id, "pass_id": series_pass.pk}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 409
    assert SeriesPass.objects.filter(pk=series_pass.pk).exists()


def test_delete_series_pass_with_only_cancelled_holder_returns_409(
    organization_owner_client: Client, series_pass: SeriesPass, revel_user: RevelUser
) -> None:
    """``HeldSeriesPass.series_pass`` is PROTECT (audit trail), so even a cancelled-only
    holder blocks a hard delete — the service turns that ProtectedError into a 409
    rather than a 500."""
    HeldSeriesPass.objects.create(
        series_pass=series_pass, user=revel_user, status=HeldSeriesPass.Status.CANCELLED, price_paid=series_pass.price
    )
    url = reverse(
        "api:delete_series_pass", kwargs={"series_id": series_pass.event_series_id, "pass_id": series_pass.pk}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 409
    assert SeriesPass.objects.filter(pk=series_pass.pk).exists()


# ---- Tier links: extend / remove ----


def test_add_series_pass_tier_links_creates_and_dispatches_materialization(
    organization_owner_client: Client,
    series_pass: SeriesPass,
    event: Event,
    ticket_tier: TicketTier,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    url = reverse(
        "api:add_series_pass_tier_links", kwargs={"series_id": series_pass.event_series_id, "pass_id": series_pass.pk}
    )
    payload = [{"event_id": str(event.id), "tier_id": str(ticket_tier.id)}]

    with patch("events.service.series_pass_service.materialize_series_pass_holders.delay") as mock_delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = _post_json(organization_owner_client, url, payload)

    assert response.status_code == 200
    assert SeriesPassTierLink.objects.filter(series_pass=series_pass, event=event, tier=ticket_tier).exists()
    mock_delay.assert_called_once()


def test_remove_series_pass_tier_link_without_holders_deletes(
    organization_owner_client: Client, series_pass: SeriesPass, event: Event, ticket_tier: TicketTier
) -> None:
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=ticket_tier)
    url = reverse(
        "api:remove_series_pass_tier_link",
        kwargs={"series_id": series_pass.event_series_id, "pass_id": series_pass.pk, "event_id": event.pk},
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not SeriesPassTierLink.objects.filter(series_pass=series_pass, event=event).exists()


def test_remove_series_pass_tier_link_with_active_holder_returns_409(
    organization_owner_client: Client,
    series_pass: SeriesPass,
    event: Event,
    ticket_tier: TicketTier,
    revel_user: RevelUser,
) -> None:
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=ticket_tier)
    HeldSeriesPass.objects.create(
        series_pass=series_pass, user=revel_user, status=HeldSeriesPass.Status.ACTIVE, price_paid=series_pass.price
    )
    url = reverse(
        "api:remove_series_pass_tier_link",
        kwargs={"series_id": series_pass.event_series_id, "pass_id": series_pass.pk, "event_id": event.pk},
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 409
    assert SeriesPassTierLink.objects.filter(series_pass=series_pass, event=event).exists()


# ---- Holders list ----


def test_list_series_pass_holders_search_by_email(
    organization_owner_client: Client,
    series_pass: SeriesPass,
    revel_user: RevelUser,
    revel_user_factory: RevelUserFactory,
) -> None:
    other_user = revel_user_factory(username="other_holder@example.com", email="other_holder@example.com")
    HeldSeriesPass.objects.create(
        series_pass=series_pass, user=revel_user, status=HeldSeriesPass.Status.ACTIVE, price_paid=series_pass.price
    )
    HeldSeriesPass.objects.create(
        series_pass=series_pass, user=other_user, status=HeldSeriesPass.Status.ACTIVE, price_paid=series_pass.price
    )
    url = reverse(
        "api:list_series_pass_holders", kwargs={"series_id": series_pass.event_series_id, "pass_id": series_pass.pk}
    )
    response = organization_owner_client.get(url, {"search": revel_user.email})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["user"]["email"] == revel_user.email


def test_list_series_pass_holders_query_count_does_not_grow_with_holder_count(
    organization_owner_client: Client, series_pass: SeriesPass, revel_user_factory: RevelUserFactory
) -> None:
    """Adding more holder rows must not add per-row queries (select_related('user'))."""
    url = reverse(
        "api:list_series_pass_holders", kwargs={"series_id": series_pass.event_series_id, "pass_id": series_pass.pk}
    )

    for i in range(2):
        user = revel_user_factory(username=f"baseline_holder{i}@example.com", email=f"baseline_holder{i}@example.com")
        HeldSeriesPass.objects.create(
            series_pass=series_pass, user=user, status=HeldSeriesPass.Status.ACTIVE, price_paid=series_pass.price
        )
    with CaptureQueriesContext(connection) as baseline_ctx:
        baseline_response = organization_owner_client.get(url)
    assert baseline_response.status_code == 200
    assert baseline_response.json()["count"] == 2
    baseline_count = len(baseline_ctx.captured_queries)

    for i in range(2, 4):
        user = revel_user_factory(username=f"scaled_holder{i}@example.com", email=f"scaled_holder{i}@example.com")
        HeldSeriesPass.objects.create(
            series_pass=series_pass, user=user, status=HeldSeriesPass.Status.ACTIVE, price_paid=series_pass.price
        )
    with CaptureQueriesContext(connection) as scaled_ctx:
        scaled_response = organization_owner_client.get(url)
    assert scaled_response.status_code == 200
    assert scaled_response.json()["count"] == 4
    scaled_count = len(scaled_ctx.captured_queries)

    additional_per_holder = (scaled_count - baseline_count) / 2
    assert additional_per_holder < 2, (
        f"Query count scaled with holder count: {baseline_count} for 2 holders, {scaled_count} for 4."
    )


# ---- Offline payment confirmation ----


@pytest.fixture
def pending_offline_held_pass(offline_series_pass: SeriesPass, revel_user: RevelUser) -> HeldSeriesPass:
    return HeldSeriesPass.objects.create(
        series_pass=offline_series_pass,
        user=revel_user,
        status=HeldSeriesPass.Status.PENDING,
        price_paid=offline_series_pass.price,
    )


@pytest.fixture
def pending_offline_ticket(pending_offline_held_pass: HeldSeriesPass, event: Event, ticket_tier: TicketTier) -> Ticket:
    return Ticket.objects.create(
        event=event,
        tier=ticket_tier,
        user=pending_offline_held_pass.user,
        held_pass=pending_offline_held_pass,
        status=Ticket.TicketStatus.PENDING,
        guest_name="Pass Holder",
    )


def test_confirm_series_pass_payment_activates_pass_and_tickets_and_notifies_once(
    organization_owner_client: Client,
    offline_series_pass: SeriesPass,
    pending_offline_held_pass: HeldSeriesPass,
    pending_offline_ticket: Ticket,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    url = reverse(
        "api:confirm_series_pass_payment",
        kwargs={"series_id": offline_series_pass.event_series_id, "held_pass_id": pending_offline_held_pass.pk},
    )

    with patch("events.service.series_pass_service.send_series_pass_purchased") as mock_notify:
        with django_capture_on_commit_callbacks(execute=True):
            response = organization_owner_client.post(url)

    assert response.status_code == 200
    pending_offline_held_pass.refresh_from_db()
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_held_pass.status == HeldSeriesPass.Status.ACTIVE
    assert pending_offline_ticket.status == Ticket.TicketStatus.ACTIVE
    mock_notify.assert_called_once_with(pending_offline_held_pass.id)


def test_confirm_series_pass_payment_non_offline_pass_returns_400(
    organization_owner_client: Client, series_pass: SeriesPass, revel_user: RevelUser
) -> None:
    """``series_pass`` fixture is FREE, not OFFLINE."""
    held_pass = HeldSeriesPass.objects.create(
        series_pass=series_pass, user=revel_user, status=HeldSeriesPass.Status.PENDING, price_paid=series_pass.price
    )
    url = reverse(
        "api:confirm_series_pass_payment",
        kwargs={"series_id": series_pass.event_series_id, "held_pass_id": held_pass.pk},
    )
    response = organization_owner_client.post(url)
    assert response.status_code == 400


def test_confirm_series_pass_payment_already_active_returns_400(
    organization_owner_client: Client, offline_series_pass: SeriesPass, revel_user: RevelUser
) -> None:
    held_pass = HeldSeriesPass.objects.create(
        series_pass=offline_series_pass,
        user=revel_user,
        status=HeldSeriesPass.Status.ACTIVE,
        price_paid=offline_series_pass.price,
    )
    url = reverse(
        "api:confirm_series_pass_payment",
        kwargs={"series_id": offline_series_pass.event_series_id, "held_pass_id": held_pass.pk},
    )
    response = organization_owner_client.post(url)
    assert response.status_code == 400


def test_confirm_series_pass_payment_wrong_series_returns_404(
    organization_owner_client: Client,
    foreign_series: EventSeries,
    offline_series_pass: SeriesPass,
    revel_user: RevelUser,
) -> None:
    held_pass = HeldSeriesPass.objects.create(
        series_pass=offline_series_pass,
        user=revel_user,
        status=HeldSeriesPass.Status.PENDING,
        price_paid=offline_series_pass.price,
    )
    url = reverse(
        "api:confirm_series_pass_payment",
        kwargs={"series_id": foreign_series.pk, "held_pass_id": held_pass.pk},
    )
    response = organization_owner_client.post(url)
    assert response.status_code == 404


# ---- Cancel ----


def test_cancel_series_pass_delegates_to_cancel_held_pass_no_refund_for_free_pass(
    organization_owner_client: Client, series_pass: SeriesPass, revel_user: RevelUser
) -> None:
    """``series_pass`` fixture is FREE — no Stripe refund should ever be attempted."""
    held_pass = HeldSeriesPass.objects.create(
        series_pass=series_pass, user=revel_user, status=HeldSeriesPass.Status.ACTIVE, price_paid=series_pass.price
    )
    url = reverse(
        "api:cancel_series_pass", kwargs={"series_id": series_pass.event_series_id, "held_pass_id": held_pass.pk}
    )

    with patch("events.service.series_pass_service.cancellation_service._issue_stripe_refund") as mock_refund:
        response = _post_json(organization_owner_client, url, {"reason": "no longer needed"})

    assert response.status_code == 200
    held_pass.refresh_from_db()
    assert held_pass.status == HeldSeriesPass.Status.CANCELLED
    mock_refund.assert_not_called()
