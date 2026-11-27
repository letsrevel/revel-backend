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
from accounts.utils.email_normalization import normalize_telegram_for_matching
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
    """Normalize telegram username: trim whitespace, remove the @ prefix, lowercase."""
    return normalize_telegram_for_matching(username) if username else None


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


def lock_organization_for_staff_changes(organization: Organization) -> None:
    """Serialize adding staff and creating a blacklist entry for one organization.

    Both paths call this first, inside their transaction, so that the "is blacklisted" check and the staff
    insert (or the "is staff" check and the entry insert) cannot interleave. The mode is ``FOR NO KEY UPDATE``
    so it does not conflict with the ``FOR KEY SHARE`` that deferred foreign keys take at commit.
    """
    Organization.objects.select_for_update(of=("self",), no_key=True).filter(pk=organization.pk).first()


def _matches_current_staff(
    organization: Organization,
    *,
    user: RevelUser | None,
    email: str | None,
    telegram_username: str | None,
    phone_number: str | None,
) -> bool:
    """Whether an entry with these normalized identifiers would hard-match a current staff member (not the owner).

    Same match as ``users_matching_entries``, evaluated before the entry exists: the linked account, or the
    email (case-insensitive), phone or telegram handle of a staff account.
    """
    q = Q()
    if user:
        q |= Q(user=user)
    if email:
        q |= Q(user__email__iexact=email)
    if phone_number:
        q |= Q(user__phone_number=phone_number)
    if telegram_username:
        q |= Q(user__telegram_users__telegram_username__iexact=telegram_username)
    if not q:
        return False
    return (
        OrganizationStaff.objects.filter(organization=organization)
        .filter(q)
        .exclude(user_id=organization.owner_id)
        .exists()
    )


def _refuse_duplicate_identifiers(
    organization: Organization, *, email: str | None, telegram_username: str | None, phone_number: str | None
) -> None:
    """Raise 400 when any entry of the organization already holds one of these normalized identifiers.

    Linked entries count too: the ban matches the identifiers on every entry, and an erased account's
    linked entry keeps them (#1543).
    """
    existing = Blacklist.objects.filter(organization=organization)
    if email and existing.filter(email__iexact=email).exists():
        raise HttpError(400, str(_("An entry with this email already exists.")))
    if telegram_username and existing.filter(telegram_username__iexact=telegram_username).exists():
        raise HttpError(400, str(_("An entry with this Telegram username already exists.")))
    if phone_number and existing.filter(phone_number=phone_number).exists():
        raise HttpError(400, str(_("An entry with this phone number already exists.")))


def _refuse_if_account_already_covered(organization: Organization, user: RevelUser, email: str | None) -> None:
    """Raise 400 when the organization already has an entry for ``user`` or a linked entry that holds its email.

    Only linked rows are read: the entry of the account itself, or the entry of another account (for example
    an erased one) that keeps this email. No hidden profile field of the account is read (ADR-0020).
    """
    existing = Blacklist.objects.filter(organization=organization)
    covered = existing.filter(user=user).exists()
    if not covered and email:
        covered = existing.filter(user__isnull=False, email__iexact=email).exists()
    if covered:
        raise HttpError(400, str(_("This user is already blacklisted.")))


