"""Polls API controller — read and write endpoints."""

import typing as t
from uuid import UUID

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from ninja.errors import HttpError
from ninja import Query
from ninja_extra import api_controller, route
from ninja_extra.pagination import PaginatedResponseSchema

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth, OptionalAuth
from common.controllers import UserAwareController
from common.throttling import AnonDefaultThrottle, UserDefaultThrottle, WriteThrottle
from events.models.mixins import ResourceVisibility
from events.models.organization import Organization
from polls.exceptions import (
    PollNotEligibleError,
    PollNotOpenError,
    PollVoteAlreadyCastError,
    PollVoteChangesNotAllowedError,
)
from polls.models import Poll
from polls.schema import (
    PollCreateSchema,
    PollDetailSchema,
    PollListItemSchema,
    PollReopenSchema,
    PollResultsSchema,
    PollUpdateSchema,
    PollVoteSchema,
)
from polls.service import eligibility, poll_service
from polls.service.aggregation import compute_poll_results

UserLike = RevelUser | AnonymousUser


def _format_validation_error(exc: DjangoValidationError) -> str:
    """Flatten a Django ``ValidationError`` into a single-line message for HTTP errors."""
    if hasattr(exc, "message_dict"):
        parts = [f"{field}: {'; '.join(msgs)}" for field, msgs in exc.message_dict.items()]
        return " | ".join(parts)
    if hasattr(exc, "messages"):
        return "; ".join(exc.messages)
    return str(exc)


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
    def list_polls(
        self,
        organization_id: UUID | None = None,
        event_id: UUID | None = None,
        status: Poll.PollStatus | None = None,
        page: int = Query(1, ge=1),  # type: ignore[type-arg]
        page_size: int = Query(20, ge=1, le=200),  # type: ignore[type-arg]
    ) -> PaginatedResponseSchema[PollListItemSchema]:
        """List polls visible to the current user, optionally filtered.

        Perf notes:

        * Pagination is performed at the DB level: ``Paginator`` slices the
          queryset so only the requested page is loaded.
        * Per-row eligibility flags (``user_has_voted``, ``user_can_vote``,
          ``user_can_see_results``) are precomputed in bulk by
          :func:`polls.service.eligibility.build_bulk_context` against the
          page slice — not the full visible set — so the bulk-query workload
          is bounded by ``page_size``.
        """
        from django.core.paginator import Paginator

        user = self.maybe_user()
        qs = (
            Poll.objects.for_user(user)
            .select_related("organization", "event", "questionnaire")
            .prefetch_related("vote_membership_tiers", "result_membership_tiers")
            # Stable ordering: ``for_user`` does not impose an ordering, but
            # Django's ``Paginator`` requires deterministic slices to avoid
            # split rows between pages.
            .order_by("-created_at", "id")
        )
        if organization_id is not None:
            qs = qs.filter(organization_id=organization_id)
        if event_id is not None:
            qs = qs.filter(event_id=event_id)
        if status is not None:
            qs = qs.filter(status=status)

        paginator = Paginator(qs, page_size)
        # ``Paginator.page`` clamps invalid pages to a 404 via ``EmptyPage`` /
        # ``PageNotAnInteger``; mirror :class:`PageNumberPaginationExtra` by
        # returning an empty result set for an out-of-range page instead.
        if page > paginator.num_pages and paginator.count > 0:
            polls_page: list[Poll] = []
        else:
            polls_page = list(paginator.page(min(page, max(paginator.num_pages, 1))).object_list)

        ctx = eligibility.build_bulk_context(user, polls_page)
        results = [self._to_list_item_bulk(poll, user, ctx) for poll in polls_page]

        next_url = self._page_url(page + 1) if page < paginator.num_pages else None
        previous_url = self._page_url(page - 1) if page > 1 else None
        return PaginatedResponseSchema[PollListItemSchema](
            count=paginator.count,
            next=next_url,
            previous=previous_url,
            results=results,
        )

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

    # ------------------------------------------------------------------ writes

    @route.post(
        "/",
        response={201: PollDetailSchema},
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
    )
    def create_poll(self, payload: PollCreateSchema) -> tuple[int, PollDetailSchema]:
        """Create a new poll under an organization the caller can manage."""
        user = self.user()
        organization = get_object_or_404(Organization, pk=payload.organization_id)
        if not organization.has_org_permission(user.id, "manage_polls"):
            raise HttpError(403, "manage_polls permission required")
        try:
            poll = poll_service.create_poll(payload)
        except DjangoValidationError as exc:
            raise HttpError(422, _format_validation_error(exc))
        return 201, self._to_detail(poll, user)

    @route.patch(
        "/{poll_id}/",
        response=PollDetailSchema,
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
    )
    def patch_poll(self, poll_id: UUID, payload: PollUpdateSchema) -> PollDetailSchema:
        """Apply a partial update to a poll the caller can manage.

        Cross-field constraint violations that can't be caught by the schema
        validator (e.g., clearing ``event_id`` on a poll that already has
        PRIVATE visibility) surface as :class:`DjangoValidationError` from
        the model's ``full_clean`` and are translated to HTTP 422.
        """
        user = self.user()
        poll = get_object_or_404(Poll, pk=poll_id)
        self._require_manage_polls(poll, user)
        try:
            updated = poll_service.update_poll(poll, payload)
        except DjangoValidationError as exc:
            raise HttpError(422, _format_validation_error(exc))
        return self._to_detail(updated, user)

    @route.post(
        "/{poll_id}/open",
        response=PollDetailSchema,
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
    )
    def open_poll_action(self, poll_id: UUID) -> PollDetailSchema:
        """Transition a DRAFT poll to OPEN."""
        user = self.user()
        poll = get_object_or_404(Poll, pk=poll_id)
        self._require_manage_polls(poll, user)
        opened = poll_service.open_poll(poll)
        return self._to_detail(opened, user)

    @route.post(
        "/{poll_id}/close",
        response=PollDetailSchema,
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
    )
    def close_poll_action(self, poll_id: UUID) -> PollDetailSchema:
        """Transition an OPEN poll to CLOSED."""
        user = self.user()
        poll = get_object_or_404(Poll, pk=poll_id)
        self._require_manage_polls(poll, user)
        closed = poll_service.close_poll(poll)
        return self._to_detail(closed, user)

    @route.post(
        "/{poll_id}/reopen",
        response=PollDetailSchema,
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
    )
    def reopen_poll_action(self, poll_id: UUID, payload: PollReopenSchema) -> PollDetailSchema:
        """Reopen a CLOSED poll, optionally setting or clearing ``closes_at``."""
        user = self.user()
        poll = get_object_or_404(Poll, pk=poll_id)
        self._require_manage_polls(poll, user)
        reopened = poll_service.reopen_poll(poll, payload)
        return self._to_detail(reopened, user)

    @route.delete(
        "/{poll_id}/",
        response={204: None},
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
    )
    def delete_poll_action(self, poll_id: UUID) -> tuple[int, None]:
        """Hard-delete a poll. Only the organization owner may perform this action."""
        user = self.user()
        poll = get_object_or_404(Poll, pk=poll_id)
        if poll.organization.owner_id != user.id:
            raise HttpError(403, "Only the organization owner can delete a poll")
        poll_service.delete_poll(poll)
        return 204, None

    @route.post(
        "/{poll_id}/vote",
        response=PollDetailSchema,
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
    )
    def vote(self, poll_id: UUID, payload: PollVoteSchema) -> PollDetailSchema:
        """Cast or replace the caller's vote for ``poll_id``.

        Translates service-layer exceptions into HTTP statuses:

        * :class:`PollNotOpenError` → ``423 Locked``
        * :class:`PollNotEligibleError` → ``403 Forbidden``
        * :class:`PollVoteAlreadyCastError` → ``409 Conflict``
        """
        user = self.user()
        try:
            poll_service.vote(user=user, poll_id=poll_id, payload=payload)
        except PollNotOpenError as exc:
            raise HttpError(423, str(exc))
        except PollNotEligibleError as exc:
            raise HttpError(403, str(exc))
        except PollVoteAlreadyCastError as exc:
            raise HttpError(409, str(exc))
        except DjangoValidationError as exc:
            # Covers PollValidationError raised when the payload references
            # unknown / cross-user file_upload ids.
            raise HttpError(422, _format_validation_error(exc))
        poll = get_object_or_404(Poll, pk=poll_id)
        return self._to_detail(poll, user)

    @route.delete(
        "/{poll_id}/vote",
        response={204: None},
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
    )
    def withdraw_vote_action(self, poll_id: UUID) -> tuple[int, None]:
        """Withdraw the caller's vote when the poll is OPEN and allows changes.

        Translates service-layer exceptions into HTTP statuses:

        * :class:`PollNotOpenError` → ``423 Locked``
        * :class:`PollVoteChangesNotAllowedError` → ``403 Forbidden``
        """
        user = self.user()
        try:
            poll_service.withdraw_vote(user=user, poll_id=poll_id)
        except PollNotOpenError as exc:
            raise HttpError(423, str(exc))
        except PollVoteChangesNotAllowedError as exc:
            raise HttpError(403, str(exc))
        return 204, None

    # ------------------------------------------------------------------ helpers

    def _page_url(self, page: int) -> str | None:
        """Build an absolute URL for a different page of the current list request.

        Mirrors :class:`PageNumberPaginationExtra` semantics: when the target
        page is 1 the ``page`` query param is removed rather than spelt out.
        """
        from ninja_extra.urls import remove_query_param, replace_query_param

        request = self.context.request  # type: ignore[union-attr]
        assert request is not None
        base = request.build_absolute_uri()
        if page <= 1:
            return remove_query_param(base, "page")
        return replace_query_param(base, "page", page)

    def _require_manage_polls(self, poll: Poll, user: t.Any) -> None:
        """Raise 403 unless ``user`` has ``manage_polls`` on ``poll.organization``."""
        if not poll.organization.has_org_permission(user.id, "manage_polls"):
            raise HttpError(403, "manage_polls permission required")

    def _to_list_item_bulk(
        self,
        poll: Poll,
        user: UserLike,
        ctx: "eligibility._BulkEligibilityContext",
    ) -> PollListItemSchema:
        """Build a list item using pre-computed per-user sets (no extra DB round-trips)."""
        # ``.all()`` hits the prefetch cache; ``values_list`` would bypass it.
        vote_tier_ids = [tier.id for tier in poll.vote_membership_tiers.all()]
        result_tier_ids = [tier.id for tier in poll.result_membership_tiers.all()]
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
            user_has_voted=eligibility.bulk_user_has_voted(user, poll, ctx),
            user_can_vote=(
                poll.status == Poll.PollStatus.OPEN and eligibility.bulk_can_vote(user, poll, vote_tier_ids, ctx)
            ),
            user_can_see_results=eligibility.bulk_can_see_results(user, poll, result_tier_ids, ctx),
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
