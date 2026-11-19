"""Blacklisting never creates a membership row for a non-member; entries do not copy a stranger's profile (ADR-0020)."""

import typing as t
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Blacklist, Organization, OrganizationMember, OrganizationStaff
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
        email_verified=True,
    )


def _row(org: Organization, user: RevelUser) -> OrganizationMember:
    return OrganizationMember.objects.get(organization=org, user=user)


def _members_url(org: Organization) -> str:
    return f"/api/organization-admin/{org.slug}/members"


def _client(user: RevelUser) -> t.Any:
    from django.test import Client
    from ninja_jwt.tokens import RefreshToken

    token = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
    return Client(headers={"Authorization": f"Bearer {token}"})


class TestBlacklistingANonMember:
    def test_quick_mode_creates_no_member_row(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            blacklist_service.add_to_blacklist(organization, organization_owner_user, user=nonmember_user)

        assert not OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()
        assert not is_known_holder(organization, nonmember_user)
        assert not has_relationship(organization, nonmember_user)

    def test_manual_mode_by_verified_email_creates_no_member_row(
        self, organization: Organization, organization_owner_user: RevelUser, profiled_user: RevelUser
    ) -> None:
        entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, email=profiled_user.email)

        assert entry.user == profiled_user
        assert not OrganizationMember.objects.filter(organization=organization, user=profiled_user).exists()
        assert not is_known_holder(organization, profiled_user)

    def test_members_list_and_update_do_not_see_the_banned_stranger(
        self, organization: Organization, organization_owner_user: RevelUser, nonmember_user: RevelUser
    ) -> None:
        blacklist_service.add_to_blacklist(organization, organization_owner_user, user=nonmember_user)
        client = _client(organization_owner_user)

        response = client.get(_members_url(organization))
        put = client.put(
            f"{_members_url(organization)}/{nonmember_user.pk}",
            data={"status": "active"},
            content_type="application/json",
        )

        assert response.status_code == 200
        assert str(nonmember_user.pk) not in [row["user"]["id"] for row in response.json()["results"]]
        assert put.status_code == 404

    def test_signup_auto_link_applies_consequences_without_a_row(
        self, organization: Organization, organization_owner_user: RevelUser, nonmember_user: RevelUser
    ) -> None:
        OrganizationStaff.objects.create(organization=organization, user=nonmember_user)
        Blacklist.objects.create(
            organization=organization, email=nonmember_user.email, created_by=organization_owner_user
        )

        assert blacklist_service.link_blacklist_entries_for_user(nonmember_user) == 1
        assert not OrganizationStaff.objects.filter(organization=organization, user=nonmember_user).exists()

        assert not OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()
        assert not is_known_holder(organization, nonmember_user)
        assert blacklist_service.check_user_hard_blacklisted(nonmember_user, organization)

    def test_a_concurrent_membership_is_banned_on_commit(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """A row created after the blacklist check but before the entry committed is still flipped."""
        with django_capture_on_commit_callbacks(execute=True):
            blacklist_service.add_to_blacklist(organization, organization_owner_user, user=nonmember_user)
            OrganizationMember.objects.create(organization=organization, user=nonmember_user)

        assert _row(organization, nonmember_user).status == Status.BANNED


class TestBanningAnExistingMember:
    def test_member_is_banned_stays_known_and_unban_cancels(
        self, organization: Organization, organization_owner_user: RevelUser, nonmember_user: RevelUser
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=nonmember_user)
        entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, user=nonmember_user)

        assert _row(organization, nonmember_user).status == Status.BANNED
        assert is_known_holder(organization, nonmember_user)
        assert has_relationship(organization, nonmember_user)

        blacklist_service.remove_from_blacklist(entry)
        assert _row(organization, nonmember_user).status == Status.CANCELLED


