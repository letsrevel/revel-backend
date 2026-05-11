"""Staff endpoints for managing subscription plans, subscriptions, and payments."""

from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import OrganizationPermission
from events.service import subscription_service
from events.service.subscription_service import InitialPayment

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    auth=I18nJWTAuth(),
    tags=["Organization Admin"],
    throttle=WriteThrottle(),
    permissions=[OrganizationPermission("manage_subscriptions")],
)
class OrganizationAdminSubscriptionsController(OrganizationAdminBaseController):
    """Plans, subscriptions, and payments — all staff-managed in Phase 1."""

    # ---- Plans ----

    @route.get(
        "/tiers/{tier_id}/plans",
        url_name="list_subscription_plans",
        response=list[schema.PlanSchema],
        throttle=UserDefaultThrottle(),
    )
    def list_plans(self, slug: str, tier_id: UUID) -> QuerySet[models.MembershipSubscriptionPlan]:
        """List all subscription plans for a tier."""
        organization = self.get_one(slug)
        tier = get_object_or_404(models.MembershipTier, pk=tier_id, organization=organization)
        return models.MembershipSubscriptionPlan.objects.filter(tier=tier)

    @route.post(
        "/tiers/{tier_id}/plans",
        url_name="create_subscription_plan",
        response={201: schema.PlanSchema},
    )
    def create_plan(
        self,
        slug: str,
        tier_id: UUID,
        payload: schema.PlanCreateSchema,
    ) -> tuple[int, models.MembershipSubscriptionPlan]:
        """Create a new subscription plan on a membership tier."""
        organization = self.get_one(slug)
        tier = get_object_or_404(models.MembershipTier, pk=tier_id, organization=organization)
        plan = subscription_service.create_plan(tier, **payload.model_dump())
        return 201, plan

    @route.patch(
        "/plans/{plan_id}",
        url_name="update_subscription_plan",
        response=schema.PlanSchema,
    )
    def update_plan(
        self,
        slug: str,
        plan_id: UUID,
        payload: schema.PlanUpdateSchema,
    ) -> models.MembershipSubscriptionPlan:
        """Patch a subscription plan."""
        organization = self.get_one(slug)
        plan = get_object_or_404(
            models.MembershipSubscriptionPlan.objects.select_related("tier"),
            pk=plan_id,
            tier__organization=organization,
        )
        return subscription_service.update_plan(plan, **payload.model_dump(exclude_unset=True))

    @route.post(
        "/plans/{plan_id}/archive",
        url_name="archive_subscription_plan",
        response=schema.PlanSchema,
    )
    def archive_plan(self, slug: str, plan_id: UUID) -> models.MembershipSubscriptionPlan:
        """Archive a plan (sets ``is_active=False``)."""
        organization = self.get_one(slug)
        plan = get_object_or_404(
            models.MembershipSubscriptionPlan.objects.select_related("tier"),
            pk=plan_id,
            tier__organization=organization,
        )
        return subscription_service.archive_plan(plan)

    @route.delete(
        "/plans/{plan_id}",
        url_name="delete_subscription_plan",
        response={204: None},
    )
    def delete_plan(self, slug: str, plan_id: UUID) -> tuple[int, None]:
        """Hard-delete a plan. Blocks when subscriptions reference it."""
        organization = self.get_one(slug)
        plan = get_object_or_404(
            models.MembershipSubscriptionPlan.objects.select_related("tier"),
            pk=plan_id,
            tier__organization=organization,
        )
        subscription_service.delete_plan(plan)
        return 204, None

    # ---- Subscriptions ----

    @route.get(
        "/subscriptions",
        url_name="list_subscriptions",
        response=PaginatedResponseSchema[schema.SubscriptionSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(
        Searching,
        search_fields=["user__email", "user__first_name", "user__last_name", "user__preferred_name", "status"],
    )
    def list_subscriptions(self, slug: str) -> QuerySet[models.MembershipSubscription]:
        """List all subscriptions for the organization."""
        organization = self.get_one(slug)
        return (
            models.MembershipSubscription.objects.filter(organization=organization)
            .select_related("user", "plan", "plan__tier")
            .order_by("-created_at")
        )

    @route.get(
        "/subscriptions/{sub_id}",
        url_name="get_subscription",
        response=schema.SubscriptionSchema,
        throttle=UserDefaultThrottle(),
    )
    def get_subscription(self, slug: str, sub_id: UUID) -> models.MembershipSubscription:
        """Get a single subscription by id."""
        organization = self.get_one(slug)
        return get_object_or_404(
            models.MembershipSubscription.objects.select_related("user", "plan", "plan__tier"),
            pk=sub_id,
            organization=organization,
        )

    @route.post(
        "/subscriptions",
        url_name="create_subscription",
        response={201: schema.SubscriptionSchema},
    )
    def create_subscription(
        self,
        slug: str,
        payload: schema.SubscriptionCreateSchema,
    ) -> tuple[int, models.MembershipSubscription]:
        """Create a subscription on behalf of a user (OFFLINE flow)."""
        organization = self.get_one(slug)
        plan = get_object_or_404(
            models.MembershipSubscriptionPlan.objects.select_related("tier"),
            pk=payload.plan_id,
            tier__organization=organization,
        )
        user = get_object_or_404(RevelUser, pk=payload.user_id)

        initial: InitialPayment | None = None
        if payload.initial_payment_amount is not None:
            # ``SubscriptionCreateSchema._validate_initial_payment`` guarantees
            # ``initial_payment_currency`` is set when amount is provided.
            assert payload.initial_payment_currency is not None  # noqa: S101
            initial = InitialPayment(
                amount=payload.initial_payment_amount,
                currency=payload.initial_payment_currency,
                recorded_by=self.user(),
                notes=payload.initial_payment_notes,
            )

        subscription = subscription_service.create_subscription(plan, user, initial_payment=initial)
        return 201, subscription

    @route.post(
        "/subscriptions/{sub_id}/payments",
        url_name="record_subscription_payment",
        response={201: schema.MembershipPaymentSchema},
    )
    def record_payment(
        self,
        slug: str,
        sub_id: UUID,
        payload: schema.PaymentRecordSchema,
    ) -> tuple[int, models.MembershipPayment]:
        """Record a manual payment against a subscription."""
        organization = self.get_one(slug)
        subscription = get_object_or_404(
            models.MembershipSubscription.objects.select_related("plan"),
            pk=sub_id,
            organization=organization,
        )
        payment = subscription_service.record_payment(
            subscription,
            amount=payload.amount,
            currency=payload.currency,
            recorded_by=self.user(),
            notes=payload.notes,
            status=payload.status,
        )
        return 201, payment

    @route.post(
        "/subscriptions/{sub_id}/cancel",
        url_name="cancel_subscription",
        response=schema.SubscriptionSchema,
    )
    def cancel_subscription(
        self,
        slug: str,
        sub_id: UUID,
        payload: schema.CancelSubscriptionSchema,
    ) -> models.MembershipSubscription:
        """Cancel a subscription. ``immediate=False`` schedules cancellation at period end."""
        organization = self.get_one(slug)
        subscription = get_object_or_404(
            models.MembershipSubscription.objects.select_related("plan", "plan__tier", "user"),
            pk=sub_id,
            organization=organization,
        )
        return subscription_service.cancel_subscription(subscription, immediate=payload.immediate)

    @route.post(
        "/subscriptions/{sub_id}/pause",
        url_name="pause_subscription",
        response=schema.SubscriptionSchema,
    )
    def pause_subscription(self, slug: str, sub_id: UUID) -> models.MembershipSubscription:
        """Pause a subscription."""
        organization = self.get_one(slug)
        subscription = get_object_or_404(
            models.MembershipSubscription.objects.select_related("plan", "plan__tier", "user"),
            pk=sub_id,
            organization=organization,
        )
        return subscription_service.pause_subscription(subscription)

    @route.post(
        "/subscriptions/{sub_id}/resume",
        url_name="resume_subscription",
        response=schema.SubscriptionSchema,
    )
    def resume_subscription(self, slug: str, sub_id: UUID) -> models.MembershipSubscription:
        """Resume a paused subscription."""
        organization = self.get_one(slug)
        subscription = get_object_or_404(
            models.MembershipSubscription.objects.select_related("plan", "plan__tier", "user"),
            pk=sub_id,
            organization=organization,
        )
        return subscription_service.resume_subscription(subscription)

    # ---- Payments ----

    @route.post(
        "/payments/{payment_id}/refund",
        url_name="refund_subscription_payment",
        response=schema.MembershipPaymentSchema,
    )
    def refund_payment(
        self,
        slug: str,
        payment_id: UUID,
        payload: schema.RefundSchema,
    ) -> models.MembershipPayment:
        """Mark a recorded payment as refunded (record-only in MVP)."""
        organization = self.get_one(slug)
        payment = get_object_or_404(
            models.MembershipPayment.objects.select_related("subscription"),
            pk=payment_id,
            subscription__organization=organization,
        )
        return subscription_service.refund_payment(payment, recorded_by=self.user(), notes=payload.notes)
