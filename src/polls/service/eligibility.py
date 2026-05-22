"""Eligibility helpers for polls.

Mirrors the visibility model used by
:meth:`events.models.event.Event._compute_can_user_see_address`.
Pure logic — no DB writes. All functions take an already-resolved
:class:`polls.models.Poll` instance and a user-like object.
"""

import typing as t
from dataclasses import dataclass, field
from uuid import UUID

from django.contrib.auth.models import AnonymousUser
from django.db.models import QuerySet

from accounts.models import RevelUser
from events.models.mixins import ResourceVisibility
from polls.models import Poll

if t.TYPE_CHECKING:
    from events.models.organization import MembershipTier

UserLike = RevelUser | AnonymousUser


@dataclass(frozen=True)
class _BulkEligibilityContext:
    """Pre-computed per-user data used by :func:`bulk_eligibility_flags`.

    All fields are sets of IDs (or maps) populated by a single batch of
    queries instead of per-poll lookups, which collapses what was a
    1-3 queries-per-row workload into a constant number of queries.
    """

    is_django_staff: bool = False
    voted_questionnaire_ids: frozenset[UUID] = field(default_factory=frozenset)
    owner_org_ids: frozenset[UUID] = field(default_factory=frozenset)
    staff_org_ids: frozenset[UUID] = field(default_factory=frozenset)
    member_org_to_tier: dict[UUID, UUID | None] = field(default_factory=dict)
    ticketed_event_ids: frozenset[UUID] = field(default_factory=frozenset)
    rsvped_event_ids: frozenset[UUID] = field(default_factory=frozenset)
    invited_event_ids: frozenset[UUID] = field(default_factory=frozenset)


def _empty_context() -> _BulkEligibilityContext:
    return _BulkEligibilityContext()


def build_bulk_context(user: UserLike, polls: list[Poll]) -> _BulkEligibilityContext:
    """Pre-compute the per-user data needed to evaluate eligibility for ``polls``.

    Runs at most one query per relation type regardless of the page size. The
    returned context is consumed by :func:`bulk_user_has_voted`,
    :func:`bulk_can_vote` and :func:`bulk_can_see_results`.
    """
    if not polls:
        return _empty_context()

    if user.is_anonymous:
        # Anonymous users never satisfy any of the per-user signals; the bulk
        # helpers short-circuit on this and skip queries.
        return _empty_context()

    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return _BulkEligibilityContext(is_django_staff=True)

    from events.models.invitation import EventInvitation
    from events.models.organization import OrganizationMember, OrganizationStaff
    from events.models.rsvp import EventRSVP
    from events.models.ticket import Ticket
    from questionnaires.models import QuestionnaireSubmission

    org_ids = {p.organization_id for p in polls}
    event_ids = {p.event_id for p in polls if p.event_id is not None}
    questionnaire_ids = {p.questionnaire_id for p in polls}

    voted_questionnaire_ids = frozenset(
        QuestionnaireSubmission.objects.filter(
            user=user,
            questionnaire_id__in=questionnaire_ids,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        ).values_list("questionnaire_id", flat=True)
    )

    owner_org_ids = frozenset({p.organization_id for p in polls if p.organization.owner_id == user.id})
    staff_org_ids = frozenset(
        OrganizationStaff.objects.filter(organization_id__in=org_ids, user=user).values_list(
            "organization_id", flat=True
        )
    )
    member_org_to_tier: dict[UUID, UUID | None] = dict(
        OrganizationMember.objects.for_visibility()
        .filter(organization_id__in=org_ids, user=user)
        .values_list("organization_id", "tier_id")
    )

    ticketed_event_ids: frozenset[UUID] = frozenset()
    rsvped_event_ids: frozenset[UUID] = frozenset()
    invited_event_ids: frozenset[UUID] = frozenset()
    if event_ids:
        ticketed_event_ids = frozenset(
            Ticket.objects.filter(user=user, event_id__in=event_ids)
            .exclude(status=Ticket.TicketStatus.CANCELLED)
            .values_list("event_id", flat=True)
        )
        rsvped_event_ids = frozenset(
            EventRSVP.objects.filter(user=user, event_id__in=event_ids, status=EventRSVP.RsvpStatus.YES).values_list(
                "event_id", flat=True
            )
        )
        invited_event_ids = frozenset(
            EventInvitation.objects.filter(user=user, event_id__in=event_ids).values_list("event_id", flat=True)
        )

    return _BulkEligibilityContext(
        is_django_staff=False,
        voted_questionnaire_ids=voted_questionnaire_ids,
        owner_org_ids=owner_org_ids,
        staff_org_ids=staff_org_ids,
        member_org_to_tier=member_org_to_tier,
        ticketed_event_ids=ticketed_event_ids,
        rsvped_event_ids=rsvped_event_ids,
        invited_event_ids=invited_event_ids,
    )


