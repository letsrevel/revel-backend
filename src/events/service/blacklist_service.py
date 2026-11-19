"""Service layer for blacklist management.

This module provides functions for managing organization blacklists,
including adding/removing entries, checking blacklist status, and
performing fuzzy name matching.
"""

from uuid import UUID

import structlog
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from rapidfuzz import fuzz

from accounts.models import RevelUser
from accounts.validators import normalize_phone_number
from events.models import Blacklist, Organization, OrganizationMember, OrganizationStaff
from events.service import day_membership_service
from events.service.holder_visibility import is_known_holder
from events.utils.blacklist import users_matching_entries

logger = structlog.get_logger(__name__)

# Default threshold for fuzzy name matching (0-100 scale)
# 85% provides a good balance between catching variations and avoiding false positives
DEFAULT_FUZZY_MATCH_THRESHOLD = 85


def _normalize_telegram_username(username: str | None) -> str | None:
    """Normalize telegram username by removing @ prefix and lowercasing."""
    return username.lstrip("@").lower().strip() if username else None


def _normalize_email(email: str | None) -> str | None:
    """Normalize email by lowercasing."""
    return email.lower().strip() if email else None


def ban_existing_member(user: RevelUser, organization: Organization) -> None:
    """Flip the user's existing membership rows (if any) to BANNED. Never creates one.

    Per-row ``save`` (not a queryset update) so post_save receivers run: permission snapshot invalidation,
    member-register mirror, and the day-membership revoke for an existing member. A day membership granted
    concurrently is swept by the on-commit revoke in :func:`apply_blacklist_consequences`.
    """
    with transaction.atomic():
        for member in (
            OrganizationMember.objects.select_for_update()
            .filter(organization=organization, user=user)
            .exclude(status=OrganizationMember.MembershipStatus.BANNED)
        ):
            member.status = OrganizationMember.MembershipStatus.BANNED
            member.save(update_fields=["status", "updated_at"])
            logger.info(
                "blacklisted_user_membership_status_set_to_banned",
                organization_id=str(organization.id),
                user_id=str(user.id),
            )


