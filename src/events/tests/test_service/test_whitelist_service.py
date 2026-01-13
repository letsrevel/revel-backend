# src/events/tests/test_service/test_whitelist_service.py

"""Tests for whitelist_service module.

Tests cover:
- Whitelist status checking (via APPROVED WhitelistRequest)
- Creating whitelist requests
- Approving/rejecting whitelist requests
- Removing from whitelist (deleting APPROVED requests)
- Notification dispatching
"""

from unittest.mock import patch

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Blacklist, Organization, WhitelistRequest
from events.service import whitelist_service

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def whitelist_admin(django_user_model: type[RevelUser]) -> RevelUser:
    """Admin user who manages whitelist."""
    return django_user_model.objects.create_user(
        username="whitelist_admin",
        email="admin@example.com",
        password="pass",
    )


@pytest.fixture
def requester_user(django_user_model: type[RevelUser]) -> RevelUser:
    """User requesting whitelist."""
    return django_user_model.objects.create_user(
        username="requester",
        email="requester@example.com",
        password="pass",
        first_name="John",
        last_name="Doe",
    )


@pytest.fixture
def whitelist_org(whitelist_admin: RevelUser) -> Organization:
    """Organization for whitelist testing."""
    return Organization.objects.create(
        name="Whitelist Test Org",
        slug="whitelist-test-org",
        owner=whitelist_admin,
    )


@pytest.fixture
def fuzzy_blacklist_entry(whitelist_org: Organization, whitelist_admin: RevelUser) -> Blacklist:
    """A blacklist entry for fuzzy matching (no user FK)."""
    return Blacklist.objects.create(
        organization=whitelist_org,
        first_name="John",
        last_name="Doe",
        created_by=whitelist_admin,
    )


# --- is_user_whitelisted tests ---


class TestIsUserWhitelisted:
    """Tests for is_user_whitelisted function."""

    def test_returns_true_when_whitelisted(
        self,
        whitelist_org: Organization,
        whitelist_admin: RevelUser,
        requester_user: RevelUser,
    ) -> None:
        """Should return True when user has an APPROVED whitelist request."""
        WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=whitelist_admin,
        )

        assert whitelist_service.is_user_whitelisted(requester_user, whitelist_org) is True

    def test_returns_false_when_not_whitelisted(
        self,
        whitelist_org: Organization,
        requester_user: RevelUser,
    ) -> None:
        """Should return False when user has no whitelist request."""
        assert whitelist_service.is_user_whitelisted(requester_user, whitelist_org) is False

    def test_returns_false_when_request_pending(
        self,
        whitelist_org: Organization,
        requester_user: RevelUser,
    ) -> None:
        """Should return False when request is pending (not yet approved)."""
        WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.PENDING,
        )

        assert whitelist_service.is_user_whitelisted(requester_user, whitelist_org) is False

    def test_returns_false_when_request_rejected(
        self,
        whitelist_org: Organization,
        requester_user: RevelUser,
    ) -> None:
        """Should return False when request was rejected."""
        WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.REJECTED,
        )

        assert whitelist_service.is_user_whitelisted(requester_user, whitelist_org) is False

    def test_returns_false_for_different_org(
        self,
        whitelist_org: Organization,
        whitelist_admin: RevelUser,
        requester_user: RevelUser,
    ) -> None:
        """Should return False when whitelisted in different org."""
        other_org = Organization.objects.create(
            name="Other Org",
            slug="other-org",
            owner=whitelist_admin,
        )

        WhitelistRequest.objects.create(
            organization=other_org,
            user=requester_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=whitelist_admin,
        )

        assert whitelist_service.is_user_whitelisted(requester_user, whitelist_org) is False


# --- get_whitelist_request tests ---


