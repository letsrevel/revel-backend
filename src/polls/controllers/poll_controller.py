"""Polls API controller — read and write endpoints.

Conventions (matching :mod:`events.controllers.questionnaire`):

* Pagination on list endpoints uses ``@paginate(PageNumberPaginationExtra)``
  rather than a manual ``Paginator``. The endpoint returns a ``QuerySet`` and
  ninja_extra slices it for the requested page.
* Object-level permissions live in :mod:`polls.permissions` as
  :class:`PollPermission`/:class:`IsPollOrganizationOwner` subclasses of
  ``events.controllers.permissions.RootPermission`` and are declared per-route.
* The controller resolves rows with ``self.get_object_or_exception(qs, pk=...)``
  which lets the framework invoke the route's permission classes.
"""

import typing as t
from uuid import UUID

from django.db.models import QuerySet
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth, OptionalAuth
from common.controllers import UserAwareController
from common.throttling import AnonDefaultThrottle, UserDefaultThrottle, WriteThrottle
from events.controllers.permissions import OrganizationPermission
from events.models.mixins import ResourceVisibility
from events.models.organization import Organization
from polls.models import Poll
from polls.permissions import IsPollOrganizationOwner, PollPermission
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
from polls.types import UserLike


@api_controller(
    "/polls",
    tags=["Polls"],
    auth=OptionalAuth(),
    throttle=[AnonDefaultThrottle(), UserDefaultThrottle()],
)
class PollController(UserAwareController):
    """Read and write endpoints for polls.

    Anonymous access is permitted via :class:`OptionalAuth`; visibility is
    enforced by :meth:`Poll.objects.for_user` and the per-route permission
    classes from :mod:`polls.permissions`.
    """

    # ------------------------------------------------------------------ querysets

    def _list_queryset(self) -> QuerySet[Poll]:
        """Visibility-filtered + annotated queryset used by the list endpoint.

        Combines :meth:`Poll.objects.for_user` (visibility filter) with
        :meth:`Poll.objects.with_user_annotations` (per-row Exists() flags) so
        per-row schema resolvers can compute eligibility without N+1 queries.
        """
        user = self.maybe_user()
        return (
            Poll.objects.for_user(user)
            .with_user_annotations(user)
            .select_related("organization", "event", "questionnaire")
            .prefetch_related("vote_membership_tiers", "result_membership_tiers")
            .order_by("-created_at", "id")
        )

    @staticmethod
    def _apply_detail_prefetches(qs: QuerySet[Poll]) -> QuerySet[Poll]:
        """Attach the prefetches needed to render :class:`PollDetailSchema`.

        The wrapped questionnaire's question/section/option payload is built
        lazily in :meth:`_to_detail` via
        :class:`questionnaires.service.QuestionnaireService`, so only the
        poll's direct relations need prefetching here.
        """
        return qs.select_related("organization", "event", "questionnaire").prefetch_related(
            "vote_membership_tiers",
            "result_membership_tiers",
        )

    def _detail_base_queryset(self) -> QuerySet[Poll]:
        """Unfiltered single-poll queryset (no visibility filtering).

        Used by the detail GET endpoint so it can distinguish "doesn't exist"
        (404) from "exists but caller cannot see it" (403). Lifecycle/write
        endpoints use :meth:`_detail_queryset` and rely on the visibility
        filter for the 404 case; permission classes then enforce write
        authority on top.
        """
        return self._apply_detail_prefetches(Poll.objects.all())

    def _detail_queryset(self) -> QuerySet[Poll]:
        """Visibility-filtered single-poll queryset (lifecycle/write endpoints)."""
        return self._apply_detail_prefetches(Poll.objects.for_user(self.maybe_user()))

    def _refetch_for_detail(self, poll_id: UUID) -> Poll:
        """Refetch a poll with the deep prefetches used by :class:`PollDetailSchema`.

        Used after write endpoints so the response includes the prefetched
        questionnaire structure even when the service returned a freshly-fetched
        instance without the deep prefetches.
        """
        return self._detail_queryset().get(pk=poll_id)

    @staticmethod
    def _organization_queryset() -> QuerySet[Organization]:
        """Queryset for resolving organizations on the create endpoint."""
        return Organization.objects.all()

    # ------------------------------------------------------------------ reads

    @route.get(
        "/",
        url_name="list_polls",
        response=PaginatedResponseSchema[PollListItemSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_polls(
        self,
        organization_id: UUID | None = None,
        event_id: UUID | None = None,
        status: Poll.PollStatus | None = None,
    ) -> QuerySet[Poll]:
        """List polls visible to the current user, optionally filtered.

        Per-user eligibility flags are computed from annotations attached by
        :meth:`Poll.objects.with_user_annotations`, so per-row query count is
        flat regardless of the page size.
        """
        qs = self._list_queryset()
        if organization_id is not None:
            qs = qs.filter(organization_id=organization_id)
        if event_id is not None:
            qs = qs.filter(event_id=event_id)
        if status is not None:
            qs = qs.filter(status=status)
        return qs

    @route.get("/{poll_id}/", url_name="get_poll", response=PollDetailSchema)
    def get_poll(self, poll_id: UUID) -> dict[str, t.Any]:
        """Retrieve a single poll, including user-specific flags and (when allowed) results.

        Returns ``404`` when the poll does not exist and ``403`` when it exists
        but the caller is not in any audience that may see it. Frontends can
        therefore distinguish "missing" from "forbidden" without inspecting
        response bodies.
        """
        user = self.maybe_user()
        poll = t.cast(Poll, self.get_object_or_exception(self._detail_base_queryset(), pk=poll_id))
        if not eligibility.can_see_poll(user, poll):
            raise HttpError(403, "You are not allowed to see this poll.")
        return self._to_detail(poll, user)

    @route.get("/{poll_id}/results", url_name="get_poll_results", response=PollResultsSchema)
    def get_poll_results(self, poll_id: UUID) -> PollResultsSchema:
        """Return aggregated poll results, honouring visibility and timing rules."""
        user = self.maybe_user()
        poll = t.cast(Poll, self.get_object_or_exception(self._detail_queryset(), pk=poll_id))
        is_staff = eligibility.is_staff_or_owner(user, poll)
        if not eligibility.can_see_results(user, poll, _is_staff=is_staff):
            raise HttpError(403, "You are not allowed to see the results for this poll.")
        return compute_poll_results(
            poll, viewer_sees_identity=self._viewer_sees_identity(poll, user, _is_staff=is_staff)
        )

    # ------------------------------------------------------------------ writes

    @route.post(
        "/organizations/{organization_id}",
        url_name="create_poll",
        response={201: PollDetailSchema},
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
        permissions=[OrganizationPermission("manage_polls")],
    )
    def create_poll(self, organization_id: UUID, payload: PollCreateSchema) -> tuple[int, dict[str, t.Any]]:
        """Create a new poll under an organization the caller can manage.

        ``organization_id`` is taken from the URL path; the
        :class:`OrganizationPermission` permission class enforces
        ``manage_polls`` against the resolved :class:`Organization`.
        """
        organization = t.cast(
            Organization, self.get_object_or_exception(self._organization_queryset(), pk=organization_id)
        )
        poll = poll_service.create_poll(organization, payload)
        return 201, self._to_detail(self._refetch_for_detail(poll.pk), self.user())

    @route.patch(
        "/{poll_id}/",
        url_name="patch_poll",
        response=PollDetailSchema,
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
        permissions=[PollPermission("manage_polls")],
    )
    def patch_poll(self, poll_id: UUID, payload: PollUpdateSchema) -> dict[str, t.Any]:
        """Apply a partial update to a poll the caller can manage.

        Cross-field constraint violations that can't be caught by the schema
        validator (e.g., clearing ``event_id`` on a poll that already has
        PRIVATE visibility) surface from the model's ``full_clean`` as a
        ``PollValidationError`` (translated by the service layer) and are
        dispatched to HTTP 422 by the registered exception handler.
        """
        poll = t.cast(Poll, self.get_object_or_exception(self._detail_queryset(), pk=poll_id))
        poll_service.update_poll(poll, payload)
        return self._to_detail(self._refetch_for_detail(poll_id), self.user())

    @route.post(
        "/{poll_id}/open",
        url_name="open_poll",
        response=PollDetailSchema,
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
        permissions=[PollPermission("manage_polls")],
    )
    def open_poll_action(self, poll_id: UUID) -> dict[str, t.Any]:
        """Transition a DRAFT poll to OPEN."""
        poll = t.cast(Poll, self.get_object_or_exception(self._detail_queryset(), pk=poll_id))
        poll_service.open_poll(poll)
        return self._to_detail(self._refetch_for_detail(poll_id), self.user())

    @route.post(
        "/{poll_id}/close",
        url_name="close_poll",
        response=PollDetailSchema,
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
        permissions=[PollPermission("manage_polls")],
    )
    def close_poll_action(self, poll_id: UUID) -> dict[str, t.Any]:
        """Transition an OPEN poll to CLOSED."""
        poll = t.cast(Poll, self.get_object_or_exception(self._detail_queryset(), pk=poll_id))
        poll_service.close_poll(poll)
        return self._to_detail(self._refetch_for_detail(poll_id), self.user())

    @route.post(
        "/{poll_id}/reopen",
        url_name="reopen_poll",
        response=PollDetailSchema,
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
        permissions=[PollPermission("manage_polls")],
    )
    def reopen_poll_action(self, poll_id: UUID, payload: PollReopenSchema) -> dict[str, t.Any]:
        """Reopen a CLOSED poll, optionally setting or clearing ``closes_at``."""
        poll = t.cast(Poll, self.get_object_or_exception(self._detail_queryset(), pk=poll_id))
        poll_service.reopen_poll(poll, payload)
        return self._to_detail(self._refetch_for_detail(poll_id), self.user())

    @route.delete(
        "/{poll_id}/",
        url_name="delete_poll",
        response={204: None},
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
        permissions=[IsPollOrganizationOwner()],
    )
    def delete_poll_action(self, poll_id: UUID) -> tuple[int, None]:
        """Hard-delete a poll. Only the organization owner may perform this action."""
        poll = t.cast(Poll, self.get_object_or_exception(self._detail_queryset(), pk=poll_id))
        poll_service.delete_poll(poll)
        return 204, None

    @route.post(
        "/{poll_id}/vote",
        url_name="vote_poll",
        response=PollDetailSchema,
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
    )
    def vote(self, poll_id: UUID, payload: PollVoteSchema) -> dict[str, t.Any]:
        """Cast or replace the caller's vote for ``poll_id``.

        Audience/visibility checks live inside the service so that the same
        rules apply to direct service callers; the controller stays thin and
        the per-app exception handlers (see :mod:`polls.exception_handlers`)
        translate service-layer exceptions into HTTP statuses:

        * :class:`PollNotOpenError` → ``423 Locked``
        * :class:`PollNotEligibleError` → ``403 Forbidden``
        * :class:`PollVoteAlreadyCastError` → ``409 Conflict``
        * :class:`PollValidationError` → ``422 Unprocessable Entity``
        """
        user = self.user()
        poll_service.vote(user=user, poll_id=poll_id, payload=payload)
        poll = t.cast(Poll, self.get_object_or_exception(self._detail_queryset(), pk=poll_id))
        return self._to_detail(poll, user)

    @route.delete(
        "/{poll_id}/vote",
        url_name="withdraw_vote",
        response={204: None},
        throttle=WriteThrottle(),
        auth=I18nJWTAuth(),
    )
    def withdraw_vote_action(self, poll_id: UUID) -> tuple[int, None]:
        """Withdraw the caller's vote when the poll is OPEN and allows changes.

        Service-layer exceptions are translated into HTTP statuses by the
        per-app handlers registered in :mod:`polls.exception_handlers`:

        * :class:`PollNotOpenError` → ``423 Locked``
        * :class:`PollVoteChangesNotAllowedError` → ``403 Forbidden``
        """
        user = self.user()
        poll_service.withdraw_vote(user=user, poll_id=poll_id)
        return 204, None

    # ------------------------------------------------------------------ helpers

    def _to_detail(self, poll: Poll, user: UserLike) -> dict[str, t.Any]:
        """Build the response payload for :class:`PollDetailSchema` endpoints.

        The wrapped questionnaire is built via
        :class:`questionnaires.service.QuestionnaireService` so the wire format
        (``multiple_choice_questions`` / ``free_text_questions`` /
        ``file_upload_questions``) matches
        :meth:`events.controllers.event_public.attendance.PublicEventController.get_questionnaire`.
        That service issues its own prefetch-aware fetch (one extra round-trip
        per detail response, same shape as the canonical endpoint) — the poll
        queryset deliberately does **not** duplicate those prefetches.

        Performance: :func:`eligibility.is_staff_or_owner` is computed once
        and threaded through every consumer helper (``can_see_results``,
        ``can_vote``, ``_viewer_sees_identity``) so the ``OrganizationStaff``
        lookup runs at most once per response, not six times.
        """
        from questionnaires.service import QuestionnaireService

        is_staff = eligibility.is_staff_or_owner(user, poll)
        user_can_see_results = eligibility.can_see_results(user, poll, _is_staff=is_staff)
        results: PollResultsSchema | None = None
        if user_can_see_results:
            results = compute_poll_results(
                poll, viewer_sees_identity=self._viewer_sees_identity(poll, user, _is_staff=is_staff)
            )
        questionnaire_schema = QuestionnaireService(poll.questionnaire_id).build()
        return {
            "id": poll.id,
            "organization_id": poll.organization_id,
            "event_id": poll.event_id,
            "questionnaire_id": poll.questionnaire_id,
            "status": Poll.PollStatus(poll.status),
            "opened_at": poll.opened_at,
            "closes_at": poll.closes_at,
            "closed_at": poll.closed_at,
            "allow_vote_changes": poll.allow_vote_changes,
            "vote_visibility": ResourceVisibility(poll.vote_visibility),
            "result_visibility": ResourceVisibility(poll.result_visibility),
            "result_timing": Poll.PollResultTiming(poll.result_timing),
            "staff_anonymous": poll.staff_anonymous,
            "public_anonymous": poll.public_anonymous,
            # ``.values_list`` would bypass the prefetched cache populated by
            # ``_detail_queryset``; iterate ``.all()`` to read the prefetched M2M.
            "vote_membership_tier_ids": [tier.id for tier in poll.vote_membership_tiers.all()],
            "result_membership_tier_ids": [tier.id for tier in poll.result_membership_tiers.all()],
            "user_has_voted": eligibility.user_has_voted(user, poll),
            "user_can_vote": poll.status == Poll.PollStatus.OPEN
            and eligibility.can_vote(user, poll, _is_staff=is_staff),
            "user_can_see_results": user_can_see_results,
            "questionnaire": questionnaire_schema,
            "results": results,
        }

    @staticmethod
    def _viewer_sees_identity(poll: Poll, user: UserLike, *, _is_staff: bool | None = None) -> bool:
        """Whether the viewer sees voter identities in the aggregated results.

        ``_is_staff`` is the pre-computed :func:`eligibility.is_staff_or_owner`
        result threaded through from :meth:`_to_detail` so the
        ``OrganizationStaff`` lookup is not repeated.
        """
        is_staff = _is_staff if _is_staff is not None else eligibility.is_staff_or_owner(user, poll)
        if is_staff:
            return not poll.staff_anonymous
        return not poll.public_anonymous


__all__ = ["PollController"]
