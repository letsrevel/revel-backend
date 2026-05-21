"""Eligibility helpers for polls.

Mirrors the visibility model used by
:meth:`events.models.event.Event._compute_can_user_see_address`.
Pure logic — no DB writes. All functions take an already-resolved
:class:`polls.models.Poll` instance and a user-like object.
"""

import typing as t

from django.contrib.auth.models import AnonymousUser
from django.db.models import QuerySet

from accounts.models import RevelUser
from events.models.mixins import ResourceVisibility
from polls.models import Poll

if t.TYPE_CHECKING:
    from events.models.organization import MembershipTier

UserLike = RevelUser | AnonymousUser


def _is_staff_or_owner(user: UserLike, poll: Poll) -> bool:
    """Return True if ``user`` is a superuser, Django staff, the org owner, or org staff."""
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
) -> bool:
    """Apply :class:`ResourceVisibility` semantics for a poll, with optional tier restriction.

    Args:
        user: The user being checked.
        poll: The poll whose audience is being evaluated.
        visibility: One of :class:`ResourceVisibility` values (e.g. ``poll.vote_visibility``).
        membership_tiers: Queryset of ``MembershipTier`` rows (typically
            ``poll.vote_membership_tiers.all()`` or ``poll.result_membership_tiers.all()``).
            Only consulted when ``visibility == MEMBERS_ONLY``.
    """
    # Staff/owner always pass.
    if _is_staff_or_owner(user, poll):
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


def can_vote(user: UserLike, poll: Poll) -> bool:
    """Whether ``user`` is currently eligible to cast a vote on ``poll``.

    Anonymous users are never eligible (poll voting requires authentication).
    Status (``poll.status == OPEN``) is NOT checked here — callers verify it separately.
    """
    if user.is_anonymous:
        return False
    return _passes_visibility(user, poll, poll.vote_visibility, poll.vote_membership_tiers.all())


def can_see_poll(user: UserLike, poll: Poll) -> bool:
    """Whether ``user`` can see that the poll exists (used for listing/detail).

    A user passes if they could vote OR could see results OR has already voted on it.
    """
    if can_vote(user, poll):
        return True
    if _passes_visibility(user, poll, poll.result_visibility, poll.result_membership_tiers.all()):
        return True
    return user_has_voted(user, poll)


def can_see_results(user: UserLike, poll: Poll) -> bool:
    """Whether ``user`` can currently view aggregate results for ``poll``."""
    # Staff/owner always see results (anonymity governs identity exposure separately).
    if _is_staff_or_owner(user, poll):
        return True

    # Result audience check first.
    if not _passes_visibility(user, poll, poll.result_visibility, poll.result_membership_tiers.all()):
        return False

    # Then timing.
    if poll.result_timing == Poll.PollResultTiming.NEVER:
        return False
    if poll.result_timing == Poll.PollResultTiming.AFTER_CLOSE:
        return poll.status == Poll.PollStatus.CLOSED
    if poll.result_timing == Poll.PollResultTiming.AFTER_VOTE:
        return user_has_voted(user, poll)
    return False
