"""Member-facing endpoints for membership subscriptions."""

from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import schema
from events.models import MembershipSubscription, MembershipSubscriptionPlan, Organization
from events.service import subscription_service, subscription_stripe_service


@api_controller("/me", auth=I18nJWTAuth(), tags=["Me - Subscriptions"], throttle=UserDefaultThrottle())
class MeSubscriptionsController(UserAwareController):
    """Member-facing access to the current user's own membership subscriptions."""

    @route.get(
        "/membership-subscriptions",
        url_name="list_my_membership_subscriptions",
        response=PaginatedResponseSchema[schema.MySubscriptionSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_my_subscriptions(self) -> QuerySet[MembershipSubscription]:
        """List all of the current user's memberships subscriptions across organizations."""
        return (
            MembershipSubscription.objects.filter(user=self.user())
            .select_related("plan", "plan__tier", "organization")
            .order_by("-created_at")
        )

    @route.get(
        "/organizations/{org_id}/subscription",
        url_name="get_my_organization_subscription",
        response=schema.MySubscriptionSchema,
    )
    def get_my_subscription(self, org_id: UUID) -> MembershipSubscription:
        """Get the current user's most recent non-terminal subscription in an organization.

        404 if the user has never subscribed (or only has fully-terminated history).
        """
        qs = (
            MembershipSubscription.objects.filter(user=self.user(), organization_id=org_id)
            .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
            .select_related("plan", "plan__tier", "organization")
            .order_by("-created_at")
        )
        return get_object_or_404(qs)

    @route.post(
        "/organizations/{org_id}/subscribe",
        url_name="subscribe_to_membership_plan",
        response={201: schema.SubscribeResponseSchema},
        throttle=WriteThrottle(),
    )
    def subscribe(
        self,
        org_id: UUID,
        payload: schema.SubscribeRequestSchema,
    ) -> tuple[int, schema.SubscribeResponseSchema]:
        """Start a Stripe-backed subscription on an ONLINE plan.

        Returns the local subscription row plus a Stripe ``client_secret`` the
        frontend uses to confirm the first invoice's PaymentIntent.
        """
        organization = get_object_or_404(Organization, pk=org_id)
        plan = get_object_or_404(
            MembershipSubscriptionPlan.objects.select_related("tier", "tier__organization"),
            pk=payload.plan_id,
            tier__organization=organization,
            is_active=True,
        )
        # ``start_online_subscription`` enforces ``payment_method == ONLINE``
        # and raises 400 if the plan is offline; no need to repeat the check.
        subscription, client_secret = subscription_stripe_service.start_online_subscription(plan, self.user())
        return 201, schema.SubscribeResponseSchema(
            subscription=schema.MySubscriptionSchema.model_validate(subscription, from_attributes=True),
            client_secret=client_secret,
        )

    @route.post(
        "/organizations/{org_id}/subscription/cancel",
        url_name="cancel_my_membership_subscription",
        response=schema.MySubscriptionSchema,
        throttle=WriteThrottle(),
    )
    def cancel_subscription(
        self,
        org_id: UUID,
        payload: schema.MemberCancelSubscriptionSchema,
    ) -> MembershipSubscription:
        """Cancel the caller's active subscription in an organization.

        For ONLINE plans the cancel is mirrored to Stripe; the webhook then
        settles local state. ``immediate=False`` (default) schedules the
        cancellation at the period boundary.
        """
        qs = (
            MembershipSubscription.objects.filter(user=self.user(), organization_id=org_id)
            .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
            .select_related("plan", "plan__tier", "organization")
            .order_by("-created_at")
        )
        subscription = get_object_or_404(qs)
        return subscription_service.cancel_subscription(subscription, immediate=payload.immediate)

    @route.post(
        "/organizations/{org_id}/subscription/change-plan",
        url_name="change_my_membership_plan",
        response=schema.MySubscriptionSchema,
        throttle=WriteThrottle(),
    )
    def change_plan(
        self,
        org_id: UUID,
        payload: schema.ChangePlanRequestSchema,
    ) -> MembershipSubscription:
        """Switch to a different plan within the same organization.

        For ONLINE plans the price delta decides: upgrade prorates immediately
        on Stripe, downgrade is scheduled via a Stripe Subscription Schedule
        and surfaces as ``pending_plan_id`` until the period rolls over.
        """
        qs = (
            MembershipSubscription.objects.filter(user=self.user(), organization_id=org_id)
            .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
            .select_related("plan", "plan__tier", "organization")
            .order_by("-created_at")
        )
        subscription = get_object_or_404(qs)
        new_plan = get_object_or_404(
            MembershipSubscriptionPlan.objects.select_related("tier", "tier__organization"),
            pk=payload.plan_id,
            tier__organization_id=org_id,
            is_active=True,
        )
        return subscription_service.change_plan(subscription, new_plan)

    @route.post(
        "/organizations/{org_id}/billing-portal",
        url_name="create_billing_portal_session",
        response={201: schema.BillingPortalSessionSchema},
        throttle=WriteThrottle(),
    )
    def create_billing_portal_session(
        self,
        org_id: UUID,
        payload: schema.BillingPortalRequestSchema,
    ) -> tuple[int, schema.BillingPortalSessionSchema]:
        """Create a Stripe Customer Portal session for the caller in this org.

        Members use the portal to manage saved payment methods and download
        invoices. The portal also offers cancel/change-plan UI when the org's
        Stripe dashboard configuration enables it.
        """
        from common.models import SiteSettings

        organization = get_object_or_404(Organization, pk=org_id)
        # ``payload.return_url`` is a validated ``HttpUrl`` (or None) — coerce
        # to plain ``str`` for the Stripe API. Falling back to the platform's
        # frontend keeps a sensible default when the client omits it.
        return_url = str(payload.return_url) if payload.return_url else SiteSettings.get_solo().frontend_base_url
        url = subscription_stripe_service.create_billing_portal_session(
            self.user(),
            organization,
            return_url=return_url,
        )
        return 201, schema.BillingPortalSessionSchema(url=url)
