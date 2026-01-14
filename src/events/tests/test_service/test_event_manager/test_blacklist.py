"""Tests for blacklist gate in the eligibility chain."""

import pytest

from accounts.models import RevelUser
from events.models import (
    Blacklist,
    Event,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    WhitelistRequest,
)
from events.service.event_manager import EligibilityService, NextStep, Reasons

pytestmark = pytest.mark.django_db


class TestBlacklistGate:
    """Tests for BlacklistGate in the eligibility chain.

    The BlacklistGate handles three scenarios:
    1. Hard blacklist - user is definitively blocked (no recourse)
    2. Fuzzy match - user's name matches blacklist entry (requires whitelist)
    3. Whitelisted - user was verified despite fuzzy match (allowed)
    """

    def test_hard_blacklisted_user_denied(
        self,
        public_user: RevelUser,
        public_event: Event,
        organization: Organization,
    ) -> None:
        """Hard-blacklisted user should be denied with no next step."""
        # Hard blacklist the user (FK match)
        Blacklist.objects.create(
            organization=organization,
            user=public_user,
            created_by=organization.owner,
        )

        handler = EligibilityService(user=public_user, event=public_event)
        eligibility = handler.check_eligibility()

        assert eligibility.allowed is False
        assert eligibility.reason == Reasons.BLACKLISTED
        assert eligibility.next_step is None  # No recourse

    def test_hard_blacklisted_by_email_denied(
        self,
        public_user: RevelUser,
        public_event: Event,
        organization: Organization,
    ) -> None:
        """User matching blacklist entry by email should be denied."""
        # Blacklist by email (no user FK)
        Blacklist.objects.create(
            organization=organization,
            email=public_user.email,
            created_by=organization.owner,
        )

        handler = EligibilityService(user=public_user, event=public_event)
        eligibility = handler.check_eligibility()

        assert eligibility.allowed is False
        assert eligibility.reason == Reasons.BLACKLISTED

    def test_fuzzy_match_prompts_whitelist_request(
        self,
        public_user: RevelUser,
        public_event: Event,
        organization: Organization,
    ) -> None:
        """User with fuzzy name match should be prompted to request whitelist."""
        # Set user's name
        public_user.first_name = "John"
        public_user.last_name = "Smith"
        public_user.save()

        # Create blacklist entry with similar name (no user FK, no hard identifiers)
        Blacklist.objects.create(
            organization=organization,
            first_name="John",
            last_name="Smith",  # Exact match for testing
            created_by=organization.owner,
        )

        handler = EligibilityService(user=public_user, event=public_event)
        eligibility = handler.check_eligibility()

        assert eligibility.allowed is False
        assert eligibility.reason == Reasons.VERIFICATION_REQUIRED
        assert eligibility.next_step == NextStep.REQUEST_WHITELIST

    def test_pending_whitelist_request_shows_waiting(
        self,
        public_user: RevelUser,
        public_event: Event,
        organization: Organization,
    ) -> None:
        """User with pending whitelist request should see waiting status."""
        public_user.first_name = "John"
        public_user.last_name = "Smith"
        public_user.save()

        blacklist_entry = Blacklist.objects.create(
            organization=organization,
            first_name="John",
            last_name="Smith",
            created_by=organization.owner,
        )

        # Create pending whitelist request
        request = WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.PENDING,
        )
        request.matched_blacklist_entries.add(blacklist_entry)

        handler = EligibilityService(user=public_user, event=public_event)
        eligibility = handler.check_eligibility()

        assert eligibility.allowed is False
        assert eligibility.reason == Reasons.WHITELIST_PENDING
        assert eligibility.next_step == NextStep.WAIT_FOR_WHITELIST_APPROVAL

    def test_rejected_whitelist_request_blocked(
        self,
        public_user: RevelUser,
        public_event: Event,
        organization: Organization,
    ) -> None:
        """User with rejected whitelist request should be blocked with no next step."""
        public_user.first_name = "John"
        public_user.last_name = "Smith"
        public_user.save()

        blacklist_entry = Blacklist.objects.create(
            organization=organization,
            first_name="John",
            last_name="Smith",
            created_by=organization.owner,
        )

        # Create rejected whitelist request
        request = WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.REJECTED,
        )
        request.matched_blacklist_entries.add(blacklist_entry)

        handler = EligibilityService(user=public_user, event=public_event)
        eligibility = handler.check_eligibility()

        assert eligibility.allowed is False
        assert eligibility.reason == Reasons.WHITELIST_REJECTED
        assert eligibility.next_step is None  # No recourse after rejection

    def test_whitelisted_user_allowed(
        self,
        public_user: RevelUser,
        public_event: Event,
        organization: Organization,
    ) -> None:
        """Whitelisted user should pass the blacklist gate."""
        public_user.first_name = "John"
        public_user.last_name = "Smith"
        public_user.save()

        blacklist_entry = Blacklist.objects.create(
            organization=organization,
            first_name="John",
            last_name="Smith",
            created_by=organization.owner,
        )

        # Create approved whitelist request (user is whitelisted)
        whitelist_request = WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=organization.owner,
        )
        whitelist_request.matched_blacklist_entries.add(blacklist_entry)

        handler = EligibilityService(user=public_user, event=public_event)
        eligibility = handler.check_eligibility()

        # User should be allowed (passes blacklist gate, continues to next gates)
        # Final result depends on other gates, but blacklist gate should pass
        # For public event with no other restrictions, should be allowed
        assert eligibility.allowed is True

    def test_no_blacklist_entries_allows_user(
        self,
        public_user: RevelUser,
        public_event: Event,
    ) -> None:
        """User with no blacklist entries should pass the gate."""
        handler = EligibilityService(user=public_user, event=public_event)
        eligibility = handler.check_eligibility()

        assert eligibility.allowed is True

    def test_blacklist_in_different_org_does_not_block(
        self,
        public_user: RevelUser,
        public_event: Event,
        organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """Blacklist in different org should not affect access."""
        # Create another org
        other_org = Organization.objects.create(
            name="Other Org",
            slug="other-org",
            owner=organization_owner_user,
        )

        # Blacklist user in other org
        Blacklist.objects.create(
            organization=other_org,
            user=public_user,
            created_by=organization_owner_user,
        )

        handler = EligibilityService(user=public_user, event=public_event)
        eligibility = handler.check_eligibility()

        # Should be allowed - blacklist is in different org
        assert eligibility.allowed is True

    def test_blacklisted_staff_loses_privileges(
        self,
        organization_staff_user: RevelUser,
        public_event: Event,
        organization: Organization,
        staff_member: OrganizationStaff,
    ) -> None:
        """Blacklisting a staff member removes their staff status and blocks them."""
        # Verify user is initially staff
        assert OrganizationStaff.objects.filter(organization=organization, user=organization_staff_user).exists()

        # Blacklist the staff user
        Blacklist.objects.create(
            organization=organization,
            user=organization_staff_user,
            created_by=organization.owner,
        )

        # Staff status should be removed
        assert not OrganizationStaff.objects.filter(organization=organization, user=organization_staff_user).exists()

        # User should have BANNED membership
        membership = OrganizationMember.objects.get(organization=organization, user=organization_staff_user)
        assert membership.status == OrganizationMember.MembershipStatus.BANNED

        handler = EligibilityService(user=organization_staff_user, event=public_event)
        eligibility = handler.check_eligibility()

        # User should now be blocked (no longer staff, treated as hard blacklisted)
        assert eligibility.allowed is False
        assert eligibility.reason == Reasons.BLACKLISTED

    def test_active_member_bypasses_fuzzy_matching(
        self,
        public_event: Event,
        organization: Organization,
        django_user_model: type[RevelUser],
    ) -> None:
        """Active members should bypass fuzzy matching verification."""
        # Create a member named "Frank"
        frank = django_user_model.objects.create_user(
            username="frank_member",
            email="frank@example.com",
            password="pass",
            first_name="Frank",
            last_name="Smith",
        )
        OrganizationMember.objects.create(
            organization=organization,
            user=frank,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        # Create a blacklist entry for "Frankie" (fuzzy match to "Frank")
        Blacklist.objects.create(
            organization=organization,
            first_name="Frankie",
            last_name="Smith",
            created_by=organization.owner,
        )

        handler = EligibilityService(user=frank, event=public_event)
        eligibility = handler.check_eligibility()

        # Frank should be allowed - active members bypass fuzzy matching
        assert eligibility.allowed is True

    def test_non_member_requires_fuzzy_match_verification(
        self,
        public_event: Event,
        organization: Organization,
        django_user_model: type[RevelUser],
    ) -> None:
        """Non-members with fuzzy matches should require whitelist verification."""
        # Create a non-member named "Frank"
        frank = django_user_model.objects.create_user(
            username="frank_nonmember",
            email="frank_nm@example.com",
            password="pass",
            first_name="Frank",
            last_name="Smith",
        )

        # Create a blacklist entry for "Frankie" (fuzzy match to "Frank")
        Blacklist.objects.create(
            organization=organization,
            first_name="Frankie",
            last_name="Smith",
            created_by=organization.owner,
        )

        handler = EligibilityService(user=frank, event=public_event)
        eligibility = handler.check_eligibility()

        # Frank should need to request whitelist verification
        assert eligibility.allowed is False
        assert eligibility.reason == Reasons.VERIFICATION_REQUIRED
        assert eligibility.next_step == NextStep.REQUEST_WHITELIST

    def test_owner_bypasses_blacklist(
        self,
        organization_owner_user: RevelUser,
        public_event: Event,
        organization: Organization,
    ) -> None:
        """Organization owner should bypass blacklist check."""
        # Blacklist the owner (weird case, but should be handled)
        Blacklist.objects.create(
            organization=organization,
            user=organization_owner_user,
            created_by=organization_owner_user,
        )

        handler = EligibilityService(user=organization_owner_user, event=public_event)
        eligibility = handler.check_eligibility()

        # Owner should be allowed despite blacklist (privileged access)
        assert eligibility.allowed is True
