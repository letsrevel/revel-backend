"""Poll model — wraps a Questionnaire with poll-specific concerns.

See `docs/superpowers/specs/2026-05-21-polls-design.md` for the design rationale.
"""

import typing as t
import uuid

from django.db import models
from django.db.models import Exists, OuterRef, Q

from common.models import TimeStampedModel
from events.models.mixins import ResourceVisibility
from polls.exceptions import PollAnonymityImmutableError

if t.TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser

    from accounts.models import RevelUser


class PollQuerySet(models.QuerySet["Poll"]):
    """Custom queryset for :class:`Poll` with visibility-aware listings."""

    def with_user_annotations(self, user: "RevelUser | AnonymousUser") -> "PollQuerySet":
        """Annotate each row with per-user signals consumed by schema resolvers.

        Adds the following boolean ``Exists()`` annotations (or constant Q() flags)
        so :class:`polls.schema.PollListItemSchema` resolvers can compute
        ``user_has_voted`` / ``user_can_vote`` / ``user_can_see_results`` without
        a per-row round trip:

        * ``_user_has_voted`` — a READY :class:`QuestionnaireSubmission` exists.
        * ``_is_org_owner`` — the user owns the poll's organization.
        * ``_is_org_staff_member`` — an :class:`OrganizationStaff` row links the user.
        * ``_is_org_member`` — an active :class:`OrganizationMember` row exists.
        * ``_user_member_tier_id`` — the user's tier id in the poll's org (or NULL).
        * ``_has_ticket`` — a non-cancelled :class:`Ticket` for the linked event.
        * ``_has_rsvp`` — a YES :class:`EventRSVP` for the linked event.
        * ``_has_invitation`` — an :class:`EventInvitation` for the linked event.

        Anonymous users get no annotations: the resolvers default missing
        attributes to ``False``.
        """
        if user.is_anonymous:
            return self

        from django.db.models import Subquery

        from events.models.invitation import EventInvitation
        from events.models.organization import OrganizationMember, OrganizationStaff
        from events.models.rsvp import EventRSVP
        from events.models.ticket import Ticket
        from questionnaires.models import QuestionnaireSubmission

        return self.annotate(
            _user_has_voted=Exists(
                QuestionnaireSubmission.objects.filter(
                    user=user,
                    questionnaire=OuterRef("questionnaire"),
                    status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
                )
            ),
            _is_org_owner=Exists(self.model.objects.filter(pk=OuterRef("pk"), organization__owner=user)),
            _is_org_staff_member=Exists(
                OrganizationStaff.objects.filter(organization=OuterRef("organization"), user=user)
            ),
            _is_org_member=Exists(
                OrganizationMember.objects.for_visibility().filter(organization=OuterRef("organization"), user=user)
            ),
            _user_member_tier_id=Subquery(
                OrganizationMember.objects.for_visibility()
                .filter(organization=OuterRef("organization"), user=user)
                .values("tier_id")[:1]
            ),
            _has_ticket=Exists(
                Ticket.objects.filter(user=user, event=OuterRef("event")).exclude(status=Ticket.TicketStatus.CANCELLED)
            ),
            _has_rsvp=Exists(
                EventRSVP.objects.filter(user=user, event=OuterRef("event"), status=EventRSVP.RsvpStatus.YES)
            ),
            _has_invitation=Exists(EventInvitation.objects.filter(user=user, event=OuterRef("event"))),
        )

    def for_user(self, user: "RevelUser | AnonymousUser") -> "PollQuerySet":
        """Return polls visible to ``user`` per the listing rule.

        A poll is visible iff the user passes ``vote_visibility`` OR
        ``result_visibility``, OR has already voted on it. Owners and
        org staff see everything in their org including drafts; Django
        superusers/staff see everything system-wide.

        Tier-restricted MEMBERS_ONLY polls are not refined here — the
        eligibility service applies the per-poll tier check. False-positive
        listing for tier-restricted polls is acceptable and matches the
        listings vs. fine-grained access split used elsewhere (e.g., Event).
        """
        from events.models.invitation import EventInvitation
        from events.models.organization import OrganizationMember, OrganizationStaff
        from events.models.rsvp import EventRSVP
        from events.models.ticket import Ticket
        from questionnaires.models import QuestionnaireSubmission

        # Django superusers/staff see everything (regardless of status).
        if not user.is_anonymous and (getattr(user, "is_superuser", False) or getattr(user, "is_staff", False)):
            return self.all()

        if user.is_anonymous:
            # Anonymous: only PUBLIC/UNLISTED polls in non-DRAFT statuses.
            return self.filter(
                Q(vote_visibility__in=ResourceVisibility.publicly_accessible())
                | Q(result_visibility__in=ResourceVisibility.publicly_accessible()),
            ).exclude(status=Poll.PollStatus.DRAFT)

        # Authenticated, non-Django-staff user.
        # --- Get banned and blacklisted organization IDs ---
        # Users banned/blacklisted from an organization cannot see its polls, even if public
        # (mirrors EventQuerySet.for_user).
        from events.utils.blacklist import get_hard_blacklisted_org_ids

        banned_org_ids = OrganizationMember.objects.filter(
            user=user, status=OrganizationMember.MembershipStatus.BANNED
        ).values_list("organization_id", flat=True)

        blacklisted_org_ids = get_hard_blacklisted_org_ids(user)

        excluded_org_ids = set(banned_org_ids) | set(blacklisted_org_ids)

        org_owner_q = Q(organization__owner=user)
        org_staff_q = Exists(OrganizationStaff.objects.filter(organization=OuterRef("organization"), user=user))
        member_q = Exists(
            OrganizationMember.objects.for_visibility().filter(organization=OuterRef("organization"), user=user)
        )
        ticket_q = Exists(
            Ticket.objects.filter(user=user, event=OuterRef("event")).exclude(status=Ticket.TicketStatus.CANCELLED)
        )
        rsvp_q = Exists(EventRSVP.objects.filter(user=user, event=OuterRef("event"), status=EventRSVP.RsvpStatus.YES))
        invite_q = Exists(EventInvitation.objects.filter(user=user, event=OuterRef("event")))
        voted_q = Exists(
            QuestionnaireSubmission.objects.filter(
                user=user,
                questionnaire=OuterRef("questionnaire"),
                status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            )
        )

        passes_vis = (
            Q(vote_visibility__in=ResourceVisibility.publicly_accessible())
            | Q(result_visibility__in=ResourceVisibility.publicly_accessible())
            | (Q(vote_visibility=ResourceVisibility.MEMBERS_ONLY) & member_q)
            | (Q(result_visibility=ResourceVisibility.MEMBERS_ONLY) & member_q)
            | (
                Q(
                    vote_visibility__in=[
                        ResourceVisibility.PRIVATE,
                        ResourceVisibility.ATTENDEES_ONLY,
                    ]
                )
                & (ticket_q | rsvp_q)
            )
            | (
                Q(
                    result_visibility__in=[
                        ResourceVisibility.PRIVATE,
                        ResourceVisibility.ATTENDEES_ONLY,
                    ]
                )
                & (ticket_q | rsvp_q)
            )
            | (Q(vote_visibility=ResourceVisibility.PRIVATE) & invite_q)
            | (Q(result_visibility=ResourceVisibility.PRIVATE) & invite_q)
        )

        is_org_owner_or_staff = org_owner_q | org_staff_q

        # Owners/org staff: everything in their org including DRAFT.
        # Other authenticated users: passes visibility OR has voted; DRAFT hidden.
        # Banned/blacklisted users do not see any poll from those orgs.
        return (
            self.filter(is_org_owner_or_staff | ((passes_vis | voted_q) & ~Q(status=Poll.PollStatus.DRAFT)))
            .exclude(organization_id__in=excluded_org_ids)
            .distinct()
        )


