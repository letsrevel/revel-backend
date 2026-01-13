"""Service layer for blacklist management.

This module provides functions for managing organization blacklists,
including adding/removing entries, checking blacklist status, and
performing fuzzy name matching.
"""

from uuid import UUID

import structlog
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from rapidfuzz import fuzz

from accounts.models import RevelUser
from accounts.validators import normalize_phone_number
from events.models import Blacklist, Organization, OrganizationMember, OrganizationStaff

logger = structlog.get_logger(__name__)


def _normalize_telegram_username(username: str | None) -> str | None:
    """Normalize telegram username by removing @ prefix and lowercasing."""
    return username.lstrip("@").lower().strip() if username else None


def _normalize_email(email: str | None) -> str | None:
    """Normalize email by lowercasing."""
    return email.lower().strip() if email else None


def apply_blacklist_consequences(user: RevelUser, organization: Organization) -> None:
    """Apply consequences when a user is blacklisted in an organization.

    This function:
    1. Removes the user from OrganizationStaff (if they are staff)
    2. Sets their OrganizationMember status to BANNED (or creates one with BANNED)

    Note: Organization owners cannot be banned from their own organization.

    Args:
        user: The user being blacklisted
        organization: The organization they are blacklisted from
    """
    # Don't ban the organization owner from their own org
    if organization.owner_id == user.id:
        logger.warning(
            "cannot_ban_organization_owner",
            organization_id=str(organization.id),
            user_id=str(user.id),
        )
        return

    # Remove from staff if they are staff
    staff_deleted, _ = OrganizationStaff.objects.filter(
        organization=organization,
        user=user,
    ).delete()

    if staff_deleted:
        logger.info(
            "blacklisted_user_removed_from_staff",
            organization_id=str(organization.id),
            user_id=str(user.id),
        )

    # Create or update membership with BANNED status
    membership, membership_created = OrganizationMember.objects.update_or_create(
        organization=organization,
        user=user,
        defaults={"status": OrganizationMember.MembershipStatus.BANNED},
    )

    if membership_created:
        logger.info(
            "blacklisted_user_membership_created_as_banned",
            organization_id=str(organization.id),
            user_id=str(user.id),
        )
    else:
        logger.info(
            "blacklisted_user_membership_status_set_to_banned",
            organization_id=str(organization.id),
            user_id=str(user.id),
        )


def _normalize_phone(phone: str | None) -> str | None:
    """Normalize phone number by removing non-numeric chars except + prefix."""
    return normalize_phone_number(phone) if phone else None


def find_user_by_identifiers(
    *,
    email: str | None = None,
    telegram_username: str | None = None,
    phone_number: str | None = None,
) -> RevelUser | None:
    """Try to find an existing user by hard identifiers.

    Args:
        email: Email address to search for
        telegram_username: Telegram username to search for
        phone_number: Phone number to search for

    Returns:
        RevelUser if found, None otherwise
    """
    if email and (user := RevelUser.objects.filter(email__iexact=email).first()):
        return user

    if phone_number:
        normalized_phone = _normalize_phone(phone_number)
        if user := RevelUser.objects.filter(phone_number=normalized_phone).first():
            return user

    if telegram_username:
        if normalized := _normalize_telegram_username(telegram_username):
            if user := RevelUser.objects.filter(telegram_users__telegram_username__iexact=normalized).first():
                return user

    return None


@transaction.atomic
def add_to_blacklist(
    organization: Organization,
    created_by: RevelUser,
    *,
    user: RevelUser | None = None,
    email: str | None = None,
    telegram_username: str | None = None,
    phone_number: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    preferred_name: str | None = None,
    reason: str = "",
) -> Blacklist:
    """Add an entry to the organization blacklist.

    Supports two modes:
    1. Quick mode: Provide a user object, all identifiers are populated from user
    2. Manual mode: Provide individual identifiers, system tries to find matching user

    Args:
        organization: The organization to add the blacklist entry to
        created_by: The user creating the blacklist entry
        user: (Quick mode) User to blacklist - all fields populated from user
        email: Email address
        telegram_username: Telegram username (with or without @)
        phone_number: Phone number in E.164 format
        first_name: First name for fuzzy matching
        last_name: Last name for fuzzy matching
        preferred_name: Preferred/nickname for fuzzy matching
        reason: Reason for blacklisting

    Returns:
        The created Blacklist entry

    Raises:
        HttpError: If no identifiers provided or user already blacklisted
    """
    if user:
        # Quick mode - populate from user
        email = user.email
        phone_number = user.phone_number
        first_name = user.first_name
        last_name = user.last_name
        preferred_name = user.preferred_name

        # Get telegram username if connected
        if tg_user := user.telegram_users.first():
            telegram_username = tg_user.telegram_username
    else:
        # Manual mode - validate we have at least one identifier or name
        has_hard_id = any([email, telegram_username, phone_number])
        has_name = any([first_name, last_name, preferred_name])

        if not has_hard_id and not has_name:
            raise HttpError(
                400,
                str(_("At least one identifier (email, telegram, phone) or name is required.")),
            )

        # Try to find matching user by hard identifiers
        user = find_user_by_identifiers(
            email=email,
            telegram_username=telegram_username,
            phone_number=phone_number,
        )

    # Check if user is already blacklisted (if we have a user)
    if user and Blacklist.objects.filter(organization=organization, user=user).exists():
        raise HttpError(400, str(_("This user is already blacklisted.")))

    # Normalize identifiers
    email = _normalize_email(email)
    telegram_username = _normalize_telegram_username(telegram_username)
    phone_number = _normalize_phone(phone_number)

    # Check for existing entry by email (when no user)
    if not user and email:
        if Blacklist.objects.filter(
            organization=organization,
            user__isnull=True,
            email__iexact=email,
        ).exists():
            raise HttpError(400, str(_("An entry with this email already exists.")))

    return Blacklist.objects.create(
        organization=organization,
        user=user,
        email=email,
        telegram_username=telegram_username,
        phone_number=phone_number,
        first_name=first_name,
        last_name=last_name,
        preferred_name=preferred_name,
        reason=reason,
        created_by=created_by,
    )