class TestGetWhitelistRequest:
    """Tests for get_whitelist_request function."""

    def test_returns_request_when_exists(
        self,
        whitelist_org: Organization,
        requester_user: RevelUser,
    ) -> None:
        """Should return whitelist request when it exists."""
        request = WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
        )

        result = whitelist_service.get_whitelist_request(requester_user, whitelist_org)

        assert result == request

    def test_returns_none_when_no_request(
        self,
        whitelist_org: Organization,
        requester_user: RevelUser,
    ) -> None:
        """Should return None when no request exists."""
        result = whitelist_service.get_whitelist_request(requester_user, whitelist_org)

        assert result is None


# --- create_whitelist_request tests ---


class TestCreateWhitelistRequest:
    """Tests for create_whitelist_request function."""

    @patch("events.service.whitelist_service.notification_requested")
    def test_creates_request_successfully(
        self,
        mock_notification: object,
        whitelist_org: Organization,
        requester_user: RevelUser,
        fuzzy_blacklist_entry: Blacklist,
    ) -> None:
        """Should create whitelist request and link matched entries."""
        request = whitelist_service.create_whitelist_request(
            user=requester_user,
            organization=whitelist_org,
            matched_entries=[fuzzy_blacklist_entry],
            message="I am not the blacklisted person",
        )

        assert request.user == requester_user
        assert request.organization == whitelist_org
        assert request.message == "I am not the blacklisted person"
        assert request.status == WhitelistRequest.Status.PENDING
        assert fuzzy_blacklist_entry in request.matched_blacklist_entries.all()

    def test_raises_error_when_already_whitelisted(
        self,
        whitelist_org: Organization,
        whitelist_admin: RevelUser,
        requester_user: RevelUser,
        fuzzy_blacklist_entry: Blacklist,
    ) -> None:
        """Should raise error when user is already whitelisted (has APPROVED request)."""
        WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=whitelist_admin,
        )

        with pytest.raises(HttpError) as exc_info:
            whitelist_service.create_whitelist_request(
                user=requester_user,
                organization=whitelist_org,
                matched_entries=[fuzzy_blacklist_entry],
            )
        assert exc_info.value.status_code == 400
        assert "already whitelisted" in str(exc_info.value.message)

    def test_raises_error_when_request_pending(
        self,
        whitelist_org: Organization,
        requester_user: RevelUser,
        fuzzy_blacklist_entry: Blacklist,
    ) -> None:
        """Should raise error when pending request already exists."""
        WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.PENDING,
        )

        with pytest.raises(HttpError) as exc_info:
            whitelist_service.create_whitelist_request(
                user=requester_user,
                organization=whitelist_org,
                matched_entries=[fuzzy_blacklist_entry],
            )
        assert exc_info.value.status_code == 400
        assert "pending" in str(exc_info.value.message)

    @patch("events.service.whitelist_service.notification_requested")
    def test_allows_new_request_after_rejection(
        self,
        mock_notification: object,
        whitelist_org: Organization,
        requester_user: RevelUser,
        fuzzy_blacklist_entry: Blacklist,
    ) -> None:
        """Should allow creating a new request after previous was rejected."""
        # Create a rejected request
        WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.REJECTED,
        )

        # Should be able to create a new request
        request = whitelist_service.create_whitelist_request(
            user=requester_user,
            organization=whitelist_org,
            matched_entries=[fuzzy_blacklist_entry],
            message="Please reconsider",
        )

        assert request.status == WhitelistRequest.Status.PENDING
        assert request.message == "Please reconsider"


# --- approve_whitelist_request tests ---


