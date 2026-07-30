"""Member-facing endpoints for membership applications."""

import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from ninja import Query
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.schema import ErrorDetail
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import schema
from events.models import (
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMembershipRequest,
)
from events.service.membership_manager import (
    MembershipEligibilityService,
    advance_application,
    apply_for_membership,
    cancel_application,
)
from events.service.membership_manager.enums import MembershipNextStep, Reasons


@api_controller("/me", auth=I18nJWTAuth(), tags=["Me - Applications"], throttle=UserDefaultThrottle())
class MeMembershipApplicationsController(UserAwareController):
    """Membership application flow: join eligibility, apply, cancel, list/detail."""

    def _resolve_tier(self, organization: Organization, tier_id: UUID | None) -> MembershipTier | None:
        if tier_id is None:
            return None
        return get_object_or_404(MembershipTier, pk=tier_id, organization=organization)

    def _resolve_plan(self, organization: Organization, plan_id: UUID | None) -> MembershipSubscriptionPlan | None:
        if plan_id is None:
            return None
        return get_object_or_404(
            MembershipSubscriptionPlan.objects.select_related("tier", "tier__organization"),
            pk=plan_id,
            tier__organization=organization,
        )

    @route.get(
        "/organizations/{slug}/join-eligibility",
        url_name="get_join_eligibility",
        response=schema.MembershipEligibilitySchema,
    )
    def get_join_eligibility(
        self,
        slug: str,
        params: t.Annotated[schema.JoinEligibilityQuery, Query(...)],
    ) -> schema.MembershipEligibilitySchema:
        """Preview membership eligibility for the caller at a target tier (and optional plan).

        Pure check — no side effects. Use the result to render the right join CTA.
        """
        # Mirror /apply's anti-enumeration posture: invisible orgs are a 404,
        # not a 200 confirming existence (+ UUID) to slug-guessing callers.
        organization = get_object_or_404(Organization.objects.for_user(self.user()), slug=slug)
        tier = self._resolve_tier(organization, params.tier_id)
        plan = self._resolve_plan(organization, params.plan_id)
        service = MembershipEligibilityService(user=self.user(), organization=organization, tier=tier, plan=plan)
        eligibility = service.check_eligibility()
        return schema.MembershipEligibilitySchema.from_eligibility(eligibility)

    @route.post(
        "/organizations/{slug}/apply",
        url_name="apply_for_membership",
        # 400 has two shapes: a plain ``{detail}`` for the plan_id refusal, and
        # the serialized eligibility payload when a gate refuses the application
        # (``MembershipApplicationIneligibleError`` — see events/exception_handlers).
        # The frontend branches on the latter to re-render the join CTA.
        response={
            201: schema.ApplyResponseSchema,
            400: schema.MembershipEligibilitySchema | ErrorDetail,
            403: ErrorDetail,
            404: ErrorDetail,
            409: ErrorDetail,
        },
        throttle=WriteThrottle(),
    )
    def apply(self, slug: str, payload: schema.ApplyRequestSchema) -> tuple[int, schema.ApplyResponseSchema]:
        """Create or refresh the caller's membership application.

        Idempotent: if a PENDING application already exists for (user, org, tier),
        re-runs the gate and may advance state. Returns the application plus the
        latest eligibility verdict.

        A ``plan_id`` makes this a paid application: the row advances to
        APPROVED once every gate passes and settles COMPLETED when the
        subscription created via ``/subscribe`` activates.
        """
        organization = get_object_or_404(Organization, slug=slug)
        plan = self._resolve_plan(organization, payload.plan_id)
        tier = self._resolve_tier(organization, payload.tier_id) or (plan.tier if plan else None)

        user = self.user()

        # Pre-gate hard-block check: refuse before creating an OMR row so we don't
        # leak org existence, queue noisy staff notifications, or persist PENDING
        # rows for users who have no in-app recourse (hard blacklist, org not
        # accepting requests, tier/plan unavailable, terminal questionnaire failure).
        # Recoverable blocks (questionnaire pending, manual approval, whitelist
        # pending) still create the OMR — it's the polling record that drives
        # state-advance-on-read. A latest-row REJECTED verdict carries
        # next_step=REAPPLY and likewise passes: the fresh PENDING row created
        # below supersedes the rejected one — unless the cause of that rejection
        # is still terminal, in which case ApplicationStatusGate surfaces the
        # terminal verdict (next_step=None) and we 403 here rather than minting a
        # row that would be auto-rejected (and re-notified) on its first read.
        if not Organization.objects.for_user(user).filter(pk=organization.pk).exists():
            # Treat invisible orgs as 404 to avoid org-existence enumeration.
            raise HttpError(404, _("Not found."))

        preview = MembershipEligibilityService(
            user=user, organization=organization, tier=tier, plan=plan
        ).check_eligibility()
        if not preview.allowed and preview.next_step in {
            None,
            MembershipNextStep.REQUIRES_INVITATION,
        }:
            raise HttpError(403, preview.reason or _("Not allowed."))
        # ALREADY_MEMBER is an *allowing* verdict (the eligibility service answers
        # "you're already in at this tier" for preview purposes), but there is
        # nothing to apply for: creating a row here would mint a junk PENDING
        # application on every call — queue noise, and tier.on_delete=PROTECT
        # means those rows permanently block deleting the tier.
        if preview.next_step == MembershipNextStep.ALREADY_MEMBER:
            raise HttpError(409, _(Reasons.ALREADY_ACTIVE_MEMBER))

        application, eligibility = apply_for_membership(
            user=user,
            organization=organization,
            tier=tier,
            plan=plan,
            notes=payload.notes,
            questionnaire_submission_id=payload.questionnaire_submission_id,
        )
        # Inject application_id so the FE can poll this exact row.
        eligibility.application_id = application.pk

        # Return a dict (not a constructed schema) so Ninja's response pipeline validates the
        # Django model through ``MembershipApplicationSchema``'s resolvers. Pre-validating the
        # inner schema would make the outer wrap-validator re-run those resolvers against the
        # already-validated schema instance, which no longer carries the Django relations.
        return 201, t.cast(
            schema.ApplyResponseSchema,
            {
                "application": application,
                "eligibility": schema.MembershipEligibilitySchema.from_eligibility(eligibility),
            },
        )

    @route.post(
        "/applications/{application_id}/cancel",
        url_name="cancel_membership_application",
        response=schema.MembershipApplicationSchema,
        throttle=WriteThrottle(),
    )
    def cancel(self, application_id: UUID) -> OrganizationMembershipRequest:
        """Cancel one of the caller's own applications. Idempotent on already-cancelled rows."""
        app = get_object_or_404(
            OrganizationMembershipRequest.objects.select_related("organization", "tier"),
            pk=application_id,
            user=self.user(),
        )
        return cancel_application(app)

    @route.get(
        "/applications/{application_id}",
        url_name="get_membership_application",
        response=schema.ApplyResponseSchema,
    )
    def get_application(self, application_id: UUID) -> schema.ApplyResponseSchema:
        """Get one of the caller's applications. Runs the gate on read; may advance state."""
        app = get_object_or_404(OrganizationMembershipRequest, pk=application_id, user=self.user())
        advanced, eligibility = advance_application(app)
        eligibility.application_id = advanced.pk
        # See the note on ``apply``: return a dict so Ninja validates the Django model
        # through ``MembershipApplicationSchema``'s resolvers.
        return t.cast(
            schema.ApplyResponseSchema,
            {
                "application": advanced,
                "eligibility": schema.MembershipEligibilitySchema.from_eligibility(eligibility),
            },
        )

    @route.get(
        "/applications",
        url_name="list_membership_applications",
        response=PaginatedResponseSchema[schema.MembershipApplicationSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_applications(self) -> QuerySet[OrganizationMembershipRequest]:
        """List the caller's applications across organizations."""
        return (
            OrganizationMembershipRequest.objects.filter(user=self.user())
            .select_related("organization", "tier", "plan", "subscription", "questionnaire_submission")
            .order_by("-created_at")
        )
