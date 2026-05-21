"""Polls API controller — read endpoints (list / detail / results)."""

from uuid import UUID

from django.contrib.auth.models import AnonymousUser
from django.shortcuts import get_object_or_404
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from accounts.models import RevelUser
from common.authentication import OptionalAuth
from common.controllers import UserAwareController
from common.throttling import AnonDefaultThrottle, UserDefaultThrottle
from events.models.mixins import ResourceVisibility
from polls.models import Poll
from polls.schema import (
    PollDetailSchema,
    PollListItemSchema,
    PollResultsSchema,
)
from polls.service import eligibility
from polls.service.aggregation import compute_poll_results

UserLike = RevelUser | AnonymousUser


@api_controller(
    "/polls",
    tags=["Polls"],
    auth=OptionalAuth(),
    throttle=[AnonDefaultThrottle(), UserDefaultThrottle()],
)
class PollController(UserAwareController):
    """Read endpoints for polls.

    Anonymous access is permitted via :class:`OptionalAuth`; visibility is
    enforced by :meth:`Poll.objects.for_user` and the eligibility service.
    """

    @route.get("/", response=PaginatedResponseSchema[PollListItemSchema])
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_polls(
        self,
        organization_id: UUID | None = None,
        event_id: UUID | None = None,
        status: Poll.PollStatus | None = None,
    ) -> list[PollListItemSchema]:
        """List polls visible to the current user, optionally filtered."""
        user = self.maybe_user()
        qs = (
            Poll.objects.for_user(user)
            .select_related("organization", "event", "questionnaire")
            .prefetch_related("vote_membership_tiers", "result_membership_tiers")
        )
        if organization_id is not None:
            qs = qs.filter(organization_id=organization_id)
        if event_id is not None:
            qs = qs.filter(event_id=event_id)
        if status is not None:
            qs = qs.filter(status=status)
        return [self._to_list_item(poll, user) for poll in qs]

    @route.get("/{poll_id}/", response=PollDetailSchema)
    def get_poll(self, poll_id: UUID) -> PollDetailSchema:
        """Retrieve a single poll, including user-specific flags and (when allowed) results."""
        user = self.maybe_user()
        poll = get_object_or_404(
            Poll.objects.for_user(user)
            .select_related("organization", "event", "questionnaire")
            .prefetch_related("vote_membership_tiers", "result_membership_tiers"),
            pk=poll_id,
        )
        return self._to_detail(poll, user)

    @route.get("/{poll_id}/results", response=PollResultsSchema)
    def get_poll_results(self, poll_id: UUID) -> PollResultsSchema:
        """Return aggregated poll results, honouring visibility and timing rules."""
        user = self.maybe_user()
        poll = get_object_or_404(Poll, pk=poll_id)
        if not eligibility.can_see_results(user, poll):
            raise HttpError(403, "You are not allowed to see the results for this poll.")
        return compute_poll_results(poll, viewer_sees_identity=self._viewer_sees_identity(poll, user))

    # ------------------------------------------------------------------ helpers

    def _to_list_item(self, poll: Poll, user: UserLike) -> PollListItemSchema:
        return PollListItemSchema(
            id=poll.id,
            organization_id=poll.organization_id,
            event_id=poll.event_id,
            questionnaire_name=poll.questionnaire.name,
            status=Poll.PollStatus(poll.status),
            opened_at=poll.opened_at,
            closes_at=poll.closes_at,
            closed_at=poll.closed_at,
            vote_visibility=ResourceVisibility(poll.vote_visibility),
            result_visibility=ResourceVisibility(poll.result_visibility),
            user_has_voted=eligibility.user_has_voted(user, poll),
            user_can_vote=(poll.status == Poll.PollStatus.OPEN and eligibility.can_vote(user, poll)),
            user_can_see_results=eligibility.can_see_results(user, poll),
        )

    def _to_detail(self, poll: Poll, user: UserLike) -> PollDetailSchema:
        user_can_see_results = eligibility.can_see_results(user, poll)
        results: PollResultsSchema | None = None
        if user_can_see_results:
            results = compute_poll_results(poll, viewer_sees_identity=self._viewer_sees_identity(poll, user))
        return PollDetailSchema(
            id=poll.id,
            organization_id=poll.organization_id,
            event_id=poll.event_id,
            questionnaire_id=poll.questionnaire_id,
            status=Poll.PollStatus(poll.status),
            opened_at=poll.opened_at,
            closes_at=poll.closes_at,
            closed_at=poll.closed_at,
            allow_vote_changes=poll.allow_vote_changes,
            vote_visibility=ResourceVisibility(poll.vote_visibility),
            result_visibility=ResourceVisibility(poll.result_visibility),
            result_timing=Poll.PollResultTiming(poll.result_timing),
            staff_anonymous=poll.staff_anonymous,
            public_anonymous=poll.public_anonymous,
            vote_membership_tier_ids=list(poll.vote_membership_tiers.values_list("id", flat=True)),
            result_membership_tier_ids=list(poll.result_membership_tiers.values_list("id", flat=True)),
            user_has_voted=eligibility.user_has_voted(user, poll),
            user_can_vote=(poll.status == Poll.PollStatus.OPEN and eligibility.can_vote(user, poll)),
            user_can_see_results=user_can_see_results,
            questionnaire=None,  # populated later when we wire questionnaire response into detail
            results=results,
        )

    def _viewer_sees_identity(self, poll: Poll, user: UserLike) -> bool:
        if eligibility._is_staff_or_owner(user, poll):
            return not poll.staff_anonymous
        return not poll.public_anonymous


__all__ = ["PollController"]
