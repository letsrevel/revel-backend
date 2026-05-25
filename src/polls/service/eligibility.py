"""Eligibility helpers for polls.

Mirrors the visibility model used by
:meth:`events.models.event.Event._compute_can_user_see_address`.
Pure logic — no DB writes. All functions take an already-resolved
:class:`polls.models.Poll` instance and a user-like object.

For list endpoints, the controller annotates each row via
:meth:`polls.models.PollQuerySet.with_user_annotations` and the schema
resolvers consume the annotations through
:func:`passes_visibility_from_annotations` / :func:`user_can_see_results_from_annotations`.
That replaces the previous "bulk context precompute" machinery and keeps the
listing endpoint compatible with ninja_extra's ``@paginate`` decorator.
"""

import typing as t
from uuid import UUID

from django.db.models import QuerySet

from accounts.models import RevelUser
from events.models.mixins import ResourceVisibility
from polls.models import Poll
from polls.types import UserLike

if t.TYPE_CHECKING:
    from events.models.organization import MembershipTier


def is_staff_or_owner(user: UserLike, poll: Poll) -> bool:
    """Return True if ``user`` is a superuser, Django staff, the org owner, or org staff.

    Callers that already know the answer (e.g. controllers that compute it
    once per detail response) should pass it through ``_is_staff`` on the
    consumer helpers (:func:`can_see_results`, :func:`can_vote`,
    :func:`user_has_voted`) so the ``OrganizationStaff`` lookup isn't
    repeated.
    """
    if user.is_anonymous:
        return False
    if user.is_superuser or user.is_staff:
        return True
    if poll.organization.owner_id == user.id:
        return True
    from events.models.organization import OrganizationStaff

    return OrganizationStaff.objects.filter(organization=poll.organization, user=user).exists()


def _passes_members_only(
    user: RevelUser,
    poll: Poll,
    membership_tiers: QuerySet["MembershipTier"] | None,
) -> bool:
    """Check MEMBERS_ONLY visibility, honoring tier restrictions when set.

    Empty tiers queryset means no tier restriction; any active member passes.
    """
    from events.models.organization import OrganizationMember

    member = OrganizationMember.objects.for_visibility().filter(user=user, organization=poll.organization).first()
    if member is None:
        return False
    if membership_tiers is None or not membership_tiers.exists():
        return True
    return member.tier_id in list(membership_tiers.values_list("id", flat=True))


def _passes_event_visibility(user: RevelUser, poll: Poll, visibility: str) -> bool:
    """Check PRIVATE / ATTENDEES_ONLY visibility based on event relationships.

    Returns False if the poll is not attached to an event.
    """
    from events.models.invitation import EventInvitation
    from events.models.rsvp import EventRSVP
    from events.models.ticket import Ticket

    if poll.event_id is None:
        return False

    has_ticket = (
        Ticket.objects.filter(user=user, event_id=poll.event_id).exclude(status=Ticket.TicketStatus.CANCELLED).exists()
    )
    has_rsvp = EventRSVP.objects.filter(user=user, event_id=poll.event_id, status=EventRSVP.RsvpStatus.YES).exists()

    if visibility == ResourceVisibility.ATTENDEES_ONLY:
        return has_ticket or has_rsvp

    if visibility == ResourceVisibility.PRIVATE:
        has_invitation = EventInvitation.objects.filter(user=user, event_id=poll.event_id).exists()
        return has_ticket or has_rsvp or has_invitation

    return False


