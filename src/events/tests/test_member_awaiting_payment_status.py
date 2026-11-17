"""AWAITING_PAYMENT member status: a held member who has not paid yet gets no member benefit."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, MembershipRecord, MembershipTier, Organization, OrganizationMember
from events.service import follow_service, permission_snapshot
from events.service.member_register_service import mirror_member

pytestmark = pytest.mark.django_db

S = OrganizationMember.MembershipStatus


@pytest.fixture
def awaiting(organization: Organization, member_user: RevelUser) -> OrganizationMember:
    """``member_user`` as an AWAITING_PAYMENT member of ``organization``."""
    return OrganizationMember.objects.create(organization=organization, user=member_user, status=S.AWAITING_PAYMENT)


def test_awaiting_member_not_in_permission_snapshot(awaiting: OrganizationMember, member_user: RevelUser) -> None:
    payload = permission_snapshot.get_my_permissions_payload(member_user)
    assert payload["memberships"] == {}


def test_awaiting_member_cannot_see_members_only_event(
    awaiting: OrganizationMember, member_user: RevelUser, members_only_event: Event
) -> None:
    assert members_only_event not in Event.objects.for_user(member_user)
    OrganizationMember.objects.filter(pk=awaiting.pk).update(status=S.ACTIVE)
    assert members_only_event in Event.objects.for_user(member_user)


def test_awaiting_member_has_no_card(awaiting: OrganizationMember, member_user: RevelUser) -> None:
    client = Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(member_user).access_token}")  # type: ignore[attr-defined]
    url = reverse("api:me_membership_pdf", kwargs={"slug": awaiting.organization.slug})
    assert client.get(url).status_code == 404


def test_awaiting_member_scan_rejected(
    awaiting: OrganizationMember, organization_owner_user: RevelUser, event: Event
) -> None:
    organization_owner_client = Client(
        HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(organization_owner_user).access_token}"  # type: ignore[attr-defined]
    )
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save(update_fields=["check_in_starts_at", "check_in_ends_at"])
    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "code": awaiting.qr_payload})
    assert organization_owner_client.post(url, content_type="application/json").status_code == 404


def test_awaiting_member_opens_no_register_entry(awaiting: OrganizationMember) -> None:
    mirror_member(awaiting)
    assert not MembershipRecord.objects.filter(organization=awaiting.organization, user=awaiting.user).exists()


def test_awaiting_revert_closes_open_entry_as_cancelled(organization: Organization, member_user: RevelUser) -> None:
    member = OrganizationMember.objects.create(organization=organization, user=member_user, status=S.ACTIVE)
    assert MembershipRecord.objects.filter(user=member_user, valid_until__isnull=True).exists()
    member.status = S.AWAITING_PAYMENT
    member.save(update_fields=["status"])
    record = MembershipRecord.objects.get(user=member_user)
    assert record.valid_until is not None
    assert record.ended_reason == MembershipRecord.EndedReason.CANCELLED


def test_awaiting_member_not_a_card_updated_recipient(awaiting: OrganizationMember, organization: Organization) -> None:
    """A tier change on an awaiting member sends no MEMBERSHIP_CARD_UPDATED (there is no card)."""
    tier = MembershipTier.objects.create(organization=organization, name="Gold")
    with (
        patch("events.signals.notification_requested.send") as send,
        patch("events.signals.transaction.on_commit", side_effect=lambda fn: fn()),
    ):
        awaiting.tier = tier
        awaiting.save(update_fields=["tier"])
    send.assert_not_called()


def test_awaiting_member_still_gets_follower_notifications(
    awaiting: OrganizationMember, organization: Organization
) -> None:
    """Followers' dedupe set excludes visible members and banned ones only; awaiting members are not excluded."""
    from events.models import OrganizationFollow

    OrganizationFollow.objects.create(
        organization=organization, user=awaiting.user, notify_new_events=True, is_archived=False
    )
    notified = {user.id for user, _ in follow_service.get_followers_for_new_event_notification(organization, None)}
    assert awaiting.user_id in notified