class TestManualModeAutoLink:
    def test_phone_does_not_link(
        self, organization: Organization, organization_owner_user: RevelUser, profiled_user: RevelUser
    ) -> None:
        entry = blacklist_service.add_to_blacklist(
            organization, organization_owner_user, phone_number=profiled_user.phone_number
        )
        assert entry.user is None

    def test_telegram_does_not_link(
        self, organization: Organization, organization_owner_user: RevelUser, profiled_user: RevelUser
    ) -> None:
        from telegram.models import TelegramUser

        TelegramUser.objects.create(telegram_id=42, telegram_username="ada_l", user=profiled_user)
        entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, telegram_username="ada_l")
        assert entry.user is None

    def test_unverified_email_does_not_link(
        self, organization: Organization, organization_owner_user: RevelUser, profiled_user: RevelUser
    ) -> None:
        RevelUser.objects.filter(pk=profiled_user.pk).update(email_verified=False)
        entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, email=profiled_user.email)
        assert entry.user is None

    def test_verified_email_links(
        self, organization: Organization, organization_owner_user: RevelUser, profiled_user: RevelUser
    ) -> None:
        entry = blacklist_service.add_to_blacklist(
            organization, organization_owner_user, email=profiled_user.email.upper()
        )
        assert entry.user == profiled_user


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


def test_blacklist_endpoints_show_the_account_name_for_an_existing_member(
    organization: Organization, organization_owner_user: RevelUser, profiled_user: RevelUser
) -> None:
    """A known holder (existing member) keeps the full display name on list and detail."""
    OrganizationMember.objects.create(organization=organization, user=profiled_user)
    entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, user=profiled_user)
    client = _client(organization_owner_user)
    base = f"/api/organization-admin/{organization.slug}/blacklist"

    listed = client.get(base)
    detail = client.get(f"{base}/{entry.pk}")

    assert listed.status_code == 200 and detail.status_code == 200
    assert [r["user_display_name"] for r in listed.json()["results"]] == [profiled_user.get_display_name()]
    assert detail.json()["user_display_name"] == profiled_user.get_display_name()


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


class TestLoggedInClaimGreeting:
    """The default greeting names the claimer only when the organization knows them through their own acts."""

    @pytest.fixture
    def claim_token(self, organization: Organization, organization_owner_user: RevelUser) -> t.Any:
        from datetime import timedelta

        from django.utils import timezone

        from events.models import Event, EventToken

        event = Event.objects.create(
            organization=organization, name="Open Night", slug="open-night", start=timezone.now() + timedelta(days=7)
        )
        return EventToken.objects.create(event=event, issuer=organization_owner_user, grants_invitation=True)

    @staticmethod
    def _message(user: RevelUser, token: t.Any) -> str:
        from events.models import EventInvitation
        from events.service.tokens import claim_invitation

        claim_invitation(user, str(token.pk))
        return EventInvitation.objects.get(event=token.event, user=user).custom_message or ""

    def test_unrelated_account_is_greeted_by_email(self, claim_token: t.Any, profiled_user: RevelUser) -> None:
        message = self._message(profiled_user, claim_token)
        assert "Ada" not in message and "Lovelace" not in message and "Countess" not in message
        assert profiled_user.email in message

    def test_member_is_greeted_by_name(
        self, organization: Organization, claim_token: t.Any, profiled_user: RevelUser
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=profiled_user)
        assert profiled_user.get_display_name() in self._message(profiled_user, claim_token)

    def test_payload_message_is_kept(self, claim_token: t.Any, profiled_user: RevelUser) -> None:
        claim_token.invitation_payload = {"custom_message": "Welcome aboard"}
        claim_token.save()
        assert self._message(profiled_user, claim_token) == "Welcome aboard"