def _passes_visibility(
    user: UserLike,
    poll: Poll,
    visibility: str,
    membership_tiers: QuerySet["MembershipTier"] | None,
    *,
    _is_staff: bool | None = None,
) -> bool:
    """Apply :class:`ResourceVisibility` semantics for a poll, with optional tier restriction.

    Args:
        user: The user being checked.
        poll: The poll whose audience is being evaluated.
        visibility: One of :class:`ResourceVisibility` values (e.g. ``poll.vote_visibility``).
        membership_tiers: Queryset of ``MembershipTier`` rows (typically
            ``poll.vote_membership_tiers.all()`` or ``poll.result_membership_tiers.all()``).
            Only consulted when ``visibility == MEMBERS_ONLY``.
        _is_staff: Optional pre-computed :func:`is_staff_or_owner` result.
            Pass this when the caller has already resolved it for the same
            ``(user, poll)`` pair to skip the redundant ``OrganizationStaff``
            query.
    """
    # Staff/owner always pass.
    if _is_staff if _is_staff is not None else is_staff_or_owner(user, poll):
        return True

    if visibility in ResourceVisibility.publicly_accessible():
        return True

    if user.is_anonymous:
        return False

    if visibility == ResourceVisibility.STAFF_ONLY:
        # Staff/owner already short-circuited above; if we got here the user is not staff.
        return False

    if visibility == ResourceVisibility.MEMBERS_ONLY:
        return _passes_members_only(user, poll, membership_tiers)

    return _passes_event_visibility(user, poll, visibility)


def user_has_voted(user: UserLike, poll: Poll) -> bool:
    """Return True if ``user`` has a READY submission for the poll's questionnaire."""
    if user.is_anonymous:
        return False
    from questionnaires.models import QuestionnaireSubmission

    return QuestionnaireSubmission.objects.filter(
        user=user,
        questionnaire_id=poll.questionnaire_id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    ).exists()


def can_vote(user: UserLike, poll: Poll, *, _is_staff: bool | None = None) -> bool:
    """Whether ``user`` is currently eligible to cast a vote on ``poll``.

    Anonymous users are never eligible (poll voting requires authentication).
    Status (``poll.status == OPEN``) is NOT checked here — callers verify it separately.

    ``_is_staff`` is an optional pre-computed :func:`is_staff_or_owner` result
    threaded through to :func:`_passes_visibility` (see its docstring).
    """
    if user.is_anonymous:
        return False
    return _passes_visibility(user, poll, poll.vote_visibility, poll.vote_membership_tiers.all(), _is_staff=_is_staff)


def can_see_poll(user: UserLike, poll: Poll, *, _is_staff: bool | None = None) -> bool:
    """Whether ``user`` can see that the poll exists (used for listing/detail).

    A user passes if they fall inside the vote audience OR the result audience
    OR has already voted on the poll. The vote-audience check uses
    :func:`_passes_visibility` directly rather than :func:`can_vote` so that
    anonymous users with a publicly-castable ``vote_visibility`` still see the
    poll exists (they cannot cast a vote, but a public poll must be listable).

    ``_is_staff`` is an optional pre-computed :func:`is_staff_or_owner` result
    threaded through to both :func:`_passes_visibility` calls so the detail GET
    (which also resolves staffness in ``_to_detail``) doesn't repeat the
    ``OrganizationStaff`` lookup. See :func:`_passes_visibility` for details.
    """
    if _passes_visibility(user, poll, poll.vote_visibility, poll.vote_membership_tiers.all(), _is_staff=_is_staff):
        return True
    if _passes_visibility(user, poll, poll.result_visibility, poll.result_membership_tiers.all(), _is_staff=_is_staff):
        return True
    return user_has_voted(user, poll)


def can_see_results(user: UserLike, poll: Poll, *, _is_staff: bool | None = None) -> bool:
    """Whether ``user`` can currently view aggregate results for ``poll``.

    ``_is_staff`` is an optional pre-computed :func:`is_staff_or_owner` result
    threaded through to :func:`_passes_visibility` and used to short-circuit
    the staff/owner check (see :func:`_passes_visibility` for details).
    """
    is_staff = _is_staff if _is_staff is not None else is_staff_or_owner(user, poll)

    # Staff/owner always see results (anonymity governs identity exposure separately).
    if is_staff:
        return True

    # Result audience check first.
    if not _passes_visibility(
        user, poll, poll.result_visibility, poll.result_membership_tiers.all(), _is_staff=is_staff
    ):
        return False

    # Then timing.
    if poll.result_timing == Poll.PollResultTiming.NEVER:
        return False
    if poll.result_timing == Poll.PollResultTiming.AFTER_CLOSE:
        return poll.status == Poll.PollStatus.CLOSED
    if poll.result_timing == Poll.PollResultTiming.AFTER_VOTE:
        return user_has_voted(user, poll)
    return False


