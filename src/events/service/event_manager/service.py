"""EligibilityService for checking user eligibility for events."""

import functools
import typing as t
import uuid
from collections import defaultdict

from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.utils import timezone

from accounts.models import RevelUser
from events import models
from events.models import (
    Blacklist,
    EventInvitationRequest,
    EventRSVP,
    MembershipTier,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
    WhitelistRequest,
)
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

from .gates import ELIGIBILITY_GATES, AvailabilityGate, BaseEligibilityGate
from .types import EventUserEligibility

if t.TYPE_CHECKING:
    from events.service.membership_manager import MembershipNextStep


class EligibilityService:
    """The Eligibility Service Class.

    This class is responsible for checking if a user is eligible to participate in an event.
    Most notably, it performs eligibility checks, raises explicit errors with relevant information.
    """

    def __init__(self, user: RevelUser, event: models.Event) -> None:
        """Initialize the service, pre-fetching all required data in a highly optimized way.

        This ensures all subsequent checks are performed in-memory without further database hits.
        """
        # First, get the user with all their relevant submissions and evaluations.
        # This is a separate query but is necessary and efficient for the questionnaire check.
        self.user = RevelUser.objects.prefetch_related(
            Prefetch(
                "questionnaire_submissions",
                queryset=QuestionnaireSubmission.objects.ready().select_related("evaluation"),
            )
        ).get(pk=user.pk)

        event_link_filter = Q(events=event)
        if event.event_series:
            event_link_filter |= Q(event_series=event.event_series)

        questionnaire_filter = event_link_filter & Q(
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
            questionnaire__status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )

        # Now, fetch the event and all its related data in a single, optimized query.
        self.event = (
            models.Event.objects.select_related("organization")
            .prefetch_related(
                Prefetch("tickets", queryset=models.Ticket.objects.only("id", "event_id", "user_id", "status")),
                Prefetch(
                    "invitations",
                    queryset=models.EventInvitation.objects.filter(user=user).prefetch_related("tiers"),
                ),
                Prefetch(
                    "invitation_requests",
                    queryset=EventInvitationRequest.objects.filter(user=user),
                ),
                # Use .only("id") to fetch lightweight model instances with only the ID populated.
                Prefetch(
                    "organization__staff_members",
                    queryset=RevelUser.objects.only("id"),
                    to_attr="staff_members_prefetched",
                ),
                Prefetch(
                    "organization__members",
                    queryset=RevelUser.objects.filter(
                        organization_memberships__status=OrganizationMember.MembershipStatus.ACTIVE
                    ).only("id"),
                    to_attr="active_members_prefetched",
                ),
                Prefetch(
                    "organization__memberships",
                    queryset=OrganizationMember.objects.only("id", "user_id", "organization_id", "status"),
                    to_attr="all_memberships_prefetched",
                ),
                Prefetch(
                    "organization__org_questionnaires",
                    queryset=models.OrganizationQuestionnaire.objects.filter(questionnaire_filter).distinct(),
                    to_attr="relevant_org_questionnaires",
                ),
                Prefetch(
                    "rsvps",
                    queryset=models.EventRSVP.objects.filter(status=EventRSVP.RsvpStatus.YES),
                ),
                "ticket_tiers",  # Prefetch ticket tiers for sales window checking
            )
            .annotate(user_is_waitlisted=Exists(models.EventWaitList.objects.filter(event=OuterRef("pk"), user=user)))
            .annotate(
                pending_waitlist_offer_count=Count(
                    "waitlist_offers",
                    filter=Q(
                        waitlist_offers__status=models.WaitlistOffer.WaitlistOfferStatus.PENDING,
                        waitlist_offers__expires_at__gt=timezone.now(),
                        waitlist_offers__is_cutoff_batch=False,
                    ),
                    distinct=True,
                )
            )
            .get(pk=event.pk)
        )

        self.active_waitlist_offer = (
            user.waitlist_offers.filter(
                event=event,
                status=models.WaitlistOffer.WaitlistOfferStatus.PENDING,
                expires_at__gt=timezone.now(),
            )
            .order_by("-created_at")
            .first()
        )

        # Create sets of IDs from the prefetched lightweight model instances.
        self.staff_ids = {staff.id for staff in self.event.organization.staff_members_prefetched}  # type: ignore[attr-defined]
        self.member_ids = {member.id for member in self.event.organization.active_members_prefetched}  # type: ignore[attr-defined]

        # Build a map of user_id -> membership status for all memberships
        self.membership_status_map: dict[uuid.UUID, OrganizationMember.MembershipStatus] = {}
        for membership in self.event.organization.all_memberships_prefetched:  # type: ignore[attr-defined]
            self.membership_status_map[membership.user_id] = membership.status

        self.invitation = self.event.invitations.first()
        self.invitation_request = self.event.invitation_requests.first()
        self.submission_map: dict[uuid.UUID, list[QuestionnaireSubmission]] = defaultdict(list)
        for sub in self.user.questionnaire_submissions.all():
            self.submission_map[sub.questionnaire_id].append(sub)

        # Blacklist/whitelist data for BlacklistGate
        self._setup_blacklist_data(user)

        self._gates: list[BaseEligibilityGate] = [gate(self) for gate in ELIGIBILITY_GATES]

    def check_eligibility(self, bypass: bool = False, skip_availability: bool = False) -> EventUserEligibility:
        """Check eligibility using the fully prefetched, in-memory data.

        Every gate reads prefetched, in-memory data, with one exception: a non-member
        on a members-only event triggers the lazy ``pending_membership_step`` lookup.

        Args:
            bypass: When True, skip all gates and return an allowed result.
            skip_availability: When True, skip the capacity/availability gate. Used for
                RSVP writes that don't claim a seat (NO/MAYBE), which must not be blocked
                by a full event — every other eligibility gate still applies.

        Returns:
            EventUserEligibility with the result of the eligibility check.
        """
        if bypass:
            return EventUserEligibility(allowed=True, event_id=self.event.pk)

        for gate in self._gates:
            if skip_availability and isinstance(gate, AvailabilityGate):
                continue
            if result := gate.check():
                return result

        result = EventUserEligibility(allowed=True, event_id=self.event.id)
        if self.active_waitlist_offer is not None:
            from events.utils import get_event_timezone, get_user_timezone

            target_tz = get_user_timezone(self.user) or get_event_timezone(self.event)
            result.active_offer_expires_at = self.active_waitlist_offer.expires_at.astimezone(target_tz)
        return result

    @functools.cached_property
    def event_submission_map(self) -> dict[uuid.UUID, list[QuestionnaireSubmission]]:
        """Build event-scoped submission map for per_event questionnaires.

        Lazily evaluated — only queried when a per_event questionnaire is encountered.
        """
        from events.models import EventQuestionnaireSubmission

        event_questionnaire_submissions = (
            EventQuestionnaireSubmission.objects.filter(
                user=self.user,
                event__pk=self.event.pk,
                submission__status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            )
            .select_related("submission__evaluation")
            .order_by("-submission__submitted_at")
        )
        result: dict[uuid.UUID, list[QuestionnaireSubmission]] = defaultdict(list)
        for evt_sub in event_questionnaire_submissions:
            result[evt_sub.questionnaire_id].append(evt_sub.submission)
        return dict(result)

    @functools.cached_property
    def pending_membership_step(self) -> "MembershipNextStep | None":
        """The waiting step of the user's in-flight membership attempt in the event's org, if any.

        Delegates to :class:`MembershipEligibilityService` (the state machine behind the
        org membership page) so the event surfaces never contradict it. Returns only
        ``WAIT_FOR_APPROVAL`` or ``WAIT_FOR_QUESTIONNAIRE_EVALUATION``; anything else the
        user must act on (submit/retake a questionnaire, reapply, ...) yields ``None`` and
        the event keeps pointing at ``BECOME_MEMBER``.

        Lazily evaluated — only queried when a non-member hits a members-only event.
        """
        from events.service.membership_manager import MembershipEligibilityService, MembershipNextStep

        organization = self.event.organization
        # Latest application per tier, mirroring MembershipEligibilityService.applications_by_tier.
        applications = list(
            OrganizationMembershipRequest.objects.filter(organization=organization, user=self.user)
            .select_related("tier__membership_questionnaire")
            .order_by("tier_id", "-created_at")
            .distinct("tier_id")
        )
        candidates: list[MembershipTier | None] = [
            app.tier for app in applications if app.status == OrganizationMembershipRequest.Status.PENDING
        ]
        if not candidates:
            # No application in flight: the user may have submitted a membership
            # questionnaire on its own. Check it against the tier it would apply to.
            candidates = self._tiers_awaiting_questionnaire_review({app.tier_id for app in applications})[:1]

        steps = {
            MembershipEligibilityService(user=self.user, organization=organization, tier=tier)
            .check_eligibility()
            .next_step
            for tier in candidates
        }
        for step in (MembershipNextStep.WAIT_FOR_APPROVAL, MembershipNextStep.WAIT_FOR_QUESTIONNAIRE_EVALUATION):
            if step in steps:
                return step
        return None

    def _tiers_awaiting_questionnaire_review(
        self, applied_tier_ids: set[uuid.UUID | None]
    ) -> list[MembershipTier | None]:
        """Tiers whose resolved membership questionnaire has an unreviewed latest submission by the user.

        Tiers the user has applied to come first, so a rejected/cancelled application
        is what the membership state machine judges. Returns ``[]`` without querying
        when the user has no unreviewed submission at all (the common case).
        """
        from events.service.membership_manager import resolve_membership_questionnaire

        pending = QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW
        unreviewed_ids = {
            questionnaire_id
            for questionnaire_id, submissions in self.submission_map.items()
            if (evaluation := getattr(submissions[0], "evaluation", None)) is None or evaluation.status == pending
        }
        if not unreviewed_ids:
            return []

        organization = self.event.organization
        tiers: list[MembershipTier | None] = list(
            MembershipTier.objects.filter(organization=organization)
            .select_related("membership_questionnaire")
            .order_by("display_order")
        )
        if None in applied_tier_ids:
            tiers.append(None)  # legacy tier-less application → org default questionnaire

        def awaits_review(tier: MembershipTier | None) -> bool:
            oq = resolve_membership_questionnaire(organization, tier)
            return oq is not None and oq.requires_evaluation and oq.questionnaire_id in unreviewed_ids

        matching = [tier for tier in tiers if awaits_review(tier)]
        return sorted(matching, key=lambda tier: (tier.pk if tier else None) not in applied_tier_ids)

    def overrides_max_attendees(self) -> bool:
        """Check if invitation overrides max attendees."""
        return getattr(self.invitation, "overrides_max_attendees", False)

    def waives_membership_required(self) -> bool:
        """Check if invitation waives membership requirement."""
        return getattr(self.invitation, "waives_membership_required", False)

    def waives_purchase(self) -> bool:
        """Check if invitation waives purchase requirement - grants complimentary access."""
        return getattr(self.invitation, "waives_purchase", False)

    def _setup_blacklist_data(self, user: RevelUser) -> None:
        """Set up blacklist/whitelist data for BlacklistGate.

        This method performs the necessary database queries to check blacklist status.
        While we prefer zero-query checks, blacklist checking requires some queries
        that cannot be efficiently prefetched in the main event query.
        """
        from events.service.blacklist_service import (
            check_user_hard_blacklisted,
            get_fuzzy_blacklist_matches,
        )
        from events.service.whitelist_service import (
            get_whitelist_request,
            is_user_whitelisted,
        )

        org = self.event.organization

        # Check hard blacklist (FK match or hard identifier match)
        self.is_hard_blacklisted = check_user_hard_blacklisted(user, org)

        # If hard blacklisted, no need to check fuzzy matches
        if self.is_hard_blacklisted:
            self.fuzzy_matched_blacklist_entries: list[Blacklist] = []
            self.is_whitelisted = False
            self.whitelist_request: WhitelistRequest | None = None
            return

        # Check fuzzy matches (name-based)
        fuzzy_matches = get_fuzzy_blacklist_matches(user, org)
        self.fuzzy_matched_blacklist_entries = [entry for entry, _score in fuzzy_matches]

        # If no fuzzy matches, no need to check whitelist
        if not self.fuzzy_matched_blacklist_entries:
            self.is_whitelisted = False
            self.whitelist_request = None
            return

        # Check whitelist status
        self.is_whitelisted = is_user_whitelisted(user, org)
        self.whitelist_request = get_whitelist_request(user, org) if not self.is_whitelisted else None