def test_removing_an_entry_linked_after_creation_lifts_every_holder_it_banned(
    organization: Organization,
    organization_owner_user: RevelUser,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """An entry created unlinked banned B by phone, was later linked to A by email: removing it lifts both."""
    from events.service.blacklist_service import link_blacklist_entries_for_user

    b = django_user_model.objects.create_user(
        username="b", email="phone-holder@example.org", password="pass", phone_number="+4915100000042"
    )
    a = django_user_model.objects.create_user(username="a", email="late-link@example.org", password="pass")
    OrganizationMember.objects.create(organization=organization, user=a)
    OrganizationMember.objects.create(organization=organization, user=b)
    with django_capture_on_commit_callbacks(execute=True):
        entry = blacklist_service.add_to_blacklist(
            organization, organization_owner_user, phone_number=b.phone_number, email=a.email
        )
        assert entry.user is None
        link_blacklist_entries_for_user(a)
    entry.refresh_from_db()
    assert entry.user_id == a.pk
    assert _row(organization, a).status == Status.BANNED and _row(organization, b).status == Status.BANNED

    blacklist_service.remove_from_blacklist(entry)

    assert _row(organization, a).status == Status.CANCELLED
    assert _row(organization, b).status == Status.CANCELLED


class TestManualEntryReachesExistingHolders:
    """A manual entry links no account unless the email is verified, but it still bans matching staff/members."""

    @pytest.fixture
    def holder(self, django_user_model: type[RevelUser]) -> RevelUser:
        from telegram.models import TelegramUser

        user = django_user_model.objects.create_user(
            username="holder",
            email="holder@example.com",
            password="pass",
            phone_number="+4915198765432",
            email_verified=False,
        )
        TelegramUser.objects.create(telegram_id=77, telegram_username="Holder_TG", user=user)
        return user

    def test_phone_entry_removes_staff_and_stays_unlinked(
        self, organization: Organization, organization_owner_user: RevelUser, holder: RevelUser
    ) -> None:
        OrganizationStaff.objects.create(organization=organization, user=holder)

        entry = blacklist_service.add_to_blacklist(
            organization, organization_owner_user, phone_number=holder.phone_number
        )

        assert entry.user is None
        assert not OrganizationStaff.objects.filter(organization=organization, user=holder).exists()
        assert holder not in organization.staff_members.all()

    def test_telegram_entry_bans_active_member_and_cancels_subscriptions(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        holder: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=holder)

        with patch("events.service.subscription.lifecycle.cancel_subscriptions_for_membership_loss") as cancel:
            with django_capture_on_commit_callbacks(execute=True):
                entry = blacklist_service.add_to_blacklist(
                    organization, organization_owner_user, telegram_username="@holder_tg"
                )

        assert entry.user is None
        assert _row(organization, holder).status == Status.BANNED
        cancel.assert_called_once()
        assert cancel.call_args.args[0] == holder

    def test_unverified_email_entry_removes_staff(
        self, organization: Organization, organization_owner_user: RevelUser, holder: RevelUser
    ) -> None:
        OrganizationStaff.objects.create(organization=organization, user=holder)

        entry = blacklist_service.add_to_blacklist(organization, organization_owner_user, email=holder.email.upper())

        assert entry.user is None
        assert not OrganizationStaff.objects.filter(organization=organization, user=holder).exists()

    def test_stranger_gets_nothing_created(
        self, organization: Organization, organization_owner_user: RevelUser, holder: RevelUser
    ) -> None:
        blacklist_service.add_to_blacklist(organization, organization_owner_user, phone_number=holder.phone_number)

        assert not OrganizationMember.objects.filter(organization=organization, user=holder).exists()
        assert not OrganizationStaff.objects.filter(organization=organization, user=holder).exists()

    def test_owner_is_exempt(
        self, organization: Organization, organization_owner_user: RevelUser, django_user_model: type[RevelUser]
    ) -> None:
        RevelUser.objects.filter(pk=organization_owner_user.pk).update(phone_number="+4915100000001")
        OrganizationMember.objects.create(organization=organization, user=organization_owner_user)
        staffer = django_user_model.objects.create_user(username="s2", email="s2@example.com", password="pass")

        blacklist_service.add_to_blacklist(organization, staffer, phone_number="+4915100000001")

        assert _row(organization, organization_owner_user).status != Status.BANNED
        assert organization in Organization.objects.for_user(organization_owner_user)

    def test_multiple_accounts_matching_different_identifiers(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        holder: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        other = django_user_model.objects.create_user(
            username="other", email="other@example.com", password="pass", phone_number="+4915111111111"
        )
        OrganizationStaff.objects.create(organization=organization, user=other)
        OrganizationMember.objects.create(organization=organization, user=holder)

        # One entry carrying a phone (matches ``other``) and an email (matches ``holder``)
        blacklist_service.add_to_blacklist(
            organization, organization_owner_user, phone_number="+4915111111111", email=holder.email
        )

        assert not OrganizationStaff.objects.filter(organization=organization, user=other).exists()
        assert _row(organization, holder).status == Status.BANNED


class TestRemovingAnUnlinkedEntry:
    """Lifting an unlinked entry restores access to the holders it banned, unless another entry still covers them."""

    @pytest.fixture
    def holder(self, django_user_model: type[RevelUser]) -> RevelUser:
        return django_user_model.objects.create_user(
            username="holder",
            email="holder@example.com",
            password="pass",
            phone_number="+4915198765432",
            email_verified=False,
        )

    @pytest.fixture
    def banned_by_phone(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        holder: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> Blacklist:
        OrganizationMember.objects.create(organization=organization, user=holder)
        with django_capture_on_commit_callbacks(execute=True):
            entry = blacklist_service.add_to_blacklist(
                organization, organization_owner_user, phone_number=holder.phone_number
            )
        assert entry.user is None
        assert _row(organization, holder).status == Status.BANNED
        return entry

    def test_unban_lifts_the_ban_of_a_matched_holder(
        self, organization: Organization, holder: RevelUser, banned_by_phone: Blacklist
    ) -> None:
        blacklist_service.remove_from_blacklist(banned_by_phone)

        assert _row(organization, holder).status == Status.CANCELLED

    def test_a_second_unlinked_entry_keeps_the_ban_until_it_is_removed_too(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        holder: RevelUser,
        banned_by_phone: Blacklist,
    ) -> None:
        second = blacklist_service.add_to_blacklist(organization, organization_owner_user, email=holder.email)

        blacklist_service.remove_from_blacklist(banned_by_phone)
        assert _row(organization, holder).status == Status.BANNED

        blacklist_service.remove_from_blacklist(second)
        assert _row(organization, holder).status == Status.CANCELLED

    def test_a_linked_entry_keeps_the_ban(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        holder: RevelUser,
        banned_by_phone: Blacklist,
    ) -> None:
        RevelUser.objects.filter(pk=holder.pk).update(email_verified=True)
        linked = blacklist_service.add_to_blacklist(organization, organization_owner_user, email=holder.email)
        assert linked.user_id == holder.pk

        blacklist_service.remove_from_blacklist(banned_by_phone)
        assert _row(organization, holder).status == Status.BANNED

        blacklist_service.remove_from_blacklist(linked)
        assert _row(organization, holder).status == Status.CANCELLED


class TestMembershipSavedAroundABlacklistCommit:
    def test_membership_saved_before_the_entry_committed_is_banned_on_commit(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """The row is saved, then an entry for the account lands before the row's commit: the row ends banned."""
        with django_capture_on_commit_callbacks(execute=True):
            OrganizationMember.objects.create(organization=organization, user=nonmember_user)
            Blacklist.objects.create(
                organization=organization, email=nonmember_user.email, created_by=organization_owner_user
            )

        assert _row(organization, nonmember_user).status == Status.BANNED

    def test_subscription_created_in_the_race_is_cancelled_and_staff_removed(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        from decimal import Decimal

        from events.models import MembershipSubscription, MembershipTier
        from events.service.subscription import lifecycle as subscription_lifecycle
        from events.service.subscription import plans as subscription_plans

        tier = MembershipTier.objects.get(organization=organization, name="General membership")
        plan = subscription_plans.create_plan(
            tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
        )
        OrganizationStaff.objects.create(organization=organization, user=nonmember_user)

        with django_capture_on_commit_callbacks(execute=True):
            OrganizationMember.objects.create(organization=organization, user=nonmember_user)
            sub = subscription_lifecycle.create_subscription(plan, nonmember_user)
            Blacklist.objects.create(
                organization=organization, email=nonmember_user.email, created_by=organization_owner_user
            )

        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert _row(organization, nonmember_user).status == Status.BANNED
        assert not OrganizationStaff.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_non_blacklisted_save_stays_active(
        self,
        organization: Organization,
        nonmember_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            OrganizationMember.objects.create(organization=organization, user=nonmember_user)

        assert _row(organization, nonmember_user).status == Status.ACTIVE

    def test_owner_is_never_banned_by_the_recheck(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            OrganizationMember.objects.create(organization=organization, user=organization_owner_user)
            Blacklist.objects.create(
                organization=organization, email=organization_owner_user.email, created_by=organization_owner_user
            )

        assert _row(organization, organization_owner_user).status == Status.ACTIVE
