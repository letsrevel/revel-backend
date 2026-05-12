"""Member-facing endpoints for membership subscriptions."""

from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
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
        if plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
            raise HttpError(400, str(_("This plan is not available for self-service subscription.")))

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
