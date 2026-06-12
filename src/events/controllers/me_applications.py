"""Member-facing endpoints for membership applications."""

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
)
from events.service.membership_manager.enums import MembershipNextStep


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
        params: schema.JoinEligibilityQuery = Query(...),  # type: ignore[type-arg]
    ) -> schema.MembershipEligibilitySchema:
        """Preview membership eligibility for the caller at a target tier (and optional plan).

        Pure check — no side effects. Use the result to render the right join CTA.
        """
        organization = get_object_or_404(Organization, slug=slug)
        tier = self._resolve_tier(organization, params.tier_id)
        plan = self._resolve_plan(organization, params.plan_id)
        service = MembershipEligibilityService(user=self.user(), organization=organization, tier=tier, plan=plan)
        eligibility = service.check_eligibility()
        return schema.MembershipEligibilitySchema.from_eligibility(eligibility)

    @route.post(
        "/organizations/{slug}/apply",
        url_name="apply_for_membership",
        response={201: schema.ApplyResponseSchema},
        throttle=WriteThrottle(),
    )
    def apply(self, slug: str, payload: schema.ApplyRequestSchema) -> tuple[int, schema.ApplyResponseSchema]:
        """Create or refresh the caller's membership application.

        Idempotent: if a PENDING application already exists for (user, org, tier),
        re-runs the gate and may advance state. Returns the application plus the
        latest eligibility verdict.

        Phase 1: plan_id is rejected (paid path lands in Phase 2).
        """
        if payload.plan_id is not None:
            raise HttpError(400, _("Paid applications are not supported in this release."))

        organization = get_object_or_404(Organization, slug=slug)
        tier = self._resolve_tier(organization, payload.tier_id)

        user = self.user()

        # Pre-gate hard-block check: refuse before creating an OMR row so we don't
        # leak org existence, queue noisy staff notifications, or persist PENDING
        # rows for users who have no in-app recourse (hard blacklist, org not
        # accepting requests, tier/plan unavailable, terminal questionnaire failure).
        # Recoverable blocks (questionnaire pending, manual approval, whitelist
        # pending) still create the OMR — it's the polling record that drives
        # state-advance-on-read.
        if not Organization.objects.for_user(user).filter(pk=organization.pk).exists():
            # Treat invisible orgs as 404 to avoid org-existence enumeration.
            raise HttpError(404, _("Not found."))

        preview = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()
        if not preview.allowed and preview.next_step in {
            None,
            MembershipNextStep.REQUIRES_INVITATION,
        }:
            raise HttpError(403, preview.reason or _("Not allowed."))

        application, eligibility = apply_for_membership(
            user=user,
            organization=organization,
            tier=tier,
            notes=payload.notes,
            questionnaire_submission_id=payload.questionnaire_submission_id,
        )
        # Inject application_id so the FE can poll this exact row.
        eligibility.application_id = application.pk

        return 201, schema.ApplyResponseSchema(
            application=schema.MembershipApplicationSchema.model_validate(application, from_attributes=True),
            eligibility=schema.MembershipEligibilitySchema.from_eligibility(eligibility),
        )

    @route.post(
        "/applications/{application_id}/cancel",
        url_name="cancel_membership_application",
        response=schema.MembershipApplicationSchema,
        throttle=WriteThrottle(),
    )
    def cancel(self, application_id: UUID) -> OrganizationMembershipRequest:
        """Cancel one of the caller's own applications. Idempotent on already-cancelled rows."""
        app = get_object_or_404(OrganizationMembershipRequest, pk=application_id, user=self.user())
        if app.status not in {
            OrganizationMembershipRequest.Status.PENDING,
            OrganizationMembershipRequest.Status.APPROVED,
        }:
            return app
        app.status = OrganizationMembershipRequest.Status.CANCELLED
        app.save(update_fields=["status", "updated_at"])
        return app

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
        return schema.ApplyResponseSchema(
            application=schema.MembershipApplicationSchema.model_validate(advanced, from_attributes=True),
            eligibility=schema.MembershipEligibilitySchema.from_eligibility(eligibility),
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
