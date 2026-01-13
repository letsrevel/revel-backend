# src/events/tests/test_service/test_blacklist_service.py

"""Tests for blacklist_service module.

Tests cover:
- User lookup by identifiers (email, phone, telegram)
- Adding users to blacklist (quick mode and manual mode)
- Hard blacklist checking
- Fuzzy name matching
- Automatic user linking
- Phone number normalization
"""

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Blacklist, Organization
from events.service import blacklist_service
from telegram.models import TelegramUser

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def blacklist_admin(django_user_model: type[RevelUser]) -> RevelUser:
    """Admin user who creates blacklist entries."""
    return django_user_model.objects.create_user(
        username="blacklist_admin",
        email="admin@example.com",
        password="pass",
    )


@pytest.fixture
def target_user(django_user_model: type[RevelUser]) -> RevelUser:
    """User to be blacklisted."""
    return django_user_model.objects.create_user(
        username="target_user",
        email="target@example.com",
        password="pass",
        first_name="John",
        last_name="Doe",
        preferred_name="Johnny",
        phone_number="+1234567890",
    )


@pytest.fixture
def blacklist_org(blacklist_admin: RevelUser) -> Organization:
    """Organization for blacklist testing."""
    return Organization.objects.create(
        name="Blacklist Test Org",
        slug="blacklist-test-org",
        owner=blacklist_admin,
    )


# --- find_user_by_identifiers tests ---


class TestFindUserByIdentifiers:
    """Tests for find_user_by_identifiers function."""

    def test_finds_user_by_email(self, target_user: RevelUser) -> None:
        """Should find user by exact email match (case insensitive)."""
        result = blacklist_service.find_user_by_identifiers(email="TARGET@EXAMPLE.COM")
        assert result == target_user

    def test_finds_user_by_phone(self, target_user: RevelUser) -> None:
        """Should find user by phone number."""
        result = blacklist_service.find_user_by_identifiers(phone_number="+1234567890")
        assert result == target_user

    def test_finds_user_by_phone_with_formatting(self, target_user: RevelUser) -> None:
        """Should find user by phone number even with formatting differences."""
        # Phone is normalized, so +1 234-567-890 should match +1234567890
        result = blacklist_service.find_user_by_identifiers(phone_number="+1 234-567-890")
        assert result == target_user

    def test_finds_user_by_telegram(self, target_user: RevelUser) -> None:
        """Should find user by telegram username."""
        TelegramUser.objects.create(
            user=target_user,
            telegram_id=12345,
            telegram_username="johndoe",
        )
        result = blacklist_service.find_user_by_identifiers(
            telegram_username="@JohnDoe"  # with @ prefix, different case
        )
        assert result == target_user

    def test_returns_none_when_no_match(self) -> None:
        """Should return None when no user matches."""
        result = blacklist_service.find_user_by_identifiers(email="nonexistent@example.com")
        assert result is None

    def test_email_takes_priority(self, target_user: RevelUser, django_user_model: type[RevelUser]) -> None:
        """Email match should be returned first even if other identifiers match different users."""
        # First update target_user to free up the phone number
        target_user.phone_number = "+9999999999"
        target_user.save()

        # Now create other user with the old phone number
        django_user_model.objects.create_user(
            username="other",
            email="other@example.com",
            phone_number="+1234567890",  # previously target's phone
        )

        result = blacklist_service.find_user_by_identifiers(
            email="target@example.com",
            phone_number="+1234567890",  # matches other_user (but email wins)
        )
        assert result == target_user  # email match wins


# --- add_to_blacklist tests (Manual Mode) ---


