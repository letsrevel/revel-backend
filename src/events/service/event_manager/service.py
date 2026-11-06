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
    MembershipSubscriptionPlan,
    MembershipTier,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
    WhitelistRequest,
)
from events.utils.membership_access import is_eligible_status
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

    def __init__(
        self,
        user: RevelUser,
        event: models.Event,
        *,
        questionnaire_scope: t.Literal["event", "series"] = "event",
    ) -> None:
        """Initialize the service, pre-fetching all required data in a highly optimized way.

        This ensures all subsequent checks are performed in-memory without further database hits.

        Args:
            user: The user whose eligibility is checked.
            event: The event to check against.
            questionnaire_scope: Which admission questionnaires apply. ``"event"`` (default) uses
                those linked to the event or its series; ``"series"`` uses only those linked to
                the event's series (none if the event has no series).
        """
        # First, get the user with all their relevant submissions and evaluations.
        # This is a separate query but is necessary and efficient for the questionnaire check.
        self.user = RevelUser.objects.prefetch_related(
            Prefetch(
                "questionnaire_submissions",
                queryset=QuestionnaireSubmission.objects.ready().select_related("evaluation"),
            )
        ).get(pk=user.pk)

        self.questionnaire_scope = questionnaire_scope
        if questionnaire_scope == "series":
            event_link_filter = Q(event_series=event.event_series) if event.event_series else Q(pk__in=[])
        else:
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
                    "organization__memberships",
                    queryset=OrganizationMember.objects.filter(user=user).only(
                        "id", "user_id", "organization_id", "status"
                    ),
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

        # Build a map of user_id -> membership status for all memberships
        self.membership_status_map: dict[uuid.UUID, OrganizationMember.MembershipStatus] = {}
        for membership in self.event.organization.all_memberships_prefetched:  # type: ignore[attr-defined]
            self.membership_status_map[membership.user_id] = membership.status
        self.member_ids = {uid for uid, status in self.membership_status_map.items() if is_eligible_status(status)}

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
        return self._build_event_submission_map(Q(event__pk=self.event.pk))

    @functools.cached_property
    def series_event_submission_map(self) -> dict[uuid.UUID, list[QuestionnaireSubmission]]:
        """Like :attr:`event_submission_map`, but spanning every event of this event's series.

        Used in series scope, where one per_event submission made for any event of the
        series covers the pass (#1321). Empty if the event has no series.
        """
        if not self.event.event_series_id:
            return {}
        return self._build_event_submission_map(Q(event__event_series_id=self.event.event_series_id))

    def _build_event_submission_map(self, event_filter: Q) -> dict[uuid.UUID, list[QuestionnaireSubmission]]:
        from events.models import EventQuestionnaireSubmission

        event_questionnaire_submissions = (
            EventQuestionnaireSubmission.objects.filter(
                event_filter,
                user=self.user,
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
            .select_related("tier__membership_questionnaire", "plan")
            .order_by("tier_id", "-created_at")
            .distinct("tier_id")
        )
        # (tier, plan) pairs: a paid application must be judged with its plan, or
        # TierAvailabilityGate blocks a monetized tier before the waiting gates run.
        candidates: list[tuple[MembershipTier | None, MembershipSubscriptionPlan | None]] = [
            (app.tier, app.plan) for app in applications if app.status == OrganizationMembershipRequest.Status.PENDING
        ]
        if not candidates:
            # No application in flight: the user may have submitted a membership
            # questionnaire on its own. Check it against the tier it would apply to.
            candidates = [
                (tier, self._any_active_plan(tier)) for tier in self._tiers_awaiting_questionnaire_review(applications)
            ]

        steps = {
            MembershipEligibilityService(user=self.user, organization=organization, tier=tier, plan=plan)
            .check_eligibility()
            .next_step
            for tier, plan in candidates
        }
        for step in (MembershipNextStep.WAIT_FOR_APPROVAL, MembershipNextStep.WAIT_FOR_QUESTIONNAIRE_EVALUATION):
            if step in steps:
                return step
        return None

    def _tiers_awaiting_questionnaire_review(
        self, applications: list[OrganizationMembershipRequest]
    ) -> list[MembershipTier | None]:
        """One tier per membership questionnaire the user has an unreviewed latest submission to.

        For each such questionnaire, the tier of the user's most recent application wins
        (then display order), so their latest decision on it, e.g. a rejection, is what
        the membership state machine judges. Returns ``[]`` without querying
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
        # Newest application first; tiers never applied to keep display order after them.
        recency = {
            app.tier_id: rank for rank, app in enumerate(sorted(applications, key=lambda a: a.created_at, reverse=True))
        }
        if None in recency or not tiers:
            tiers.append(None)  # tier-less application (or tier-less org) → org default questionnaire

        tiers.sort(key=lambda tier: recency.get(tier.pk if tier else None, len(recency)))
        tier_by_questionnaire: dict[uuid.UUID, MembershipTier | None] = {}
        for tier in tiers:
            oq = resolve_membership_questionnaire(organization, tier)
            if oq is not None and oq.requires_evaluation and oq.questionnaire_id in unreviewed_ids:
                tier_by_questionnaire.setdefault(oq.questionnaire_id, tier)
        return list(tier_by_questionnaire.values())

    @staticmethod
    def _any_active_plan(tier: MembershipTier | None) -> MembershipSubscriptionPlan | None:
        """An active plan of a monetized tier, so a standalone submission is judged on the paid path.

        Which plan is irrelevant here: plans only matter to TierAvailabilityGate (before the
        questionnaire/approval gates) and PaymentReadyGate (after them).
        """
        if tier is None:
            return None
        return MembershipSubscriptionPlan.objects.filter(tier=tier, is_active=True).first()

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
