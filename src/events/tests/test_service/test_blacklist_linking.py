"""Blacklist duplicate identifiers (#1554) and one linked entry per organization (#1553)."""

import datetime
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.email_backend import erased_placeholder_email
from events.models import Blacklist, Organization, OrganizationMember, OrganizationStaff
from events.service import blacklist_service

pytestmark = pytest.mark.django_db


class TestManualModeDuplicateIdentifiers:
    """Manual entries refuse a repeated email, telegram handle or phone number (#1554)."""

    def test_duplicate_telegram_unlinked_raises_400(
        self, blacklist_org: Organization, blacklist_admin: RevelUser
    ) -> None:
        """A normalized telegram handle that an unlinked entry holds gives a 400, not an IntegrityError."""
        Blacklist.objects.create(organization=blacklist_org, telegram_username="foo", created_by=blacklist_admin)

        with pytest.raises(HttpError) as exc_info:
            blacklist_service.add_to_blacklist(
                organization=blacklist_org, created_by=blacklist_admin, telegram_username="@Foo"
            )

        assert exc_info.value.status_code == 400
        assert "Telegram username already exists" in str(exc_info.value.message)
        assert Blacklist.objects.filter(organization=blacklist_org).count() == 1

    def test_duplicate_phone_unlinked_raises_400(self, blacklist_org: Organization, blacklist_admin: RevelUser) -> None:
        """A phone number that an unlinked entry holds gives a 400."""
        Blacklist.objects.create(organization=blacklist_org, phone_number="+393331234567", created_by=blacklist_admin)

        with pytest.raises(HttpError) as exc_info:
            blacklist_service.add_to_blacklist(
                organization=blacklist_org, created_by=blacklist_admin, phone_number="+39 333 1234567"
            )

        assert exc_info.value.status_code == 400
        assert "phone number already exists" in str(exc_info.value.message)
        assert Blacklist.objects.filter(organization=blacklist_org).count() == 1

    def test_duplicate_telegram_and_phone_on_linked_entry_raise_400(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, target_user: RevelUser
    ) -> None:
        """A linked entry (for example of an erased account) still holds its identifiers."""
        Blacklist.objects.create(
            organization=blacklist_org,
            user=target_user,
            telegram_username="linkedtg",
            phone_number="+393331234567",
            created_by=blacklist_admin,
        )

        with pytest.raises(HttpError) as tg_exc:
            blacklist_service.add_to_blacklist(
                organization=blacklist_org, created_by=blacklist_admin, telegram_username="LinkedTG"
            )
        with pytest.raises(HttpError) as phone_exc:
            blacklist_service.add_to_blacklist(
                organization=blacklist_org, created_by=blacklist_admin, phone_number="+393331234567"
            )

        assert tg_exc.value.status_code == 400
        assert phone_exc.value.status_code == 400
        assert Blacklist.objects.filter(organization=blacklist_org).count() == 1