def _link_existing_email_entry(organization: Organization, user: RevelUser, email: str | None) -> Blacklist | None:
    """Link the oldest unlinked entry of the organization that holds ``email`` to ``user``, if there is one.

    The link is a queryset update, so the ``Blacklist`` post_save receiver does not run. The consequences are
    applied here instead. Returns the linked entry, or ``None`` when no unlinked entry holds the email.
    """
    if not email:
        return None
    entry = (
        Blacklist.objects.select_for_update(of=("self",))
        .filter(organization=organization, user__isnull=True, email__iexact=email)
        .order_by("created_at", "pk")
        .first()
    )
    if entry is None:
        return None
    Blacklist.objects.filter(pk=entry.pk).update(user=user)
    apply_blacklist_consequences(user, organization)
    entry.refresh_from_db()
    return entry


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

    When an account is resolved (quick mode, or manual mode with a verified-email match), in this order:

    - The request is refused (400) when the organization has an entry for the account, or a linked entry
      of another account (for example an erased one) that holds the account's email.
    - In manual mode, the typed Telegram username and phone number are refused (400) when another entry
      holds them. Identifiers copied from the account profile are never checked, so no hidden profile
      field decides the outcome.
    - A current staff member can be blacklisted only by the organization owner (403).
    - When an unlinked entry of the organization holds the account's email, that entry is linked to the
      account and returned. No new row is created. The reason and names given in this call are not applied
      to the existing entry.
    - Otherwise a linked entry is created.

    Without a resolved account, every typed identifier is refused (400) when any entry holds it.

    An entry that matches a current staff member (in either mode) removes their staff role, so only the
    organization owner can create it.

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
        The created Blacklist entry, or the existing unlinked entry that was linked to the account

    Raises:
        HttpError: If no identifiers provided or the user or an identifier is already blacklisted (400), or
            if the entry matches a current staff member and ``created_by`` is not the organization owner (403)
    """
    lock_organization_for_staff_changes(organization)

    manual_mode = user is None
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

    # Normalize identifiers
    email = _normalize_email(email)
    telegram_username = _normalize_telegram_username(telegram_username)
    phone_number = _normalize_phone(phone_number)

    if user is None:
        _refuse_duplicate_identifiers(
            organization, email=email, telegram_username=telegram_username, phone_number=phone_number
        )
    else:
        _refuse_if_account_already_covered(organization, user, email)
        if manual_mode:  # typed identifiers only; the email is owned by the checks above and the link below
            _refuse_duplicate_identifiers(
                organization, email=None, telegram_username=telegram_username, phone_number=phone_number
            )

    if created_by.pk != organization.owner_id and _matches_current_staff(
        organization, user=user, email=email, telegram_username=telegram_username, phone_number=phone_number
    ):
        raise HttpError(403, str(_("Only the organization owner can blacklist a staff member.")))

    if user and (linked := _link_existing_email_entry(organization, user, email)):
        return linked

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
    user regains visibility of the organization. ``CANCELLED`` is used instead of restoring ``ACTIVE`` to avoid
    implicitly re-granting membership privileges that may have pre-existed the ban.

    Args:
        entry: The Blacklist entry to remove
    """
    organization = entry.organization
    # The entry may have banned holders it matched before it was linked to an account (or while unlinked);
    # collect them before the entry is gone, together with its linked user.
    holders = {
        holder.pk: holder
        for holder in (
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
    }
    if entry.user is not None:
        holders.setdefault(entry.user.pk, entry.user)
    entry.delete()

    for holder in holders.values():
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


def _link_matching_entries(user: RevelUser, match: Q) -> int:
    """Link at most one matching unlinked entry per organization to ``user`` and apply the consequences.

    A user holds one linked entry per organization (``unique_blacklist_user_per_org``). In each organization
    that has matching entries:

    - If the user already has a linked entry there, nothing is linked.
    - Otherwise the oldest matching unlinked entry (``created_at``, then ``pk``) is linked.

    The other matching entries stay unlinked. They still enforce the ban by identifier.
    The consequences apply in every matched organization, including the ones where nothing was linked, and
    including the ones where the only match is an entry linked to another account (for example an erased one).
    The link is a queryset update, so the ``Blacklist`` post_save signal does not apply them a second time.

    Each matched organization is locked with ``lock_organization_for_staff_changes`` before the link state is
    read, in ascending pk order so that two linkers cannot deadlock. ``add_to_blacklist`` takes the same lock,
    so a concurrent manual add and this link serialize instead of both creating a linked entry.

    Args:
        user: The user to link an entry for
        match: Filter that selects the entries that match the user's identifiers

    Returns:
        Number of entries linked
    """
    org_ids = set(Blacklist.objects.filter(match).values_list("organization_id", flat=True))
    if not org_ids:
        return 0

    organizations = list(Organization.objects.filter(id__in=org_ids).order_by("pk"))
    for org in organizations:
        lock_organization_for_staff_changes(org)

    candidates = list(
        Blacklist.objects.select_for_update(of=("self",))
        .filter(user__isnull=True, organization_id__in=org_ids)
        .filter(match)
        .order_by("created_at", "pk")
    )
    already_linked = set(
        Blacklist.objects.filter(user=user, organization_id__in=org_ids).values_list("organization_id", flat=True)
    )
    to_link: dict[UUID, Blacklist] = {}
    for entry in candidates:  # oldest first
        if entry.organization_id not in already_linked:
            to_link.setdefault(entry.organization_id, entry)

    linked_count = Blacklist.objects.filter(pk__in=[entry.pk for entry in to_link.values()]).update(user=user)

    for org in organizations:
        apply_blacklist_consequences(user, org)

    return linked_count


@transaction.atomic
def link_blacklist_entries_for_user(user: RevelUser) -> int:
    """Link unlinked blacklist entries to a user and apply consequences.

    Called after user registration, email verification or telegram connection to link
    the existing blacklist entries that match the user's identifiers.
    At most one entry per organization is linked (see :func:`_link_matching_entries`).
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

    return _link_matching_entries(user, q)


@transaction.atomic
def link_blacklist_entries_by_telegram(user: RevelUser, telegram_username: str) -> int:
    """Link blacklist entries by telegram username and apply consequences.

    Called when a user connects their telegram account.
    At most one entry per organization is linked (see :func:`_link_matching_entries`).

    Args:
        user: The user to link entries for
        telegram_username: The telegram username being connected

    Returns:
        Number of entries linked
    """
    if not (normalized := _normalize_telegram_username(telegram_username)):
        return 0

    return _link_matching_entries(user, Q(telegram_username__iexact=normalized))