def add_user_to_blacklist(
    organization: Organization,
    user_id: UUID,
    created_by: RevelUser,
    reason: str = "",
) -> Blacklist:
    """Quick method to add a user to blacklist by ID.

    Args:
        organization: The organization
        user_id: The user's UUID to blacklist
        created_by: The user creating the entry
        reason: Reason for blacklisting

    Returns:
        The created Blacklist entry

    Raises:
        HttpError: If user not found or already blacklisted
    """
    if not (user := RevelUser.objects.filter(id=user_id).first()):
        raise HttpError(404, str(_("User not found.")))

    return add_to_blacklist(
        organization=organization,
        created_by=created_by,
        user=user,
        reason=reason,
    )


def remove_from_blacklist(entry: Blacklist) -> None:
    """Remove an entry from the blacklist.

    Args:
        entry: The Blacklist entry to remove
    """
    entry.delete()


def update_blacklist_entry(
    entry: Blacklist,
    *,
    reason: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    preferred_name: str | None = None,
) -> Blacklist:
    """Update a blacklist entry.

    Only allows updating reason and name fields.
    Hard identifiers cannot be changed after creation.

    Args:
        entry: The Blacklist entry to update
        reason: New reason (if provided)
        first_name: New first name (if provided)
        last_name: New last name (if provided)
        preferred_name: New preferred name (if provided)

    Returns:
        Updated Blacklist entry
    """
    update_fields = []

    if reason is not None:
        entry.reason = reason
        update_fields.append("reason")

    if first_name is not None:
        entry.first_name = first_name
        update_fields.append("first_name")

    if last_name is not None:
        entry.last_name = last_name
        update_fields.append("last_name")

    if preferred_name is not None:
        entry.preferred_name = preferred_name
        update_fields.append("preferred_name")

    if update_fields:
        entry.save(update_fields=update_fields)

    return entry


def check_user_hard_blacklisted(user: RevelUser, organization: Organization) -> bool:
    """Check if user is hard-blacklisted (FK match or hard identifier match).

    Hard blacklisting means the user is definitively blocked with no recourse.

    Args:
        user: The user to check
        organization: The organization to check against

    Returns:
        True if user is hard-blacklisted, False otherwise
    """
    # Build query for hard matches
    q = Q(user=user)

    # Email match
    if user.email:
        q |= Q(email__iexact=user.email)

    # Phone match
    if user.phone_number:
        q |= Q(phone_number=user.phone_number)

    # Telegram match
    if telegram_usernames := list(user.telegram_users.values_list("telegram_username", flat=True)):
        q |= Q(telegram_username__in=[u.lower() for u in telegram_usernames if u])

    return Blacklist.objects.filter(organization=organization).filter(q).exists()


def get_hard_blacklisted_org_ids(user: RevelUser) -> "QuerySet[Blacklist]":
    """Get organization IDs where user is hard-blacklisted.

    Used by for_user() managers to exclude blacklisted organizations.

    Args:
        user: The user to check

    Returns:
        ValuesQuerySet of organization IDs
    """
    q = Q(user=user)

    if user.email:
        q |= Q(email__iexact=user.email)

    if user.phone_number:
        q |= Q(phone_number=user.phone_number)

    if telegram_usernames := list(user.telegram_users.values_list("telegram_username", flat=True)):
        q |= Q(telegram_username__in=[u.lower() for u in telegram_usernames if u])

    return Blacklist.objects.filter(q).values_list("organization_id", flat=True)  # type: ignore[return-value]