class TestApproveWhitelistRequest:
    """Tests for approve_whitelist_request function."""

    @patch("events.service.whitelist_service.notification_requested")
    def test_approves_request(
        self,
        mock_notification: object,
        whitelist_org: Organization,
        whitelist_admin: RevelUser,
        requester_user: RevelUser,
        fuzzy_blacklist_entry: Blacklist,
    ) -> None:
        """Should approve request and update status to APPROVED."""
        request = WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.PENDING,
        )
        request.matched_blacklist_entries.add(fuzzy_blacklist_entry)

        result = whitelist_service.approve_whitelist_request(request, decided_by=whitelist_admin)

        # Check request updated
        assert result.status == WhitelistRequest.Status.APPROVED
        assert result.decided_by == whitelist_admin
        assert result.decided_at is not None

        # Verify user is now whitelisted
        assert whitelist_service.is_user_whitelisted(requester_user, whitelist_org) is True

    def test_raises_error_when_not_pending(
        self,
        whitelist_org: Organization,
        whitelist_admin: RevelUser,
        requester_user: RevelUser,
    ) -> None:
        """Should raise error when request is not pending."""
        request = WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.REJECTED,
        )

        with pytest.raises(HttpError) as exc_info:
            whitelist_service.approve_whitelist_request(request, decided_by=whitelist_admin)
        assert exc_info.value.status_code == 400
        assert "not pending" in str(exc_info.value.message)


# --- reject_whitelist_request tests ---


class TestRejectWhitelistRequest:
    """Tests for reject_whitelist_request function."""

    @patch("events.service.whitelist_service.notification_requested")
    def test_rejects_request(
        self,
        mock_notification: object,
        whitelist_org: Organization,
        whitelist_admin: RevelUser,
        requester_user: RevelUser,
    ) -> None:
        """Should reject request and update status."""
        request = WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.PENDING,
        )

        result = whitelist_service.reject_whitelist_request(request, decided_by=whitelist_admin)

        assert result.status == WhitelistRequest.Status.REJECTED
        assert result.decided_by == whitelist_admin
        assert result.decided_at is not None

    def test_raises_error_when_not_pending(
        self,
        whitelist_org: Organization,
        whitelist_admin: RevelUser,
        requester_user: RevelUser,
    ) -> None:
        """Should raise error when request is not pending."""
        request = WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.APPROVED,
        )

        with pytest.raises(HttpError) as exc_info:
            whitelist_service.reject_whitelist_request(request, decided_by=whitelist_admin)
        assert exc_info.value.status_code == 400


# --- remove_from_whitelist tests ---


class TestRemoveFromWhitelist:
    """Tests for remove_from_whitelist function."""

    def test_removes_approved_request(
        self,
        whitelist_org: Organization,
        whitelist_admin: RevelUser,
        requester_user: RevelUser,
    ) -> None:
        """Should delete an APPROVED whitelist request."""
        request = WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=whitelist_admin,
        )
        request_id = request.id

        whitelist_service.remove_from_whitelist(request)

        assert not WhitelistRequest.objects.filter(id=request_id).exists()

    def test_raises_error_when_not_approved(
        self,
        whitelist_org: Organization,
        requester_user: RevelUser,
    ) -> None:
        """Should raise error when trying to remove a non-approved request."""
        request = WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.PENDING,
        )

        with pytest.raises(HttpError) as exc_info:
            whitelist_service.remove_from_whitelist(request)
        assert exc_info.value.status_code == 400
        assert "approved" in str(exc_info.value.message).lower()

    @patch("events.service.whitelist_service.notification_requested")
    def test_user_can_request_again_after_removal(
        self,
        mock_notification: object,
        whitelist_org: Organization,
        whitelist_admin: RevelUser,
        requester_user: RevelUser,
        fuzzy_blacklist_entry: Blacklist,
    ) -> None:
        """After removing from whitelist, user should be able to request again."""
        # Create and remove an approved request
        request = WhitelistRequest.objects.create(
            organization=whitelist_org,
            user=requester_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=whitelist_admin,
        )
        whitelist_service.remove_from_whitelist(request)

        # User should be able to submit a new request
        new_request = whitelist_service.create_whitelist_request(
            user=requester_user,
            organization=whitelist_org,
            matched_entries=[fuzzy_blacklist_entry],
            message="Requesting again",
        )

        assert new_request.status == WhitelistRequest.Status.PENDING
