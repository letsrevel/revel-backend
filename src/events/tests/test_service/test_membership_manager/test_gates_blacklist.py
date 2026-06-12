"""Tests for BlacklistGate on the membership eligibility pipeline."""

from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Organization
from events.service.membership_manager import MembershipEligibilityService
from events.service.membership_manager.enums import MembershipNextStep, Reasons

pytestmark = pytest.mark.django_db


def test_hard_blacklisted_user_blocked_with_no_recourse(user: RevelUser, organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])
    with patch("events.service.blacklist_service.check_user_hard_blacklisted", return_value=True):
        service = MembershipEligibilityService(user=user, organization=organization)
        result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.BLACKLISTED)
    assert result.next_step is None


def test_fuzzy_match_with_no_whitelist_request_offers_request(user: RevelUser, organization: Organization) -> None:
    from events.models import Blacklist

    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])
    with (
        patch("events.service.blacklist_service.check_user_hard_blacklisted", return_value=False),
        patch(
            "events.service.blacklist_service.get_fuzzy_blacklist_matches",
            return_value=[(Blacklist(organization=organization, first_name="Jane", last_name="Doe"), 90)],
        ),
        patch("events.service.whitelist_service.is_user_whitelisted", return_value=False),
        patch("events.service.whitelist_service.get_whitelist_request", return_value=None),
    ):
        service = MembershipEligibilityService(user=user, organization=organization)
        result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.REQUIRES_VERIFICATION)
    assert result.next_step == MembershipNextStep.REQUIRES_INVITATION


def test_clean_user_falls_through(user: RevelUser, organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])
    with (
        patch("events.service.blacklist_service.check_user_hard_blacklisted", return_value=False),
        patch("events.service.blacklist_service.get_fuzzy_blacklist_matches", return_value=[]),
    ):
        service = MembershipEligibilityService(user=user, organization=organization)
        result = service.check_eligibility()
    assert result.allowed is True


def test_fuzzy_match_with_pending_whitelist_request_blocks_with_pending_reason(
    user: RevelUser, organization: Organization
) -> None:
    """A fuzzy-matched user with a PENDING whitelist request must see WHITELIST_PENDING.

    The FE uses this distinct reason to render a 'waiting for verification' state
    rather than a fresh 'request verification' CTA.
    """
    from events.models import Blacklist, WhitelistRequest

    # Arrange
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])

    pending_request = WhitelistRequest(
        organization=organization,
        user=user,
        status=WhitelistRequest.Status.PENDING,
    )

    # Act
    with (
        patch("events.service.blacklist_service.check_user_hard_blacklisted", return_value=False),
        patch(
            "events.service.blacklist_service.get_fuzzy_blacklist_matches",
            return_value=[(Blacklist(organization=organization, first_name="Jane", last_name="Doe"), 90)],
        ),
        patch("events.service.whitelist_service.is_user_whitelisted", return_value=False),
        patch("events.service.whitelist_service.get_whitelist_request", return_value=pending_request),
    ):
        service = MembershipEligibilityService(user=user, organization=organization)
        result = service.check_eligibility()

    # Assert
    assert result.allowed is False
    assert result.reason == str(Reasons.WHITELIST_PENDING)
    # No next_step (None) — user has already requested; nothing for them to do.
    assert result.next_step is None


def test_fuzzy_match_with_rejected_whitelist_request_blocks_with_rejected_reason(
    user: RevelUser, organization: Organization
) -> None:
    """A fuzzy-matched user with a REJECTED whitelist request must see WHITELIST_REJECTED.

    Distinct from PENDING so the FE can render a terminal 'verification denied' state.
    """
    from events.models import Blacklist, WhitelistRequest

    # Arrange
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])

    rejected_request = WhitelistRequest(
        organization=organization,
        user=user,
        status=WhitelistRequest.Status.REJECTED,
    )

    # Act
    with (
        patch("events.service.blacklist_service.check_user_hard_blacklisted", return_value=False),
        patch(
            "events.service.blacklist_service.get_fuzzy_blacklist_matches",
            return_value=[(Blacklist(organization=organization, first_name="Jane", last_name="Doe"), 90)],
        ),
        patch("events.service.whitelist_service.is_user_whitelisted", return_value=False),
        patch("events.service.whitelist_service.get_whitelist_request", return_value=rejected_request),
    ):
        service = MembershipEligibilityService(user=user, organization=organization)
        result = service.check_eligibility()

    # Assert
    assert result.allowed is False
    assert result.reason == str(Reasons.WHITELIST_REJECTED)
    assert result.next_step is None


def test_fuzzy_match_when_whitelisted_falls_through(user: RevelUser, organization: Organization) -> None:
    """A fuzzy-matched user who is on the whitelist (APPROVED) must pass the gate.

    An approved whitelist entry overrides the fuzzy match — the user is verified.
    """
    from events.models import Blacklist

    # Arrange
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])

    # Act
    with (
        patch("events.service.blacklist_service.check_user_hard_blacklisted", return_value=False),
        patch(
            "events.service.blacklist_service.get_fuzzy_blacklist_matches",
            return_value=[(Blacklist(organization=organization, first_name="Jane", last_name="Doe"), 90)],
        ),
        patch("events.service.whitelist_service.is_user_whitelisted", return_value=True),
        # whitelist_request must NOT be consulted when is_whitelisted=True — return None to
        # prove BlacklistGate short-circuits before reaching whitelist_request lookup.
        patch("events.service.whitelist_service.get_whitelist_request", return_value=None) as mock_get_req,
    ):
        service = MembershipEligibilityService(user=user, organization=organization)
        result = service.check_eligibility()

    # Assert: full pipeline pass (no later gates fail in this minimal setup).
    assert result.allowed is True
    # Sanity: whitelisted users skip the get_whitelist_request lookup entirely.
    mock_get_req.assert_not_called()
