"""Tests for the OrganizationAdmin changelist filters and the payments-volume column.

The filters exist so an operator can narrow a large organization list: which orgs
finished Stripe onboarding, which ones actually run events, and where each stands
on VAT. The payments column makes the list sortable by processing volume.

Each test drives the real changelist URL rather than the filter classes in
isolation, so a filter that is implemented but never wired into ``list_filter``
still fails.
"""

import typing as t
import uuid
from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, Organization, Payment, Ticket, TicketTier

pytestmark = pytest.mark.django_db

CHANGELIST = reverse("admin:events_organization_changelist")

# The changelist renders Unfold templates that reach for hashed static assets.
NO_MANIFEST_STORAGE = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)


def _org(owner: RevelUser, name: str, **kwargs: t.Any) -> Organization:
    return Organization.objects.create(name=name, slug=name.lower().replace(" ", "-"), owner=owner, **kwargs)


def _event(org: Organization, *, is_template: bool = False) -> Event:
    return Event.objects.create(
        organization=org,
        name=f"{org.name} Event",
        slug=f"event-{org.slug}",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=100,
        # The template_events_must_be_draft constraint forbids an OPEN template.
        status=Event.EventStatus.DRAFT if is_template else Event.EventStatus.OPEN,
        start=timezone.now(),
        requires_ticket=True,
        is_template=is_template,
    )


def _succeeded_payment(
    event: Event,
    user: RevelUser,
    *,
    status: Payment.PaymentStatus = Payment.PaymentStatus.SUCCEEDED,
) -> Payment:
    # TicketTier has a unique (event, name) constraint, so each ticket needs its own tier.
    tier = TicketTier.objects.create(event=event, name=f"GA-{uuid.uuid4().hex[:8]}")
    ticket = Ticket.objects.create(
        event=event, user=user, tier=tier, status=Ticket.TicketStatus.ACTIVE, guest_name="Guest"
    )
    return Payment.objects.create(
        ticket=ticket,
        user=user,
        amount=Decimal("40.00"),
        platform_fee=Decimal("0.00"),
        currency="EUR",
        status=status,
        stripe_session_id=f"cs_test_{ticket.id}",
    )


def _changelist(admin_client: Client, query: str = "") -> t.Any:
    """GET the organization changelist and return its ``ChangeList``."""
    response = admin_client.get(f"{CHANGELIST}{query}")
    assert response.status_code == 200
    context = response.context_data
    assert context is not None
    return context["cl"]


def _payments_column_index(admin_client: Client) -> int:
    """Position of the Payments column for the ``?o=`` sort param.

    Read off the rendered ChangeList rather than ``ModelAdmin.list_display``:
    Django prepends ``action_checkbox`` when the admin defines actions, so the
    two lists are off by one.
    """
    return list(_changelist(admin_client).list_display).index("payments_count")


def _names(admin_client: Client, query: str = "") -> set[str]:
    """Return the org names the changelist yields for ``query``."""
    return {org.name for org in _changelist(admin_client, query).queryset}


@pytest.fixture
def stripe_orgs(revel_user_factory: RevelUserFactory) -> dict[str, Organization]:
    """Three orgs spanning the Stripe-onboarding states, including a half-finished one."""
    owner = revel_user_factory()
    return {
        "connected": _org(
            owner,
            "Connected",
            stripe_account_id="acct_connected",
            stripe_charges_enabled=True,
            stripe_details_submitted=True,
        ),
        # Onboarding started but charges never enabled — must not read as connected.
        "half": _org(
            owner,
            "Half",
            stripe_account_id="acct_half",
            stripe_charges_enabled=False,
            stripe_details_submitted=True,
        ),
        "none": _org(owner, "None"),
    }


@NO_MANIFEST_STORAGE
def test_stripe_connected_filter_requires_all_three_columns(
    admin_client: Client, stripe_orgs: dict[str, Organization]
) -> None:
    """Only an org with an account id, charges enabled AND details submitted is 'connected'."""
    assert _names(admin_client, "?stripe_connected=yes") == {"Connected"}
    assert _names(admin_client, "?stripe_connected=no") == {"Half", "None"}


@NO_MANIFEST_STORAGE
def test_stripe_connected_filter_absent_returns_everything(
    admin_client: Client, stripe_orgs: dict[str, Organization]
) -> None:
    """No filter selected leaves the queryset untouched."""
    assert _names(admin_client) == {"Connected", "Half", "None"}