def _get_name_variants(
    first_name: str | None,
    last_name: str | None,
    preferred_name: str | None,
) -> list[str]:
    """Build list of name variants for fuzzy matching.

    Args:
        first_name: First name
        last_name: Last name
        preferred_name: Preferred/nick name

    Returns:
        List of lowercase name variants to match against
    """
    variants = []

    if first_name:
        variants.append(first_name.lower())
    if last_name:
        variants.append(last_name.lower())
    if preferred_name:
        variants.append(preferred_name.lower())
    if first_name and last_name:
        variants.append(f"{first_name} {last_name}".lower())

    return variants


def get_fuzzy_match_score(user: RevelUser, entry: Blacklist, threshold: int = 85) -> int | None:
    """Calculate fuzzy match score between user and blacklist entry.

    Cross-matches all name variants using rapidfuzz.

    Args:
        user: The user to check
        entry: The blacklist entry to match against
        threshold: Minimum score to consider a match (0-100)

    Returns:
        Highest match score if above threshold, None otherwise
    """
    user_variants = _get_name_variants(
        user.first_name,
        user.last_name,
        user.preferred_name,
    )

    blacklist_variants = _get_name_variants(
        entry.first_name,
        entry.last_name,
        entry.preferred_name,
    )

    if not user_variants or not blacklist_variants:
        return None

    best_score: float = 0.0
    for user_name in user_variants:
        for bl_name in blacklist_variants:
            score = fuzz.ratio(user_name, bl_name)
            best_score = max(best_score, score)

            # Early exit on perfect match
            if score == 100:
                return 100

    return int(best_score) if best_score >= threshold else None


def get_fuzzy_blacklist_matches(
    user: RevelUser,
    organization: Organization,
    threshold: int = 85,
) -> list[tuple[Blacklist, int]]:
    """Get blacklist entries that fuzzy-match the user's name.

    Only checks unlinked entries (entries without a user FK).

    Args:
        user: The user to check
        organization: The organization to check against
        threshold: Minimum score to consider a match (0-100)

    Returns:
        List of (Blacklist, score) tuples, sorted by score descending
    """
    # Only check unlinked entries that have name fields
    entries = Blacklist.objects.filter(
        organization=organization,
        user__isnull=True,
    ).filter(Q(first_name__isnull=False) | Q(last_name__isnull=False) | Q(preferred_name__isnull=False))

    matches = [(entry, score) for entry in entries if (score := get_fuzzy_match_score(user, entry, threshold))]

    # Sort by score descending
    return sorted(matches, key=lambda x: x[1], reverse=True)


@transaction.atomic
def link_blacklist_entries_for_user(user: RevelUser) -> int:
    """Link unlinked blacklist entries to a user and apply consequences.

    Called after user registration or telegram connection to link
    any existing blacklist entries that match the user's identifiers.
    Also applies blacklist consequences (removes from staff, sets membership to BANNED).

    Args:
        user: The user to link entries for

    Returns:
        Number of entries linked
    """
    q = Q()

    # Email match
    if user.email:
        q |= Q(email__iexact=user.email)

    # Phone match
    if user.phone_number:
        q |= Q(phone_number=user.phone_number)

    # Telegram match
    if telegram_usernames := list(user.telegram_users.values_list("telegram_username", flat=True)):
        q |= Q(telegram_username__in=[u.lower() for u in telegram_usernames if u])

    if not q:
        return 0

    # Get the entries that will be linked (need org IDs for consequences)
    entries_to_link = Blacklist.objects.filter(user__isnull=True).filter(q).select_related("organization")
    org_ids = set(entries_to_link.values_list("organization_id", flat=True))

    if not org_ids:
        return 0

    # Update unlinked entries that match
    linked_count = entries_to_link.update(user=user)

    # Apply blacklist consequences for each organization
    for org in Organization.objects.filter(id__in=org_ids):
        apply_blacklist_consequences(user, org)

    return linked_count


@transaction.atomic
def link_blacklist_entries_by_telegram(user: RevelUser, telegram_username: str) -> int:
    """Link blacklist entries by telegram username and apply consequences.

    Called when a user connects their telegram account.

    Args:
        user: The user to link entries for
        telegram_username: The telegram username being connected

    Returns:
        Number of entries linked
    """
    if not (normalized := _normalize_telegram_username(telegram_username)):
        return 0

    # Get the entries that will be linked (need org IDs for consequences)
    entries_to_link = Blacklist.objects.filter(
        user__isnull=True,
        telegram_username__iexact=normalized,
    ).select_related("organization")
    org_ids = set(entries_to_link.values_list("organization_id", flat=True))

    if not org_ids:
        return 0

    # Update the entries
    linked_count = entries_to_link.update(user=user)

    # Apply blacklist consequences for each organization
    for org in Organization.objects.filter(id__in=org_ids):
        apply_blacklist_consequences(user, org)

    return linked_count