class TestAddToBlacklistManualMode:
    """Tests for add_to_blacklist function in manual mode."""

    def test_creates_entry_with_email_only(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
    ) -> None:
        """Should create blacklist entry with just an email."""
        entry = blacklist_service.add_to_blacklist(
            organization=blacklist_org,
            created_by=blacklist_admin,
            email="bad@example.com",
            reason="Spam",
        )

        assert entry.email == "bad@example.com"
        assert entry.reason == "Spam"
        assert entry.user is None
        assert entry.created_by == blacklist_admin

    def test_creates_entry_with_name_only(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
    ) -> None:
        """Should create blacklist entry with just name fields (for fuzzy matching)."""
        entry = blacklist_service.add_to_blacklist(
            organization=blacklist_org,
            created_by=blacklist_admin,
            first_name="Bad",
            last_name="Actor",
        )

        assert entry.first_name == "Bad"
        assert entry.last_name == "Actor"
        assert entry.email is None
        assert entry.user is None

    def test_normalizes_email(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
    ) -> None:
        """Should normalize email to lowercase."""
        entry = blacklist_service.add_to_blacklist(
            organization=blacklist_org,
            created_by=blacklist_admin,
            email="  BAD@EXAMPLE.COM  ",
        )
        assert entry.email == "bad@example.com"

    def test_normalizes_telegram_username(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
    ) -> None:
        """Should normalize telegram username (remove @, lowercase)."""
        entry = blacklist_service.add_to_blacklist(
            organization=blacklist_org,
            created_by=blacklist_admin,
            telegram_username="@BadUser",
        )
        assert entry.telegram_username == "baduser"

    def test_normalizes_phone_number(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
    ) -> None:
        """Should normalize phone number (remove formatting)."""
        entry = blacklist_service.add_to_blacklist(
            organization=blacklist_org,
            created_by=blacklist_admin,
            phone_number="+1 (234) 567-890",
        )
        assert entry.phone_number == "+1234567890"

    def test_auto_links_existing_user_by_email(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should auto-link to existing user when email matches."""
        entry = blacklist_service.add_to_blacklist(
            organization=blacklist_org,
            created_by=blacklist_admin,
            email="target@example.com",
        )

        assert entry.user == target_user

    def test_auto_links_existing_user_by_telegram(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should auto-link to existing user when telegram matches."""
        TelegramUser.objects.create(
            user=target_user,
            telegram_id=12345,
            telegram_username="targetuser",
        )

        entry = blacklist_service.add_to_blacklist(
            organization=blacklist_org,
            created_by=blacklist_admin,
            telegram_username="@targetuser",
        )

        assert entry.user == target_user

    def test_raises_error_no_identifiers(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
    ) -> None:
        """Should raise error when no identifiers or names provided."""
        with pytest.raises(HttpError) as exc_info:
            blacklist_service.add_to_blacklist(
                organization=blacklist_org,
                created_by=blacklist_admin,
            )
        assert exc_info.value.status_code == 400
        assert "At least one identifier" in str(exc_info.value.message)

    def test_raises_error_duplicate_email(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
    ) -> None:
        """Should raise error when email already blacklisted."""
        # Create first entry
        blacklist_service.add_to_blacklist(
            organization=blacklist_org,
            created_by=blacklist_admin,
            email="duplicate@example.com",
        )

        # Try to create duplicate
        with pytest.raises(HttpError) as exc_info:
            blacklist_service.add_to_blacklist(
                organization=blacklist_org,
                created_by=blacklist_admin,
                email="duplicate@example.com",
            )
        assert exc_info.value.status_code == 400
        assert "already exists" in str(exc_info.value.message)


# --- add_to_blacklist tests (Quick Mode) ---


class TestAddToBlacklistQuickMode:
    """Tests for add_to_blacklist function in quick mode (with user object)."""

    def test_populates_fields_from_user(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should populate all fields from user object."""
        entry = blacklist_service.add_to_blacklist(
            organization=blacklist_org,
            created_by=blacklist_admin,
            user=target_user,
            reason="Test reason",
        )

        assert entry.user == target_user
        assert entry.email == target_user.email
        assert entry.phone_number == target_user.phone_number
        assert entry.first_name == target_user.first_name
        assert entry.last_name == target_user.last_name
        assert entry.preferred_name == target_user.preferred_name

    def test_includes_telegram_username(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should include telegram username when user has connected telegram."""
        TelegramUser.objects.create(
            user=target_user,
            telegram_id=12345,
            telegram_username="targetuser",
        )

        entry = blacklist_service.add_to_blacklist(
            organization=blacklist_org,
            created_by=blacklist_admin,
            user=target_user,
        )

        assert entry.telegram_username == "targetuser"

    def test_raises_error_user_already_blacklisted(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should raise error when user is already blacklisted."""
        # Blacklist user first time
        blacklist_service.add_to_blacklist(
            organization=blacklist_org,
            created_by=blacklist_admin,
            user=target_user,
        )

        # Try again
        with pytest.raises(HttpError) as exc_info:
            blacklist_service.add_to_blacklist(
                organization=blacklist_org,
                created_by=blacklist_admin,
                user=target_user,
            )
        assert exc_info.value.status_code == 400
        assert "already blacklisted" in str(exc_info.value.message)


# --- add_user_to_blacklist tests ---


class TestAddUserToBlacklist:
    """Tests for add_user_to_blacklist function (by user ID)."""

    def test_blacklists_user_by_id(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should blacklist user by UUID."""
        entry = blacklist_service.add_user_to_blacklist(
            organization=blacklist_org,
            user_id=target_user.id,
            created_by=blacklist_admin,
            reason="Quick blacklist",
        )

        assert entry.user == target_user
        assert entry.reason == "Quick blacklist"

    def test_raises_404_for_nonexistent_user(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
    ) -> None:
        """Should raise 404 when user_id doesn't exist."""
        import uuid

        fake_id = uuid.uuid4()
        with pytest.raises(HttpError) as exc_info:
            blacklist_service.add_user_to_blacklist(
                organization=blacklist_org,
                user_id=fake_id,
                created_by=blacklist_admin,
            )
        assert exc_info.value.status_code == 404


# --- check_user_hard_blacklisted tests ---


class TestCheckUserHardBlacklisted:
    """Tests for check_user_hard_blacklisted function."""

    def test_returns_true_for_fk_match(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should return True when user FK matches."""
        Blacklist.objects.create(
            organization=blacklist_org,
            user=target_user,
            created_by=blacklist_admin,
        )

        assert blacklist_service.check_user_hard_blacklisted(target_user, blacklist_org) is True

    def test_returns_true_for_email_match(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should return True when email matches (even without user FK)."""
        Blacklist.objects.create(
            organization=blacklist_org,
            email=target_user.email,
            created_by=blacklist_admin,
        )

        assert blacklist_service.check_user_hard_blacklisted(target_user, blacklist_org) is True

    def test_returns_true_for_phone_match(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should return True when phone matches."""
        Blacklist.objects.create(
            organization=blacklist_org,
            phone_number=target_user.phone_number,
            created_by=blacklist_admin,
        )

        assert blacklist_service.check_user_hard_blacklisted(target_user, blacklist_org) is True

    def test_returns_true_for_telegram_match(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should return True when telegram username matches."""
        TelegramUser.objects.create(
            user=target_user,
            telegram_id=12345,
            telegram_username="targetuser",
        )

        Blacklist.objects.create(
            organization=blacklist_org,
            telegram_username="targetuser",
            created_by=blacklist_admin,
        )

        assert blacklist_service.check_user_hard_blacklisted(target_user, blacklist_org) is True

    def test_returns_false_when_not_blacklisted(
        self,
        blacklist_org: Organization,
        target_user: RevelUser,
    ) -> None:
        """Should return False when user is not blacklisted."""
        assert blacklist_service.check_user_hard_blacklisted(target_user, blacklist_org) is False

    def test_returns_false_for_different_org(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should return False when blacklisted in different org."""
        other_org = Organization.objects.create(
            name="Other Org",
            slug="other-org",
            owner=blacklist_admin,
        )

        Blacklist.objects.create(
            organization=other_org,
            user=target_user,
            created_by=blacklist_admin,
        )

        assert blacklist_service.check_user_hard_blacklisted(target_user, blacklist_org) is False


# --- get_hard_blacklisted_org_ids tests ---


class TestGetHardBlacklistedOrgIds:
    """Tests for get_hard_blacklisted_org_ids function."""

    def test_returns_org_ids_where_blacklisted(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should return organization IDs where user is hard-blacklisted."""
        Blacklist.objects.create(
            organization=blacklist_org,
            user=target_user,
            created_by=blacklist_admin,
        )

        org_ids = list(blacklist_service.get_hard_blacklisted_org_ids(target_user))
        assert blacklist_org.id in org_ids

    def test_returns_empty_when_not_blacklisted(
        self,
        target_user: RevelUser,
    ) -> None:
        """Should return empty when user is not blacklisted anywhere."""
        org_ids = list(blacklist_service.get_hard_blacklisted_org_ids(target_user))
        assert len(org_ids) == 0


# --- Fuzzy Matching Tests ---


class TestFuzzyMatching:
    """Tests for fuzzy name matching functions."""

    def test_get_fuzzy_match_score_exact_match(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should return 100 for exact name match."""
        entry = Blacklist.objects.create(
            organization=blacklist_org,
            first_name="John",
            last_name="Doe",
            created_by=blacklist_admin,
        )

        score = blacklist_service.get_fuzzy_match_score(target_user, entry)
        assert score == 100

    def test_get_fuzzy_match_score_similar_names(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should return high score for similar names."""
        entry = Blacklist.objects.create(
            organization=blacklist_org,
            first_name="Jon",  # typo
            last_name="Doe",
            created_by=blacklist_admin,
        )

        score = blacklist_service.get_fuzzy_match_score(target_user, entry)
        assert score is not None
        assert score >= 85  # above threshold

    def test_get_fuzzy_match_score_different_names(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should return None for completely different names."""
        entry = Blacklist.objects.create(
            organization=blacklist_org,
            first_name="Alice",
            last_name="Smith",
            created_by=blacklist_admin,
        )

        score = blacklist_service.get_fuzzy_match_score(target_user, entry)
        assert score is None  # below threshold

    def test_get_fuzzy_match_score_matches_preferred_name(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should match against preferred_name as well."""
        entry = Blacklist.objects.create(
            organization=blacklist_org,
            preferred_name="Johnny",  # matches target_user.preferred_name
            created_by=blacklist_admin,
        )

        score = blacklist_service.get_fuzzy_match_score(target_user, entry)
        assert score == 100

    def test_get_fuzzy_blacklist_matches_returns_sorted(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should return matches sorted by score descending."""
        # Create entries with varying similarity
        Blacklist.objects.create(
            organization=blacklist_org,
            first_name="John",
            last_name="Doe",  # exact match
            created_by=blacklist_admin,
        )
        Blacklist.objects.create(
            organization=blacklist_org,
            first_name="Jon",  # typo, less similar
            last_name="Doh",
            created_by=blacklist_admin,
        )

        matches = blacklist_service.get_fuzzy_blacklist_matches(target_user, blacklist_org)

        assert len(matches) >= 1
        # First match should have highest score
        if len(matches) > 1:
            assert matches[0][1] >= matches[1][1]

    def test_get_fuzzy_blacklist_matches_ignores_linked_entries(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        """Should not include entries that are already linked to a user."""
        other_user = django_user_model.objects.create_user(username="other", email="other@test.com")

        # Entry linked to other user (should be ignored)
        Blacklist.objects.create(
            organization=blacklist_org,
            user=other_user,
            first_name="John",
            last_name="Doe",
            created_by=blacklist_admin,
        )

        matches = blacklist_service.get_fuzzy_blacklist_matches(target_user, blacklist_org)

        assert len(matches) == 0


# --- Automatic Linking Tests ---


class TestAutomaticLinking:
    """Tests for automatic blacklist entry linking.

    Note: The signal in events/signals.py auto-links on user creation.
    These tests verify the link_blacklist_entries_for_user function works
    when called manually (e.g., after a user adds a phone number later).
    """

    def test_link_blacklist_entries_for_user_by_email(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should link unlinked entries when user's email matches.

        Note: In practice, the signal auto-links on user creation, but this tests
        the function when called manually (e.g., for users created before the feature).
        """
        # Create unlinked entry that matches target_user's email
        entry = Blacklist.objects.create(
            organization=blacklist_org,
            email=target_user.email,  # matches target_user
            created_by=blacklist_admin,
        )
        assert entry.user is None

        # Manually call link (simulating retroactive linking for existing users)
        linked_count = blacklist_service.link_blacklist_entries_for_user(target_user)

        assert linked_count == 1
        entry.refresh_from_db()
        assert entry.user == target_user

    def test_link_blacklist_entries_for_user_by_phone(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should link unlinked entries when user's phone matches."""
        # Create unlinked entry that matches target_user's phone
        entry = Blacklist.objects.create(
            organization=blacklist_org,
            phone_number=target_user.phone_number,  # matches target_user
            created_by=blacklist_admin,
        )
        assert entry.user is None

        # Manually call link
        linked_count = blacklist_service.link_blacklist_entries_for_user(target_user)

        assert linked_count == 1
        entry.refresh_from_db()
        assert entry.user == target_user

    def test_link_blacklist_entries_by_telegram(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
    ) -> None:
        """Should link entries when telegram username is connected."""
        entry = Blacklist.objects.create(
            organization=blacklist_org,
            telegram_username="newtguser",
            created_by=blacklist_admin,
        )

        linked_count = blacklist_service.link_blacklist_entries_by_telegram(target_user, "@NewTGUser")

        assert linked_count == 1
        entry.refresh_from_db()
        assert entry.user == target_user

    def test_does_not_relink_already_linked_entries(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
        target_user: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        """Should not change entries already linked to another user."""
        other_user = django_user_model.objects.create_user(
            username="other",
            email="other@test.com",
        )

        # Entry already linked to other_user
        entry = Blacklist.objects.create(
            organization=blacklist_org,
            user=other_user,
            email="target@example.com",  # same as target_user
            created_by=blacklist_admin,
        )

        linked_count = blacklist_service.link_blacklist_entries_for_user(target_user)

        assert linked_count == 0
        entry.refresh_from_db()
        assert entry.user == other_user  # unchanged


# --- Update/Remove Tests ---


class TestUpdateAndRemove:
    """Tests for updating and removing blacklist entries."""

    def test_update_blacklist_entry(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
    ) -> None:
        """Should update reason and name fields."""
        entry = Blacklist.objects.create(
            organization=blacklist_org,
            email="test@example.com",
            reason="Original reason",
            first_name="Original",
            created_by=blacklist_admin,
        )

        updated = blacklist_service.update_blacklist_entry(
            entry,
            reason="Updated reason",
            first_name="Updated",
            last_name="NewLast",
        )

        assert updated.reason == "Updated reason"
        assert updated.first_name == "Updated"
        assert updated.last_name == "NewLast"

    def test_update_blacklist_entry_partial(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
    ) -> None:
        """Should only update provided fields."""
        entry = Blacklist.objects.create(
            organization=blacklist_org,
            email="test@example.com",
            reason="Original",
            first_name="Keep",
            created_by=blacklist_admin,
        )

        updated = blacklist_service.update_blacklist_entry(
            entry,
            reason="Changed",
        )

        assert updated.reason == "Changed"
        assert updated.first_name == "Keep"  # unchanged

    def test_remove_from_blacklist(
        self,
        blacklist_org: Organization,
        blacklist_admin: RevelUser,
    ) -> None:
        """Should delete blacklist entry."""
        entry = Blacklist.objects.create(
            organization=blacklist_org,
            email="delete@example.com",
            created_by=blacklist_admin,
        )
        entry_id = entry.id

        blacklist_service.remove_from_blacklist(entry)

        assert not Blacklist.objects.filter(id=entry_id).exists()