@NO_MANIFEST_STORAGE
def test_has_events_filter_ignores_template_events(admin_client: Client, revel_user_factory: RevelUserFactory) -> None:
    """A recurring-series template is not a real event, so its org has no events."""
    owner = revel_user_factory()
    with_event = _org(owner, "With Event")
    _event(with_event)
    template_only = _org(owner, "Template Only")
    _event(template_only, is_template=True)
    _org(owner, "Empty")

    assert _names(admin_client, "?has_events=yes") == {"With Event"}
    assert _names(admin_client, "?has_events=no") == {"Template Only", "Empty"}


@NO_MANIFEST_STORAGE
def test_vat_status_filter_partitions_all_orgs(admin_client: Client, revel_user_factory: RevelUserFactory) -> None:
    """The three VAT lookups are mutually exclusive and together cover every org."""
    owner = revel_user_factory()
    _org(owner, "No Vat")
    _org(owner, "Unvalidated", vat_id="IT12345678901", vat_id_validated=False)
    _org(owner, "Validated", vat_id="AT12345678901", vat_id_validated=True)

    assert _names(admin_client, "?vat_status=none") == {"No Vat"}
    assert _names(admin_client, "?vat_status=unvalidated") == {"Unvalidated"}
    assert _names(admin_client, "?vat_status=validated") == {"Validated"}


@NO_MANIFEST_STORAGE
def test_filters_compose(admin_client: Client, revel_user_factory: RevelUserFactory) -> None:
    """Two filters at once narrow rather than replace each other."""
    owner = revel_user_factory()
    connected_kwargs: dict[str, t.Any] = {
        "stripe_account_id": "acct_both",
        "stripe_charges_enabled": True,
        "stripe_details_submitted": True,
    }
    both = _org(owner, "Both", **connected_kwargs)
    _event(both)
    _org(
        owner, "Stripe Only", stripe_account_id="acct_solo", stripe_charges_enabled=True, stripe_details_submitted=True
    )
    events_only = _org(owner, "Events Only")
    _event(events_only)

    assert _names(admin_client, "?stripe_connected=yes&has_events=yes") == {"Both"}


@NO_MANIFEST_STORAGE
def test_payments_count_counts_only_succeeded_payments(
    admin_client: Client, revel_user_factory: RevelUserFactory
) -> None:
    """Pending and failed checkouts must not inflate an org's processing volume."""
    owner = revel_user_factory()
    buyer = revel_user_factory()
    org = _org(owner, "Seller")
    event = _event(org)
    _succeeded_payment(event, buyer)
    _succeeded_payment(event, buyer)
    _succeeded_payment(event, buyer, status=Payment.PaymentStatus.PENDING)
    _succeeded_payment(event, buyer, status=Payment.PaymentStatus.FAILED)

    row = _changelist(admin_client).queryset.get(pk=org.pk)

    assert row._payments_count == 2


@NO_MANIFEST_STORAGE
def test_payments_count_is_zero_not_null_for_orgs_without_sales(
    admin_client: Client, revel_user_factory: RevelUserFactory
) -> None:
    """Coalesced to 0 so the column renders and sorts sanely for orgs that never sold."""
    owner = revel_user_factory()
    org = _org(owner, "Quiet")

    row = _changelist(admin_client).queryset.get(pk=org.pk)

    assert row._payments_count == 0


@NO_MANIFEST_STORAGE
def test_changelist_orders_by_payment_volume(admin_client: Client, revel_user_factory: RevelUserFactory) -> None:
    """The Payments column is sortable, descending, with no-sales orgs last."""
    owner = revel_user_factory()
    buyer = revel_user_factory()

    busy = _org(owner, "Busy")
    busy_event = _event(busy)
    for _ in range(3):
        _succeeded_payment(busy_event, buyer)

    quiet = _org(owner, "Quiet")
    quiet_event = _event(quiet)
    _succeeded_payment(quiet_event, buyer)

    _org(owner, "Dormant")

    # Django's ORDER_VAR is a 0-based index into list_display; "-" prefixes descending.
    ordered = [org.name for org in _changelist(admin_client, f"?o=-{_payments_column_index(admin_client)}").queryset]

    assert ordered == ["Busy", "Quiet", "Dormant"]