class TestAddWhenAccountResolved:
    """A resolved account is refused when any entry already covers it (#1553)."""

    def test_quick_mode_links_existing_unlinked_email_entry(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, target_user: RevelUser
    ) -> None:
        """An unlinked entry with the account's email is linked to the account instead of a second row."""
        entry = Blacklist.objects.create(
            organization=blacklist_org, email=target_user.email, created_by=blacklist_admin
        )
        member = OrganizationMember.objects.create(organization=blacklist_org, user=target_user)

        result = blacklist_service.add_to_blacklist(
            organization=blacklist_org, created_by=blacklist_admin, user=target_user
        )

        entry.refresh_from_db()
        member.refresh_from_db()
        assert result.pk == entry.pk
        assert entry.user == target_user
        assert Blacklist.objects.filter(organization=blacklist_org).count() == 1
        assert member.status == OrganizationMember.MembershipStatus.BANNED

    def test_quick_mode_link_removes_staff_role(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, target_user: RevelUser
    ) -> None:
        """Linking an existing entry applies the consequences, which remove the staff role."""
        Blacklist.objects.create(organization=blacklist_org, email=target_user.email, created_by=blacklist_admin)
        OrganizationStaff.objects.create(organization=blacklist_org, user=target_user)

        blacklist_service.add_to_blacklist(organization=blacklist_org, created_by=blacklist_admin, user=target_user)

        assert not OrganizationStaff.objects.filter(organization=blacklist_org, user=target_user).exists()

    def test_quick_mode_refused_when_account_already_has_entry(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, target_user: RevelUser
    ) -> None:
        """An entry linked to the account itself is refused."""
        Blacklist.objects.create(organization=blacklist_org, user=target_user, created_by=blacklist_admin)

        with pytest.raises(HttpError) as exc_info:
            blacklist_service.add_to_blacklist(organization=blacklist_org, created_by=blacklist_admin, user=target_user)

        assert exc_info.value.status_code == 400
        assert "already blacklisted" in str(exc_info.value.message)

    def test_quick_mode_phone_held_by_unlinked_entry_of_stranger_does_not_refuse(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, target_user: RevelUser
    ) -> None:
        """A phone-only entry must not decide the outcome: the phone is a hidden field of a stranger (S1)."""
        Blacklist.objects.create(
            organization=blacklist_org, phone_number=target_user.phone_number, created_by=blacklist_admin
        )

        result = blacklist_service.add_to_blacklist(
            organization=blacklist_org, created_by=blacklist_admin, user=target_user
        )

        assert result.user == target_user
        assert Blacklist.objects.filter(organization=blacklist_org).count() == 2

    def test_quick_mode_phone_held_by_unlinked_entry_of_known_holder_does_not_refuse(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, target_user: RevelUser
    ) -> None:
        """A known holder (an org member) with a covered phone still gets a new linked entry (G2)."""
        OrganizationMember.objects.create(organization=blacklist_org, user=target_user)
        Blacklist.objects.create(
            organization=blacklist_org, phone_number=target_user.phone_number, created_by=blacklist_admin
        )

        result = blacklist_service.add_to_blacklist(
            organization=blacklist_org, created_by=blacklist_admin, user=target_user
        )

        assert result.user == target_user
        assert Blacklist.objects.filter(organization=blacklist_org, user=target_user).count() == 1
        assert Blacklist.objects.filter(organization=blacklist_org).count() == 2

    def test_manual_email_refused_when_linked_entry_of_other_account_holds_email(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        """An erased account's linked entry keeps the email; a new verified holder must not get a second entry."""
        erased = django_user_model.objects.create_user(username="erased", email=erased_placeholder_email("x"))
        Blacklist.objects.create(
            organization=blacklist_org, user=erased, email="held@example.com", created_by=blacklist_admin
        )
        new_holder = django_user_model.objects.create_user(username="holder", email="held@example.com")
        RevelUser.objects.filter(pk=new_holder.pk).update(email_verified=True)

        with pytest.raises(HttpError) as exc_info:
            blacklist_service.add_to_blacklist(
                organization=blacklist_org, created_by=blacklist_admin, email="held@example.com"
            )

        assert exc_info.value.status_code == 400
        assert "already blacklisted" in str(exc_info.value.message)
        assert Blacklist.objects.filter(organization=blacklist_org).count() == 1
        assert not Blacklist.objects.filter(organization=blacklist_org, user=new_holder).exists()

    def test_manual_email_refused_when_account_has_linked_entry_without_email(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        """The account's own entry blocks a manual add by its verified email, even when the entry has no email."""
        holder = django_user_model.objects.create_user(username="holder", email="holder@example.com")
        RevelUser.objects.filter(pk=holder.pk).update(email_verified=True)
        Blacklist.objects.create(organization=blacklist_org, user=holder, created_by=blacklist_admin)

        with pytest.raises(HttpError) as exc_info:
            blacklist_service.add_to_blacklist(
                organization=blacklist_org, created_by=blacklist_admin, email="holder@example.com"
            )

        assert exc_info.value.status_code == 400
        assert "already blacklisted" in str(exc_info.value.message)
        assert Blacklist.objects.filter(organization=blacklist_org).count() == 1

    def test_manual_email_links_existing_unlinked_entry(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        """A manual add by the verified email of an account links the entry that already holds the email."""
        holder = django_user_model.objects.create_user(username="holder", email="holder@example.com")
        RevelUser.objects.filter(pk=holder.pk).update(email_verified=True)
        entry = Blacklist.objects.create(
            organization=blacklist_org, email="holder@example.com", reason="first", created_by=blacklist_admin
        )

        result = blacklist_service.add_to_blacklist(
            organization=blacklist_org, created_by=blacklist_admin, email="Holder@Example.com", reason="second"
        )

        entry.refresh_from_db()
        assert result.pk == entry.pk
        assert entry.user == holder
        assert entry.reason == "first"
        assert Blacklist.objects.filter(organization=blacklist_org).count() == 1

    def test_manual_linked_entry_refused_when_typed_telegram_is_held_by_another_entry(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        """A manual entry linked by verified email still must not repeat another entry's Telegram handle."""
        Blacklist.objects.create(organization=blacklist_org, telegram_username="foo", created_by=blacklist_admin)
        holder = django_user_model.objects.create_user(username="holder", email="holder@example.com")
        RevelUser.objects.filter(pk=holder.pk).update(email_verified=True)

        with pytest.raises(HttpError) as exc_info:
            blacklist_service.add_to_blacklist(
                organization=blacklist_org,
                created_by=blacklist_admin,
                email="holder@example.com",
                telegram_username="@Foo",
            )

        assert exc_info.value.status_code == 400
        assert "Telegram username" in str(exc_info.value.message)
        assert Blacklist.objects.filter(organization=blacklist_org).count() == 1


class TestLinkOneEntryPerOrganization:
    """Linking never links two entries of one organization to one user (#1553)."""

    def test_registration_with_two_matching_entries_links_one(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, django_user_model: type[RevelUser]
    ) -> None:
        """Email entry and phone entry both match a new user: no IntegrityError, the earliest created_at is linked."""
        by_email = Blacklist.objects.create(
            organization=blacklist_org, email="new@example.com", created_by=blacklist_admin
        )
        by_phone = Blacklist.objects.create(
            organization=blacklist_org, phone_number="+393331112222", created_by=blacklist_admin
        )
        # The oldest created_at wins, not the insertion order: make the phone entry the older one.
        Blacklist.objects.filter(pk=by_phone.pk).update(created_at=timezone.now() - datetime.timedelta(days=1))

        user = django_user_model.objects.create_user(
            username="newbie", email="new@example.com", phone_number="+393331112222"
        )

        by_email.refresh_from_db()
        by_phone.refresh_from_db()
        assert by_phone.user == user
        assert by_email.user is None
        assert Blacklist.objects.filter(organization=blacklist_org, user=user).count() == 1

    def test_registration_applies_consequences(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, django_user_model: type[RevelUser]
    ) -> None:
        """The org with two matching entries still bans the new user's existing membership."""
        Blacklist.objects.create(organization=blacklist_org, email="new@example.com", created_by=blacklist_admin)
        Blacklist.objects.create(organization=blacklist_org, phone_number="+393331112222", created_by=blacklist_admin)
        user = django_user_model.objects.create_user(
            username="newbie", email="new@example.com", phone_number="+393331112222"
        )
        member = OrganizationMember.objects.create(organization=blacklist_org, user=user)

        # Re-run the linking: the user is already linked in the org, nothing more links, consequences still apply.
        assert blacklist_service.link_blacklist_entries_for_user(user) == 0

        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.BANNED

    def test_already_linked_user_links_nothing_in_that_org_but_links_other_org(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, target_user: RevelUser
    ) -> None:
        """Telegram connect: org A already has a linked entry, so its unlinked match stays; org B's is linked."""
        org_b = Organization.objects.create(name="Org B", slug="org-b", owner=blacklist_admin)
        Blacklist.objects.create(organization=blacklist_org, user=target_user, created_by=blacklist_admin)
        unlinked_a = Blacklist.objects.create(
            organization=blacklist_org, telegram_username="tguser", created_by=blacklist_admin
        )
        unlinked_b = Blacklist.objects.create(
            organization=org_b, telegram_username="tguser", created_by=blacklist_admin
        )
        member_a = OrganizationMember.objects.create(organization=blacklist_org, user=target_user)

        linked = blacklist_service.link_blacklist_entries_by_telegram(target_user, "@TGUser")

        unlinked_a.refresh_from_db()
        unlinked_b.refresh_from_db()
        member_a.refresh_from_db()
        assert linked == 1
        assert unlinked_a.user is None
        assert unlinked_b.user == target_user
        assert member_a.status == OrganizationMember.MembershipStatus.BANNED


class TestLinkOnEmailVerification:
    """Verifying an email links matching blacklist entries (#1553)."""

    def test_verification_links_entry_and_applies_consequences(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, django_user_model: type[RevelUser]
    ) -> None:
        """An entry created while the email was unverified links when the account verifies."""
        user = django_user_model.objects.create_user(username="late", email="late@example.com")
        RevelUser.objects.filter(pk=user.pk).update(email_verified=False)
        user.refresh_from_db()
        member = OrganizationMember.objects.create(organization=blacklist_org, user=user)
        entry = Blacklist.objects.create(
            organization=blacklist_org, email="late@example.com", created_by=blacklist_admin
        )
        assert entry.user_id is None

        user.email_verified = True
        user.save(update_fields=["email_verified"])

        entry.refresh_from_db()
        member.refresh_from_db()
        assert entry.user == user
        assert member.status == OrganizationMember.MembershipStatus.BANNED


class TestLinkTriggers:
    """The post_save receiver links on guest conversion and email change, not on an unrelated save (T1)."""

    def _verified_user(self, django_user_model: type[RevelUser], email: str, *, guest: bool = False) -> RevelUser:
        user = django_user_model.objects.create_user(username=email, email=email)
        RevelUser.objects.filter(pk=user.pk).update(email_verified=True, guest=guest)
        user.refresh_from_db()
        return user

    def test_guest_conversion_links_matching_entry(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, django_user_model: type[RevelUser]
    ) -> None:
        """A verified guest that becomes a full account gets its matching entry linked."""
        user = self._verified_user(django_user_model, "guest@example.com", guest=True)
        entry = Blacklist.objects.create(
            organization=blacklist_org, email="guest@example.com", created_by=blacklist_admin
        )
        assert entry.user_id is None

        user.guest = False
        user.save(update_fields=["guest"])

        entry.refresh_from_db()
        assert entry.user == user

    def test_verified_email_change_links_matching_entry(
        self, blacklist_org: Organization, blacklist_admin: RevelUser, django_user_model: type[RevelUser]
    ) -> None:
        """A verified user that changes to a listed address gets the entry linked."""
        user = self._verified_user(django_user_model, "before@example.com")
        entry = Blacklist.objects.create(
            organization=blacklist_org, email="after@example.com", created_by=blacklist_admin
        )

        user.email = "after@example.com"
        user.save(update_fields=["email"])

        entry.refresh_from_db()
        assert entry.user == user

    def test_unrelated_save_does_not_link(self, django_user_model: type[RevelUser]) -> None:
        """Saving a field that is not link-relevant never calls the linker."""
        user = self._verified_user(django_user_model, "plain@example.com")

        with patch("events.signals.user.link_blacklist_entries_for_user", new_callable=MagicMock) as linker:
            user.first_name = "Changed"
            user.save(update_fields=["first_name"])

        linker.assert_not_called()


class TestLinkOrganizationsMatchedByOtherAccountEntry:
    """Consequences apply in an organization whose only match is an entry linked to another account (G4)."""

    def test_verification_bans_member_when_only_match_is_erased_accounts_entry(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        """Nothing can be linked, but the member that holds the listed email must still be banned."""
        erased = django_user_model.objects.create_user(username="erased", email=erased_placeholder_email("x"))
        Blacklist.objects.create(
            organization=blacklist_org, user=erased, email="held@example.com", created_by=blacklist_admin
        )
        user = django_user_model.objects.create_user(username="holder", email="held@example.com")
        RevelUser.objects.filter(pk=user.pk).update(email_verified=False)
        user.refresh_from_db()
        member = OrganizationMember.objects.create(
            organization=blacklist_org, user=user, status=OrganizationMember.MembershipStatus.ACTIVE
        )
        assert not Blacklist.objects.filter(organization=blacklist_org, user=user).exists()

        user.email_verified = True
        user.save(update_fields=["email_verified"])

        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.BANNED
        assert not Blacklist.objects.filter(organization=blacklist_org, user=user).exists()
