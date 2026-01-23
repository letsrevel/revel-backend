"""EligibilityService for checking user eligibility for events."""

import uuid
from collections import defaultdict

from django.db.models import Exists, OuterRef, Prefetch, Q

from accounts.models import RevelUser
from events import models
from events.models import (
    Blacklist,
    EventInvitationRequest,
    EventRSVP,
    OrganizationMember,
    OrganizationQuestionnaire,
    WhitelistRequest,
)
from questionnaires.models import Questionnaire, QuestionnaireSubmission

from .gates import ELIGIBILITY_GATES, BaseEligibilityGate
from .types import EventUserEligibility


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
                    queryset=models.EventInvitation.objects.filter(user=user).select_related("tier"),
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
            .get(pk=event.pk)
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

    def check_eligibility(self, bypass: bool = False) -> EventUserEligibility:
        """Check eligibility using the fully prefetched, in-memory data.

        This method SHOULD make ZERO database queries.

        Returns:
            EventUserEligibility with the result of the eligibility check.
        """
        if bypass:
            return EventUserEligibility(allowed=True, event_id=self.event.pk)

        for gate in self._gates:
            if result := gate.check():
                return result

        return EventUserEligibility(allowed=True, event_id=self.event.id)

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
