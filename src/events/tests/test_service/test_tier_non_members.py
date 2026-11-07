"""Ticket tiers that members cannot buy ("non-members only", #1316)."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import orjson
import pytest
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja.errors import HttpError
from ninja_extra.exceptions import PermissionDenied
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.controllers.permissions import CanPurchaseTicket
from events.models import (
    Event,
    EventSeries,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.schema import TicketPurchaseItem
from events.service import tier_eligibility
from events.service.batch_ticket_service import BatchTicketService
from events.service.batch_ticket_service.eligibility import assert_cart_purchasable_by
from events.service.duplication import duplicate_event
from events.service.event_manager import NextStep, ReasonCode, UserIsIneligibleError
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.service.ticket_service import UserEventStatus, get_user_event_status

pytestmark = pytest.mark.django_db

PB = TicketTier.PurchasableBy
Status = OrganizationMember.MembershipStatus


def _client(user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def gold(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Gold")


@pytest.fixture
def silver(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Silver")


@pytest.fixture
def nm_tier(event: Event) -> TicketTier:
    """A free non-members-only tier (event comes with a default tier we leave alone)."""
    return TicketTier.objects.create(
        event=event,
        name="Non-members",
        price=Decimal("0"),
        payment_method=TicketTier.PaymentMethod.FREE,
        purchasable_by=PB.NON_MEMBERS,
    )


@pytest.fixture
def gold_excluded_tier(nm_tier: TicketTier, gold: MembershipTier) -> TicketTier:
    nm_tier.excluded_membership_tiers.add(gold)
    return TicketTier.objects.get(pk=nm_tier.pk)


def _join(
    organization: Organization,
    user: RevelUser,
    status: str = Status.ACTIVE,
    tier: MembershipTier | None = None,
) -> OrganizationMember:
    return OrganizationMember.objects.create(organization=organization, user=user, status=status, tier=tier)


def _names(event: Event, user: RevelUser) -> set[str]:
    return {x.name for x in tier_eligibility.get_eligible_tiers(event, user)}


class TestEligibleTiers:
    def test_active_member_excluded(
        self, event: Event, organization: Organization, nm_tier: TicketTier, member_user: RevelUser
    ) -> None:
        _join(organization, member_user)
        assert "Non-members" not in _names(event, member_user)

    @pytest.mark.parametrize("status", [Status.PAUSED, Status.CANCELLED])
    def test_inactive_member_included(
        self, event: Event, organization: Organization, nm_tier: TicketTier, member_user: RevelUser, status: str
    ) -> None:
        _join(organization, member_user, status)
        assert "Non-members" in _names(event, member_user)

    def test_non_member_owner_staff_included(
        self,
        event: Event,
        nm_tier: TicketTier,
        public_user: RevelUser,
        organization_owner_user: RevelUser,
        organization_staff_user: RevelUser,
        staff_member: OrganizationStaff,
        organization: Organization,
    ) -> None:
        # Staff are exempt even when they hold an active membership.
        _join(organization, organization_staff_user)
        for user in (public_user, organization_owner_user, organization_staff_user):
            assert "Non-members" in _names(event, user)

    def test_excluded_tiers_only_exclude_those_members(
        self,
        event: Event,
        organization: Organization,
        gold_excluded_tier: TicketTier,
        gold: MembershipTier,
        silver: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        gold_user, silver_user, plain_user = (
            django_user_model.objects.create_user(username=n, email=f"{n}@example.com", password="pass")
            for n in ("gold_u", "silver_u", "plain_u")
        )
        _join(organization, gold_user, tier=gold)
        _join(organization, silver_user, tier=silver)
        _join(organization, plain_user)
        assert "Non-members" not in _names(event, gold_user)
        assert "Non-members" in _names(event, silver_user)
        assert "Non-members" in _names(event, plain_user)

    def test_anonymous_can_purchase(self, event: Event, nm_tier: TicketTier) -> None:
        assert tier_eligibility.anonymous_can_purchase(nm_tier, event, None) is True


class TestListing:
    def test_member_sees_tier_not_purchasable(
        self,
        event: Event,
        organization: Organization,
        gold_excluded_tier: TicketTier,
        gold: MembershipTier,
        member_user: RevelUser,
        public_user: RevelUser,
    ) -> None:
        _join(organization, member_user, tier=gold)
        url = reverse("api:tier_list", kwargs={"event_id": event.pk})

        rows = {r["name"]: r for r in _client(member_user).get(url).json()}
        assert rows["Non-members"]["can_purchase"] is False
        assert [x["name"] for x in rows["Non-members"]["excluded_membership_tiers"]] == ["Gold"]

        rows = {r["name"]: r for r in _client(public_user).get(url).json()}
        assert rows["Non-members"]["can_purchase"] is True


def _buy(event: Event, tier: TicketTier, user: RevelUser) -> list[Ticket]:
    result = BatchTicketService(event, tier, user).create_batch([TicketPurchaseItem(guest_name="Guest")])
    assert isinstance(result, list)
    return result


class TestCheckout:
    def test_active_member_blocked(
        self, event: Event, organization: Organization, nm_tier: TicketTier, member_user: RevelUser
    ) -> None:
        _join(organization, member_user)
        with pytest.raises(UserIsIneligibleError) as exc_info:
            _buy(event, nm_tier, member_user)
        eligibility = exc_info.value.eligibility
        assert eligibility.reason_code == ReasonCode.MEMBERS_EXCLUDED
        assert eligibility.next_step == NextStep.CHOOSE_ANOTHER_TIER
        assert not Ticket.objects.filter(tier=nm_tier).exists()

    def test_other_membership_tier_allowed(
        self,
        event: Event,
        organization: Organization,
        gold_excluded_tier: TicketTier,
        silver: MembershipTier,
        member_user: RevelUser,
    ) -> None:
        _join(organization, member_user, tier=silver)
        assert len(_buy(event, gold_excluded_tier, member_user)) == 1

    def test_non_member_staff_owner_succeed(
        self,
        event: Event,
        organization: Organization,
        nm_tier: TicketTier,
        django_user_model: type[RevelUser],
        organization_owner_user: RevelUser,
        organization_staff_user: RevelUser,
        staff_member: OrganizationStaff,
    ) -> None:
        _join(organization, organization_staff_user)
        buyer = django_user_model.objects.create_user(username="buyer", email="buyer@example.com", password="pass")
        for user in (buyer, organization_owner_user, organization_staff_user):
            assert len(_buy(event, nm_tier, user)) == 1

    def test_multi_tier_endpoint_returns_400_payload(
        self, event: Event, organization: Organization, nm_tier: TicketTier, member_user: RevelUser
    ) -> None:
        _join(organization, member_user)
        response = _client(member_user).post(
            reverse("api:multi_tier_checkout", kwargs={"event_id": event.pk}),
            data=orjson.dumps({"items": [{"tier_id": str(nm_tier.pk), "tickets": [{"guest_name": "A"}]}]}),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert response.json()["reason_code"] == "members_excluded"
        assert response.json()["next_step"] == "choose_another_tier"

    def test_guest_cart_blocks_active_member(
        self, event: Event, organization: Organization, nm_tier: TicketTier, member_user: RevelUser
    ) -> None:
        _join(organization, member_user)
        with pytest.raises(UserIsIneligibleError):
            assert_cart_purchasable_by(event, member_user, [nm_tier])

    def test_can_purchase_ticket_permission(
        self,
        event: Event,
        organization: Organization,
        nm_tier: TicketTier,
        member_user: RevelUser,
        public_user: RevelUser,
    ) -> None:
        _join(organization, member_user)

        def _check(user: RevelUser) -> bool:
            request = HttpRequest()
            request.user = user
            return CanPurchaseTicket().has_object_permission(request, t.cast(t.Any, None), nm_tier)

        assert _check(public_user) is True
        with pytest.raises(PermissionDenied):
            _check(member_user)


class TestSeriesPass:
    @pytest.fixture
    def nm_pass(self, organization: Organization, event_series: EventSeries, event: Event) -> SeriesPass:
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save(update_fields=["visibility"])
        event.start = timezone.now() + timedelta(days=3)
        event.end = event.start + timedelta(hours=4)
        event.save(update_fields=["start", "end"])
        second = Event.objects.create(
            organization=organization,
            name="Second",
            slug="second",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            event_series=event_series,
            max_attendees=100,
            start=timezone.now() + timedelta(days=6),
            end=timezone.now() + timedelta(days=6, hours=4),
            status=Event.EventStatus.OPEN,
            requires_ticket=True,
        )
        series_pass = SeriesPass.objects.create(
            event_series=event_series,
            name="Non-member Pass",
            price=Decimal("20.00"),
            pro_rata_discount=Decimal("0.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
            visibility=SeriesPass.Visibility.PUBLIC,
            purchasable_by=PB.NON_MEMBERS,
        )
        for covered in (event, second):
            tier = TicketTier.objects.create(
                event=covered, name=f"Pass tier {covered.slug}", price=Decimal("10.00"), currency="EUR",
                payment_method=TicketTier.PaymentMethod.FREE,
            )  # fmt: skip
            SeriesPassTierLink.objects.create(series_pass=series_pass, event=covered, tier=tier)
        return series_pass

    def test_member_blocked(self, nm_pass: SeriesPass, organization: Organization, member_user: RevelUser) -> None:
        _join(organization, member_user)
        with pytest.raises(HttpError) as exc_info:
            SeriesPassPurchaseService(nm_pass, member_user).purchase()
        assert exc_info.value.status_code == 403

    def test_non_member_and_staff_ok(
        self,
        nm_pass: SeriesPass,
        organization: Organization,
        public_user: RevelUser,
        organization_staff_user: RevelUser,
        staff_member: OrganizationStaff,
    ) -> None:
        _join(organization, organization_staff_user)
        SeriesPassPurchaseService(nm_pass, public_user).purchase()
        SeriesPassPurchaseService(nm_pass, organization_staff_user).purchase()


class TestAdminValidation:
    @pytest.fixture
    def owner_client(self, organization_owner_user: RevelUser) -> Client:
        return _client(organization_owner_user)

    @staticmethod
    def _create(client: Client, event: Event, **extra: t.Any) -> t.Any:
        payload = {"name": "T", "price": 0, "payment_method": "free", **extra}
        return client.post(
            reverse("api:create_ticket_tier", kwargs={"event_id": event.pk}),
            data=orjson.dumps(payload),
            content_type="application/json",
        )

    @staticmethod
    def _update(client: Client, tier: TicketTier, **payload: t.Any) -> t.Any:
        return client.put(
            reverse("api:update_ticket_tier", kwargs={"event_id": tier.event_id, "tier_id": tier.pk}),
            data=orjson.dumps(payload),
            content_type="application/json",
        )

    def test_create_non_members_with_excluded(self, owner_client: Client, event: Event, gold: MembershipTier) -> None:
        response = self._create(
            owner_client, event, purchasable_by="non_members", excluded_membership_tiers_ids=[str(gold.pk)]
        )
        assert response.status_code == 200, response.content
        assert [x["name"] for x in response.json()["excluded_membership_tiers"]] == ["Gold"]

    def test_excluded_requires_non_members(self, owner_client: Client, event: Event, gold: MembershipTier) -> None:
        response = self._create(
            owner_client, event, purchasable_by="public", excluded_membership_tiers_ids=[str(gold.pk)]
        )
        assert response.status_code == 400, response.content
        assert not TicketTier.objects.filter(event=event, name="T").exists()

    def test_foreign_org_excluded_tier_404(self, owner_client: Client, event: Event, django_user_model: t.Any) -> None:
        other = Organization.objects.create(
            name="Other",
            slug="other",
            owner=django_user_model.objects.create_user(username="o2", email="o2@example.com", password="p"),
        )
        foreign = MembershipTier.objects.create(organization=other, name="Foreign")
        response = self._create(
            owner_client, event, purchasable_by="non_members", excluded_membership_tiers_ids=[str(foreign.pk)]
        )
        assert response.status_code == 404, response.content
        assert not TicketTier.objects.filter(event=event, name="T").exists()

    def test_non_members_cannot_be_members_only_visible(self, owner_client: Client, event: Event) -> None:
        response = self._create(owner_client, event, purchasable_by="non_members", visibility="members-only")
        assert response.status_code == 400, response.content
        assert not TicketTier.objects.filter(event=event, name="T").exists()

    def test_restricted_and_excluded_together_rejected(
        self, owner_client: Client, event: Event, gold: MembershipTier
    ) -> None:
        for purchasable_by in ("members", "non_members"):
            response = self._create(
                owner_client,
                event,
                purchasable_by=purchasable_by,
                restricted_to_membership_tiers_ids=[str(gold.pk)],
                excluded_membership_tiers_ids=[str(gold.pk)],
            )
            assert response.status_code == 400, response.content
            assert not TicketTier.objects.filter(event=event, name="T").exists()

    def test_single_put_switches_members_to_non_members(
        self, owner_client: Client, event: Event, gold: MembershipTier
    ) -> None:
        tier = TicketTier.objects.create(
            event=event,
            name="Sw",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.MEMBERS,
        )
        tier.restricted_to_membership_tiers.add(gold)
        response = self._update(
            owner_client,
            tier,
            purchasable_by="non_members",
            restricted_to_membership_tiers_ids=[],
            excluded_membership_tiers_ids=[str(gold.pk)],
        )
        assert response.status_code == 200, response.content
        tier = TicketTier.objects.get(pk=tier.pk)
        assert tier.purchasable_by == PB.NON_MEMBERS
        assert not tier.restricted_to_membership_tiers.exists()
        assert list(tier.excluded_membership_tiers.all()) == [gold]

    def test_omitted_keeps_and_empty_clears(self, owner_client: Client, gold_excluded_tier: TicketTier) -> None:
        assert self._update(owner_client, gold_excluded_tier, name="Renamed").status_code == 200
        assert TicketTier.objects.get(pk=gold_excluded_tier.pk).excluded_membership_tiers.count() == 1
        assert self._update(owner_client, gold_excluded_tier, excluded_membership_tiers_ids=[]).status_code == 200
        assert TicketTier.objects.get(pk=gold_excluded_tier.pk).excluded_membership_tiers.count() == 0

    def test_failed_update_rolls_back_links(self, owner_client: Client, gold_excluded_tier: TicketTier) -> None:
        response = self._update(owner_client, gold_excluded_tier, purchasable_by="public")
        assert response.status_code == 400, response.content
        assert TicketTier.objects.get(pk=gold_excluded_tier.pk).purchasable_by == PB.NON_MEMBERS

    def test_update_rejects_non_members_members_only_visibility(
        self, owner_client: Client, nm_tier: TicketTier
    ) -> None:
        response = self._update(owner_client, nm_tier, visibility="members-only")
        assert response.status_code == 400, response.content
        assert TicketTier.objects.get(pk=nm_tier.pk).visibility != "members-only"

    def test_duplicate_ids_do_not_404(self, owner_client: Client, event: Event, gold: MembershipTier) -> None:
        response = self._create(
            owner_client,
            event,
            purchasable_by="non_members",
            excluded_membership_tiers_ids=[str(gold.pk), str(gold.pk)],
        )
        assert response.status_code == 200, response.content
        assert len(response.json()["excluded_membership_tiers"]) == 1

    def test_failed_update_rolls_back_link_changes(
        self, owner_client: Client, gold_excluded_tier: TicketTier, gold: MembershipTier, silver: MembershipTier
    ) -> None:
        """The links are written before the save; a failing full_clean must undo them."""
        response = self._update(
            owner_client,
            gold_excluded_tier,
            purchasable_by="public",
            excluded_membership_tiers_ids=[str(silver.pk)],
        )
        assert response.status_code == 400, response.content
        tier = TicketTier.objects.get(pk=gold_excluded_tier.pk)
        assert tier.purchasable_by == PB.NON_MEMBERS
        assert list(tier.excluded_membership_tiers.all()) == [gold]


def test_duplication_copies_excluded_tiers(event: Event, gold_excluded_tier: TicketTier, gold: MembershipTier) -> None:
    new_event = duplicate_event(event, "Copy", timezone.now() + timedelta(days=30))
    copy = TicketTier.objects.get(event=new_event, name="Non-members")
    assert copy.purchasable_by == PB.NON_MEMBERS
    assert list(copy.excluded_membership_tiers.all()) == [gold]


@pytest.mark.parametrize("status", [Status.PAUSED, Status.CANCELLED])
def test_inactive_member_checkout_succeeds(
    event: Event, organization: Organization, nm_tier: TicketTier, member_user: RevelUser, status: str
) -> None:
    _join(organization, member_user, status)
    assert len(_buy(event, nm_tier, member_user)) == 1


def test_paused_member_can_buy_non_member_pass(
    organization: Organization, member_user: RevelUser, event_series: EventSeries, event: Event
) -> None:
    _join(organization, member_user, Status.PAUSED)
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])
    events = [event]
    for i in (1, 2):
        events.append(
            Event.objects.create(
                organization=organization,
                name=f"E{i}",
                slug=f"e{i}",
                event_type=Event.EventType.PUBLIC,
                visibility=Event.Visibility.PUBLIC,
                event_series=event_series,
                max_attendees=100,
                start=timezone.now() + timedelta(days=3 * i),
                end=timezone.now() + timedelta(days=3 * i, hours=4),
                status=Event.EventStatus.OPEN,
                requires_ticket=True,
            )
        )
    event.start = timezone.now() + timedelta(days=1)
    event.end = event.start + timedelta(hours=4)
    event.save(update_fields=["start", "end"])
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="NM",
        price=Decimal("20.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=SeriesPass.Visibility.PUBLIC,
        purchasable_by=PB.NON_MEMBERS,
    )
    for covered in events:
        tier = TicketTier.objects.create(
            event=covered,
            name=f"PT {covered.slug}",
            price=Decimal("10.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=covered, tier=tier)
    SeriesPassPurchaseService(series_pass, member_user).purchase()


def test_series_pass_non_members_rejects_members_only_visibility(event_series: EventSeries) -> None:
    series_pass = SeriesPass(
        event_series=event_series,
        name="Bad",
        price=Decimal("1"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=SeriesPass.Visibility.MEMBERS_ONLY,
        purchasable_by=PB.NON_MEMBERS,
    )
    with pytest.raises(ValidationError) as exc_info:
        series_pass.clean()
    assert "purchasable_by" in exc_info.value.message_dict


def test_user_event_status_reports_excluded_member_cannot_purchase(
    event: Event, organization: Organization, nm_tier: TicketTier, member_user: RevelUser
) -> None:
    open_tier = TicketTier.objects.create(
        event=event,
        name="Open",
        price=Decimal("0"),
        payment_method=TicketTier.PaymentMethod.FREE,
        purchasable_by=PB.PUBLIC,
    )
    _join(organization, member_user)
    _buy(event, open_tier, member_user)
    result = get_user_event_status(event, member_user)
    assert isinstance(result, UserEventStatus)
    by_tier = {r.tier_id: r for r in result.remaining_tickets}
    assert by_tier[nm_tier.pk].can_purchase is False
    assert by_tier[open_tier.pk].can_purchase is True


def test_can_purchase_ticket_permission_staff_and_other_membership_tier(
    event: Event,
    organization: Organization,
    gold_excluded_tier: TicketTier,
    silver: MembershipTier,
    member_user: RevelUser,
    organization_staff_user: RevelUser,
    staff_member: OrganizationStaff,
) -> None:
    _join(organization, member_user, tier=silver)
    for user in (member_user, organization_staff_user):
        request = HttpRequest()
        request.user = user
        assert CanPurchaseTicket().has_object_permission(request, t.cast(t.Any, None), gold_excluded_tier) is True