class PollManager(models.Manager["Poll"]):
    """Manager exposing :meth:`PollQuerySet.for_user`."""

    def get_queryset(self) -> PollQuerySet:
        """Return a :class:`PollQuerySet` bound to this manager's database."""
        return PollQuerySet(self.model, using=self._db)

    def for_user(self, user: "RevelUser | AnonymousUser") -> PollQuerySet:
        """Proxy to :meth:`PollQuerySet.for_user`."""
        return self.get_queryset().for_user(user)

    def with_user_annotations(self, user: "RevelUser | AnonymousUser") -> PollQuerySet:
        """Proxy to :meth:`PollQuerySet.with_user_annotations`."""
        return self.get_queryset().with_user_annotations(user)


class Poll(TimeStampedModel):
    """A poll backed by a :class:`questionnaires.Questionnaire`.

    The Poll wraps a Questionnaire with poll-specific lifecycle, audience and
    anonymity concerns. The actual votes are stored as ``QuestionnaireSubmission``
    rows against the wrapped questionnaire — no separate vote table is needed.
    """

    class PollStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    class PollResultTiming(models.TextChoices):
        AFTER_VOTE = "after_vote", "After vote"
        AFTER_CLOSE = "after_close", "After close"
        NEVER = "never", "Never"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    questionnaire = models.OneToOneField(
        "questionnaires.Questionnaire",
        on_delete=models.CASCADE,
        related_name="poll",
    )
    organization = models.ForeignKey(
        "events.Organization",
        on_delete=models.CASCADE,
        related_name="polls",
    )
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="polls",
    )

    status = models.CharField(
        choices=PollStatus.choices,
        max_length=10,
        default=PollStatus.DRAFT,
        db_index=True,
    )
    opened_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    allow_vote_changes = models.BooleanField(default=False)

    vote_visibility = models.CharField(
        choices=ResourceVisibility.choices,
        max_length=20,
        db_index=True,
    )
    result_visibility = models.CharField(
        choices=ResourceVisibility.choices,
        max_length=20,
        default=ResourceVisibility.STAFF_ONLY,
    )
    result_timing = models.CharField(
        choices=PollResultTiming.choices,
        max_length=20,
        default=PollResultTiming.NEVER,
    )

    vote_membership_tiers = models.ManyToManyField(
        "events.MembershipTier",
        blank=True,
        related_name="voteable_polls",
    )
    result_membership_tiers = models.ManyToManyField(
        "events.MembershipTier",
        blank=True,
        related_name="results_visible_polls",
    )

    staff_anonymous = models.BooleanField(default=True)
    public_anonymous = models.BooleanField(default=True)

    objects: t.ClassVar[PollManager] = PollManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~models.Q(
                        result_visibility__in=[
                            ResourceVisibility.PUBLIC.value,
                            ResourceVisibility.UNLISTED.value,
                        ]
                    )
                    | models.Q(public_anonymous=True)
                ),
                name="poll_public_results_must_be_anonymous",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        ~models.Q(
                            vote_visibility__in=[
                                ResourceVisibility.PRIVATE.value,
                                ResourceVisibility.ATTENDEES_ONLY.value,
                            ]
                        )
                        & ~models.Q(
                            result_visibility__in=[
                                ResourceVisibility.PRIVATE.value,
                                ResourceVisibility.ATTENDEES_ONLY.value,
                            ]
                        )
                    )
                    | models.Q(event__isnull=False)
                ),
                name="poll_restricted_visibility_requires_event",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Poll<{self.id}> ({self.status})"

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Persist the poll, enforcing anonymity immutability after creation."""
        if self.pk is not None:
            old = Poll.objects.filter(pk=self.pk).only("staff_anonymous", "public_anonymous").first()
            if old is not None and (
                old.staff_anonymous != self.staff_anonymous or old.public_anonymous != self.public_anonymous
            ):
                raise PollAnonymityImmutableError()
        super().save(*args, **kwargs)