def _bulk_is_staff_or_owner(user: UserLike, poll: Poll, ctx: _BulkEligibilityContext) -> bool:
    if user.is_anonymous:
        return False
    if ctx.is_django_staff:
        return True
    return poll.organization_id in ctx.owner_org_ids or poll.organization_id in ctx.staff_org_ids


def _bulk_passes_members_only(
    poll: Poll,
    ctx: _BulkEligibilityContext,
    tier_ids: t.Iterable[UUID],
) -> bool:
    tier_id = ctx.member_org_to_tier.get(poll.organization_id)
    if poll.organization_id not in ctx.member_org_to_tier:
        return False
    tier_list = list(tier_ids)
    if not tier_list:
        return True
    return tier_id in tier_list


def _bulk_passes_event_visibility(poll: Poll, ctx: _BulkEligibilityContext, visibility: str) -> bool:
    if poll.event_id is None:
        return False
    has_ticket = poll.event_id in ctx.ticketed_event_ids
    has_rsvp = poll.event_id in ctx.rsvped_event_ids
    if visibility == ResourceVisibility.ATTENDEES_ONLY:
        return has_ticket or has_rsvp
    if visibility == ResourceVisibility.PRIVATE:
        return has_ticket or has_rsvp or (poll.event_id in ctx.invited_event_ids)
    return False


def _bulk_passes_visibility(
    user: UserLike,
    poll: Poll,
    visibility: str,
    tier_ids: t.Iterable[UUID],
    ctx: _BulkEligibilityContext,
) -> bool:
    if _bulk_is_staff_or_owner(user, poll, ctx):
        return True
    if visibility in ResourceVisibility.publicly_accessible():
        return True
    if user.is_anonymous:
        return False
    if visibility == ResourceVisibility.STAFF_ONLY:
        return False
    if visibility == ResourceVisibility.MEMBERS_ONLY:
        return _bulk_passes_members_only(poll, ctx, tier_ids)
    return _bulk_passes_event_visibility(poll, ctx, visibility)


def bulk_user_has_voted(user: UserLike, poll: Poll, ctx: _BulkEligibilityContext) -> bool:
    """Set-lookup counterpart to :func:`user_has_voted`."""
    if user.is_anonymous:
        return False
    return poll.questionnaire_id in ctx.voted_questionnaire_ids


def bulk_can_vote(user: UserLike, poll: Poll, vote_tier_ids: t.Iterable[UUID], ctx: _BulkEligibilityContext) -> bool:
    """Set-lookup counterpart to :func:`can_vote`."""
    if user.is_anonymous:
        return False
    return _bulk_passes_visibility(user, poll, poll.vote_visibility, vote_tier_ids, ctx)


def bulk_can_see_results(
    user: UserLike, poll: Poll, result_tier_ids: t.Iterable[UUID], ctx: _BulkEligibilityContext
) -> bool:
    """Set-lookup counterpart to :func:`can_see_results`."""
    if _bulk_is_staff_or_owner(user, poll, ctx):
        return True
    if not _bulk_passes_visibility(user, poll, poll.result_visibility, result_tier_ids, ctx):
        return False
    if poll.result_timing == Poll.PollResultTiming.NEVER:
        return False
    if poll.result_timing == Poll.PollResultTiming.AFTER_CLOSE:
        return poll.status == Poll.PollStatus.CLOSED
    if poll.result_timing == Poll.PollResultTiming.AFTER_VOTE:
        return bulk_user_has_voted(user, poll, ctx)
    return False


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