# ============================================================================
# Annotation-aware helpers (used by PollListItemSchema resolvers)
# ============================================================================
#
# These read the per-user flags written by
# :meth:`polls.models.PollQuerySet.with_user_annotations`.  If the annotations
# are missing (anonymous user or queryset built without ``with_user_annotations``)
# every flag falls back to False, which matches the pre-existing "anonymous
# users never satisfy per-user signals" semantics in :func:`_passes_visibility`.


def _annotated(poll: Poll, attr: str) -> bool:
    """Read a boolean annotation off ``poll`` defaulting to False if absent."""
    return bool(getattr(poll, attr, False))


def _is_staff_or_owner_from_annotations(user: UserLike, poll: Poll) -> bool:
    """Annotation counterpart to :func:`is_staff_or_owner`."""
    if user.is_anonymous:
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    return _annotated(poll, "_is_org_owner") or _annotated(poll, "_is_org_staff_member")


def passes_visibility_from_annotations(
    user: UserLike,
    poll: Poll,
    visibility: str,
    tier_ids: t.Sequence[UUID],
) -> bool:
    """Pure-Python visibility check using ANNOTATED ``poll`` attributes.

    ``tier_ids`` is the list of ``MembershipTier`` ids attached to the poll for
    the audience being checked (e.g. ``poll.vote_membership_tiers``); pass the
    result of iterating the prefetched M2M to avoid extra queries.
    """
    if _is_staff_or_owner_from_annotations(user, poll):
        return True
    if visibility in ResourceVisibility.publicly_accessible():
        return True
    if user.is_anonymous:
        return False
    if visibility == ResourceVisibility.STAFF_ONLY:
        return False
    if visibility == ResourceVisibility.MEMBERS_ONLY:
        if not _annotated(poll, "_is_org_member"):
            return False
        if not tier_ids:
            return True
        user_tier_id = getattr(poll, "_user_member_tier_id", None)
        return user_tier_id in tier_ids
    if visibility == ResourceVisibility.ATTENDEES_ONLY:
        return _annotated(poll, "_has_ticket") or _annotated(poll, "_has_rsvp")
    if visibility == ResourceVisibility.PRIVATE:
        return _annotated(poll, "_has_ticket") or _annotated(poll, "_has_rsvp") or _annotated(poll, "_has_invitation")
    return False


def user_has_voted_from_annotations(user: UserLike, poll: Poll) -> bool:
    """Annotation counterpart to :func:`user_has_voted`."""
    if user.is_anonymous:
        return False
    return _annotated(poll, "_user_has_voted")


def can_vote_from_annotations(user: UserLike, poll: Poll, vote_tier_ids: t.Sequence[UUID]) -> bool:
    """Annotation counterpart to :func:`can_vote` (without status check)."""
    if user.is_anonymous:
        return False
    return passes_visibility_from_annotations(user, poll, poll.vote_visibility, vote_tier_ids)


def can_see_results_from_annotations(
    user: UserLike,
    poll: Poll,
    result_tier_ids: t.Sequence[UUID],
) -> bool:
    """Annotation counterpart to :func:`can_see_results`."""
    if _is_staff_or_owner_from_annotations(user, poll):
        return True
    if not passes_visibility_from_annotations(user, poll, poll.result_visibility, result_tier_ids):
        return False
    if poll.result_timing == Poll.PollResultTiming.NEVER:
        return False
    if poll.result_timing == Poll.PollResultTiming.AFTER_CLOSE:
        return poll.status == Poll.PollStatus.CLOSED
    if poll.result_timing == Poll.PollResultTiming.AFTER_VOTE:
        return user_has_voted_from_annotations(user, poll)
    return False
