"""Exact-email lookup of an account by an org admin (privacy-sensitive: an existence oracle)."""

import structlog

from accounts.models import RevelUser
from events.models import MembershipRecord, Organization, OrganizationMember, OrganizationMembershipRequest
from events.schema import OrgUserLookupSchema
from events.utils.membership_access import is_eligible_status

logger = structlog.get_logger(__name__)


def lookup_user_by_email(organization: Organization, actor: RevelUser, email: str) -> OrgUserLookupSchema | None:
    """Find a verified, active, non-guest account whose email equals ``email`` (case-insensitive).

    Every other case (no account, unverified, guest, inactive/banned) returns ``None`` so callers cannot tell
    them apart; so does an email shared by several eligible accounts. Exact match only: no partial matching, no
    listing. The email is never logged.

    A match runs a few more queries than a miss, so latency could in theory tell them apart; the per-user
    30/hour throttle on the route bounds that, and the trade-off is accepted.
    """
    matches = list(RevelUser.objects.filter(email__iexact=email, email_verified=True, guest=False, is_active=True)[:2])
    user = matches[0] if len(matches) == 1 else None  # ambiguous (case-variant duplicates) counts as no match
    logger.info(
        "org_user_lookup",
        organization_id=str(organization.pk),
        user_id=str(actor.pk),
        found=user is not None,
        matched_user_id=str(user.pk) if user else None,
    )
    if user is None:
        return None

    member_status = (
        OrganizationMember.objects.filter(organization=organization, user=user).values_list("status", flat=True).first()
    )
    record_id = (
        MembershipRecord.objects.filter(organization=organization, user=user, kind=MembershipRecord.Kind.STANDARD)
        .open()
        .values_list("pk", flat=True)
        .first()
    )
    request_id = (
        OrganizationMembershipRequest.objects.filter(
            organization=organization, user=user, status=OrganizationMembershipRequest.Status.PENDING
        )
        .values_list("pk", flat=True)
        .first()
    )
    return OrgUserLookupSchema(
        user_id=user.pk,
        # Never fall back to ``username``: it is the email address.
        display_name=user.preferred_name or user.get_full_name(),
        is_member=is_eligible_status(member_status),
        membership_status=OrganizationMember.MembershipStatus(member_status) if member_status else None,
        register_record_id=record_id,
        pending_request_id=request_id,
    )
