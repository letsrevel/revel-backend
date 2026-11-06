"""Paths that bypass or mis-drive the access-row -> register mirror (#1318)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib import admin
from django.test import RequestFactory

from accounts.models import RevelUser
from accounts.tasks import delete_user_account
from events.exceptions import MembershipTierInUseError
from events.models import (
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)
from events.service import blacklist_service, organization_service

pytestmark = pytest.mark.django_db

Status = OrganizationMember.MembershipStatus
Reason = MembershipRecord.EndedReason


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    """A tier in the organization."""
    return MembershipTier.objects.create(organization=organization, name="Gold")


def _join(organization: Organization, user: RevelUser, tier: MembershipTier | None) -> OrganizationMember:
    return OrganizationMember.objects.create(organization=organization, user=user, tier=tier, status=Status.ACTIVE)


def _offline_subscription(tier: MembershipTier, user: RevelUser) -> MembershipSubscription:
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
    )
    return MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=tier.organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
    )


def test_unban_resets_to_cancelled_and_keeps_record_closed(
    organization: Organization, member_user: RevelUser, organization_owner_user: RevelUser, tier: MembershipTier
) -> None:
    """Pin: unban goes BANNED -> CANCELLED, opens no record, and busts the permission snapshot."""
    _join(organization, member_user, tier)
    entry = blacklist_service.add_to_blacklist(
        organization=organization, created_by=organization_owner_user, user=member_user
    )
    (rec,) = MembershipRecord.objects.filter(organization=organization, user=member_user)
    assert rec.ended_reason == Reason.EXPELLED

    with patch("events.signals.permission_snapshot.invalidate_my_permissions") as invalidate:
        blacklist_service.remove_from_blacklist(entry)

    member = OrganizationMember.objects.get(organization=organization, user=member_user)
    assert member.status == Status.CANCELLED
    assert not MembershipRecord.objects.open().filter(organization=organization, user=member_user).exists()
    invalidate.assert_called_with(member_user.id)


def test_remove_member_closes_with_removed(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    _join(organization, member_user, tier)
    _offline_subscription(tier, member_user)

    organization_service.remove_member(organization, member_user)

    (rec,) = MembershipRecord.objects.filter(organization=organization, user=member_user)
    assert rec.ended_reason == Reason.REMOVED
    assert rec.valid_until is not None
    assert not OrganizationMember.objects.filter(organization=organization, user=member_user).exists()


def test_delete_tier_reopens_open_records_without_tier(
    organization: Organization,
    tier: MembershipTier,
    django_user_model: t.Type[RevelUser],
) -> None:
    users = [
        django_user_model.objects.create_user(username=f"u{i}", email=f"u{i}@example.com", password="pass")
        for i in range(3)
    ]
    for user in users:
        _join(organization, user, tier)
    # users[2] cancels: their record is closed and must keep its tier_name.
    leaver = OrganizationMember.objects.get(user=users[2])
    leaver.status = Status.CANCELLED
    leaver.save()

    organization_service.delete_membership_tier(tier)

    for user in users[:2]:
        old, new = MembershipRecord.objects.filter(user=user).order_by("valid_from", "created_at")
        assert old.ended_reason == Reason.TIER_CHANGED
        assert old.tier_name == "Gold"
        assert new.valid_until is None
        assert new.tier_id is None
        assert new.tier_name == ""
    (closed_rec,) = MembershipRecord.objects.filter(user=users[2])
    assert closed_rec.tier_name == "Gold"
    assert closed_rec.ended_reason == Reason.CANCELLED


def test_refused_tier_deletion_leaves_register_untouched(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    _join(organization, member_user, tier)
    OrganizationMembershipRequest.objects.create(organization=organization, user=member_user, tier=tier)
    before = list(MembershipRecord.objects.filter(user=member_user).values_list("pk", "valid_until", "ended_reason"))

    with pytest.raises(MembershipTierInUseError):
        organization_service.delete_membership_tier(tier)

    after = list(MembershipRecord.objects.filter(user=member_user).values_list("pk", "valid_until", "ended_reason"))
    assert after == before
    assert after[0][1] is None


def test_account_deletion_keeps_record_open(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    member_user.first_name, member_user.last_name = "Jane", "Doe"
    member_user.save()
    _join(organization, member_user, tier)
    _offline_subscription(tier, member_user)
    record = MembershipRecord.objects.get(organization=organization)

    delete_user_account(str(member_user.id))

    record.refresh_from_db()
    assert record.valid_until is None
    assert record.ended_reason is None
    assert record.user is None
    assert record.full_name == "Jane Doe"
    assert record.email == "member@example.com"


def test_account_deletion_without_subscription_keeps_record_open(
    organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    _join(organization, member_user, tier)
    record = MembershipRecord.objects.get(organization=organization)

    delete_user_account(str(member_user.id))

    record.refresh_from_db()
    assert record.valid_until is None
    assert record.user is None


def test_admin_tier_delete_routes_through_register(organization: Organization, tier: MembershipTier) -> None:
    users = [RevelUser.objects.create_user(username="adm1", email="adm1@example.com", password="pass")]
    _join(organization, users[0], tier)
    model_admin = admin.site._registry[MembershipTier]

    model_admin.delete_model(RequestFactory().post("/"), tier)

    old, new = MembershipRecord.objects.filter(user=users[0]).order_by("valid_from", "created_at")
    assert old.ended_reason == Reason.TIER_CHANGED
    assert new.valid_until is None
    assert new.tier_id is None
    assert not MembershipTier.objects.filter(pk=tier.pk).exists()