def apply_blacklist_consequences(user: RevelUser, organization: Organization) -> None:
    """Apply consequences when a user is blacklisted in an organization.

    This function:
    1. Removes the user from OrganizationStaff (if they are staff)
    2. Sets an existing OrganizationMember to BANNED (never creates one for a non-member)
    3. Revokes their upcoming day memberships

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

    # A ban never creates a membership row for a non-member (ADR-0020): enforcement reads the blacklist
    # itself. An existing member is flipped to BANNED, and again on commit to catch a membership created
    # concurrently whose blacklist check ran before this entry committed.
    ban_existing_member(user, organization)
    transaction.on_commit(lambda: ban_existing_member(user, organization))

    # No membership row means no post_save ban signal for a non-member, so revoke day memberships directly.
    day_membership_service.revoke_upcoming_for_ban(organization.id, user.id)
    # A day-membership grant that committed concurrently is invisible to the revoke above; sweep again on commit.
    transaction.on_commit(lambda: day_membership_service.revoke_upcoming_for_ban(organization.id, user.id))

    # A ban must also stop billing: cancel any live subscription (and its Stripe
    # side) so the member isn't charged for access they no longer have.
    from events.service.subscription import lifecycle as subscription_lifecycle  # lazy: avoid cycle

    cancelled = subscription_lifecycle.cancel_subscriptions_for_membership_loss(user, organization)
    if cancelled:
        logger.info(
            "blacklisted_user_subscriptions_cancelled",
            organization_id=str(organization.id),
            user_id=str(user.id),
            count=cancelled,
        )


def _normalize_phone(phone: str | None) -> str | None:
    """Normalize phone number by removing non-numeric chars except + prefix."""
    return normalize_phone_number(phone) if phone else None


def find_user_by_verified_email(email: str | None) -> RevelUser | None:
    """Find the account that owns ``email``: exact (case-insensitive) match on a verified, active, non-guest account.

    Manual blacklist entries link an account only this way: a typed phone number, telegram handle or
    unverified email is something staff supplied, not proof of who holds it.
    """
    if not email:
        return None
    return RevelUser.objects.filter(email__iexact=email, email_verified=True, is_active=True, guest=False).first()


def _apply_consequences_to_matched_holders(entry: Blacklist) -> None:
    """Apply consequences to existing staff/members of the org that the (unlinked) ``entry`` hard-matches.

    A manual entry links an account only by verified email, so a phone, telegram or unverified-email match
    is not linked (no FK, no rows created). Existing staff and members it matches must still lose access.
    Matching is the same as ``check_user_hard_blacklisted``; the owner is exempt.
    """
    organization = entry.organization
    holders = (
        users_matching_entries(Blacklist.objects.filter(pk=entry.pk))
        .filter(
            Exists(OrganizationStaff.objects.filter(organization=organization, user=OuterRef("pk")))
            | Exists(OrganizationMember.objects.filter(organization=organization, user=OuterRef("pk")))
        )
        .exclude(pk=organization.owner_id)
    )
    for holder in holders:
        apply_blacklist_consequences(holder, organization)


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
    1. Quick mode: Provide a user object. The email is always stored; the profile (phone, names, telegram)
       is copied only when the account is a known holder for the organization (ADR-0020).
    2. Manual mode: Provide individual identifiers. An account is linked only by an exact verified-email
       match. An unlinked entry still applies the ban consequences to existing staff and members of the
       organization that it matches by email, phone or telegram (the owner is exempt).

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
        # Quick mode - populate from user. The profile is copied only for an account that acted toward the
        # organization; a stranger's entry keeps the email alone (ADR-0020).
        email = user.email
        if is_known_holder(organization, user):
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

        # Link an account only on an exact verified-email match.
        user = find_user_by_verified_email(email)

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

    entry = Blacklist.objects.create(
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
    if entry.user is None:
        _apply_consequences_to_matched_holders(entry)
    return entry


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


@transaction.atomic
def remove_from_blacklist(entry: Blacklist) -> None:
    """Remove an entry from the blacklist.

    For each account the entry covered (its linked user, or for an unlinked entry the holders it matched that
    carry a BANNED row) that no other blacklist entry still covers in the organization, the stale ``BANNED``
    membership left over from :func:`apply_blacklist_consequences` is transitioned to ``CANCELLED`` so the
    user regains visibility of the organization. ``CANCELLED`` is used instead of restoring ``ACTIVE`` to avoid implicitly re-granting membership
    privileges that may have pre-existed the ban.

    Args:
        entry: The Blacklist entry to remove
    """
    organization = entry.organization
    if entry.user is not None:
        holders = [entry.user]
    else:
        # An unlinked entry banned the holders it matched; collect them before the entry is gone.
        holders = list(
            users_matching_entries(Blacklist.objects.filter(pk=entry.pk))
            .filter(
                Exists(
                    OrganizationMember.objects.filter(
                        organization=organization,
                        user=OuterRef("pk"),
                        status=OrganizationMember.MembershipStatus.BANNED,
                    )
                )
            )
            .exclude(pk=organization.owner_id)
        )
    entry.delete()

    for holder in holders:
        _lift_ban_if_clear(holder, organization)


def _lift_ban_if_clear(user: RevelUser, organization: Organization) -> None:
    """Move the user's BANNED rows to CANCELLED unless another entry still covers them."""
    if check_user_hard_blacklisted(user, organization):
        logger.info(
            "unban_skipped_user_still_hard_blacklisted",
            organization_id=str(organization.id),
            user_id=str(user.id),
        )
        return

    # Per-row save (not a bulk update) so post_save receivers run: permission snapshot
    # invalidation and the member-register mirror.
    banned = list(
        OrganizationMember.objects.filter(
            organization=organization,
            user=user,
            status=OrganizationMember.MembershipStatus.BANNED,
        )
    )
    for banned_member in banned:
        banned_member.status = OrganizationMember.MembershipStatus.CANCELLED
        banned_member.save(update_fields=["status", "updated_at"])

    if banned:
        logger.info(
            "unbanned_user_membership_status_reset_to_cancelled",
            organization_id=str(organization.id),
            user_id=str(user.id),
        )

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


# Re-exported for back-compat with existing service-layer call sites and tests.
# Canonical location is ``events.utils.blacklist`` because model managers must
# not import from ``events.service`` (see CLAUDE.md).
from events.utils.blacklist import get_hard_blacklisted_org_ids as get_hard_blacklisted_org_ids  # noqa: E402


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


def get_fuzzy_match_score(
    user: RevelUser, entry: Blacklist, threshold: int = DEFAULT_FUZZY_MATCH_THRESHOLD
) -> int | None:
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
    threshold: int = DEFAULT_FUZZY_MATCH_THRESHOLD,
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
