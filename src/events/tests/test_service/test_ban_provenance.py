"""A ban-created access row confers no standing; blacklist entries do not copy a stranger's profile (ADR-0020)."""

import typing as t
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Blacklist, Organization, OrganizationMember
from events.schema import BlacklistEntrySchema
from events.service import blacklist_service
from events.service.holder_visibility import is_known_holder
from events.service.organization_service import has_relationship

pytestmark = pytest.mark.django_db

Status = OrganizationMember.MembershipStatus


@pytest.fixture
def profiled_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="profiled",
        email="profiled@example.com",
        password="pass",
        first_name="Ada",
        last_name="Lovelace",
        preferred_name="Countess",
        phone_number="+4915112345678",
    )


def _row(org: Organization, user: RevelUser) -> OrganizationMember:
    return OrganizationMember.objects.get(organization=org, user=user)


class TestBanCreatedRow:
    def test_ban_of_a_stranger_confers_no_standing(
        self, organization: Organization, organization_owner_user: RevelUser, nonmember_user: RevelUser
    ) -> None:
        blacklist_service.add_to_blacklist(organization, organization_owner_user, user=nonmember_user)

        assert _row(organization, nonmember_user).status == Status.BANNED
        assert _row(organization, nonmember_user).created_by_ban is True
        assert not is_known_holder(organization, nonmember_user)
        assert not has_relationship(organization, nonmember_user)

    def test_unban_removes_the_row_it_created(
        self, organization: Organization, organization_owner_user: RevelUser, nonmember_user: RevelUser
    ) -> None:
        entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, user=nonmember_user)

        blacklist_service.remove_from_blacklist(entry)

        assert not OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_banning_an_existing_member_keeps_standing(
        self, organization: Organization, organization_owner_user: RevelUser, nonmember_user: RevelUser
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=nonmember_user)
        entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, user=nonmember_user)

        assert _row(organization, nonmember_user).status == Status.BANNED
        assert _row(organization, nonmember_user).created_by_ban is False
        assert is_known_holder(organization, nonmember_user)
        assert has_relationship(organization, nonmember_user)

        blacklist_service.remove_from_blacklist(entry)
        assert _row(organization, nonmember_user).status == Status.CANCELLED

    def test_signup_auto_link_creates_no_standing(
        self, organization: Organization, organization_owner_user: RevelUser, nonmember_user: RevelUser
    ) -> None:
        Blacklist.objects.filter(organization=organization).delete()
        Blacklist.objects.create(
            organization=organization, email=nonmember_user.email, created_by=organization_owner_user
        )

        assert blacklist_service.link_blacklist_entries_for_user(nonmember_user) == 1

        assert _row(organization, nonmember_user).created_by_ban is True
        assert not is_known_holder(organization, nonmember_user)
        assert not has_relationship(organization, nonmember_user)


class TestBlacklistEntryProfile:
    def test_quick_mode_on_a_stranger_stores_email_only(
        self, organization: Organization, organization_owner_user: RevelUser, profiled_user: RevelUser
    ) -> None:
        entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, user=profiled_user)
        out = BlacklistEntrySchema.from_orm(entry)
        dumped = out.model_dump()

        assert entry.email == profiled_user.email
        assert (entry.phone_number, entry.first_name, entry.last_name, entry.preferred_name) == (None,) * 4
        assert [dumped[k] for k in ("phone_number", "first_name", "last_name", "preferred_name")] == [None] * 4
        assert out.user_display_name == profiled_user.email
        assert "Ada" not in str(out.model_dump()) and "Lovelace" not in str(out.model_dump())

    def test_manual_mode_auto_link_does_not_expose_the_account_name(
        self, organization: Organization, organization_owner_user: RevelUser, profiled_user: RevelUser
    ) -> None:
        entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, email=profiled_user.email)
        out = BlacklistEntrySchema.from_orm(entry)

        assert entry.user == profiled_user
        assert out.user_display_name == profiled_user.email
        assert "Ada" not in str(out.model_dump())

    def test_manual_mode_shows_the_name_staff_typed(
        self, organization: Organization, organization_owner_user: RevelUser, profiled_user: RevelUser
    ) -> None:
        entry = blacklist_service.add_to_blacklist(
            organization, organization_owner_user, email=profiled_user.email, first_name="Al", last_name="B"
        )
        assert BlacklistEntrySchema.from_orm(entry).user_display_name == "Al B"

    def test_known_holder_keeps_the_full_profile(
        self, organization: Organization, organization_owner_user: RevelUser, profiled_user: RevelUser
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=profiled_user)
        entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, user=profiled_user)
        out = BlacklistEntrySchema.from_orm(entry)

        assert (entry.first_name, entry.last_name, entry.phone_number) == ("Ada", "Lovelace", "+4915112345678")
        assert out.user_display_name == profiled_user.get_display_name()


def test_unban_of_a_ban_created_row_sends_no_removal_notice(
    organization: Organization,
    organization_owner_user: RevelUser,
    nonmember_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, user=nonmember_user)
    with patch("events.signals.notification_requested") as signal:
        with django_capture_on_commit_callbacks(execute=True):
            blacklist_service.remove_from_blacklist(entry)
    signal.send.assert_not_called()


def test_blacklist_endpoints_show_staff_supplied_identity_for_a_stranger(
    organization: Organization, organization_owner_user: RevelUser, profiled_user: RevelUser
) -> None:
    from django.test import Client
    from ninja_jwt.tokens import RefreshToken

    blacklist_service.add_to_blacklist(organization, organization_owner_user, email=profiled_user.email)
    token = str(RefreshToken.for_user(organization_owner_user).access_token)  # type: ignore[attr-defined]
    client = Client(headers={"Authorization": f"Bearer {token}"})

    body = client.get(f"/api/organization-admin/{organization.slug}/blacklist").json()

    assert [r["user_display_name"] for r in body["results"]] == [profiled_user.email]
    assert "Lovelace" not in str(body)


def test_guest_link_claim_greets_by_email_not_stored_name(
    organization: Organization, organization_owner_user: RevelUser, django_user_model: type[RevelUser]
) -> None:
    """An anonymous request naming an existing guest's email must not learn the stored name from the invitation."""
    from datetime import timedelta

    from django.utils import timezone

    from events.models import Event, EventInvitation, EventToken
    from events.service import guest as guest_service

    event = Event.objects.create(
        organization=organization, name="Open Night", slug="open-night", start=timezone.now() + timedelta(days=7)
    )
    token = EventToken.objects.create(event=event, issuer=organization_owner_user, grants_invitation=True)
    guest = django_user_model.objects.create_user(
        username="g@example.com",
        email="g@example.com",
        password="x",
        guest=True,
        first_name="Grace",
        last_name="Hopper",
    )

    guest_service.claim_invitation_link(guest, event, token)

    message = EventInvitation.objects.get(event=event, user=guest).custom_message or ""
    assert "Grace" not in message and "Hopper" not in message
    assert "g@example.com" in message
