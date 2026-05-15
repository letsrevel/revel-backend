"""Member-facing read endpoints for membership subscriptions."""

from uuid import UUID

from django.db.models import Prefetch, QuerySet
from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.throttling import UserDefaultThrottle
from events import schema
from events.models import MembershipSubscription, OrganizationMember


@api_controller("/me", auth=I18nJWTAuth(), tags=["Me - Subscriptions"], throttle=UserDefaultThrottle())
class MeSubscriptionsController(UserAwareController):
    """Read-only access to the current user's own membership subscriptions."""

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
        "/memberships",
        url_name="list_my_memberships",
        response=PaginatedResponseSchema[schema.MyMembershipSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_my_memberships(self) -> QuerySet[OrganizationMember]:
        """List every organization the user is a member of (legacy + subscription-backed).

        Each entry inlines the caller's most recent non-terminal subscription for that
        organization when one exists. Includes BANNED memberships so the frontend can
        render an explicit banned state; the FE may filter as needed.
        """
        user = self.user()
        active_subs = (
            MembershipSubscription.objects.filter(user=user)
            .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
            .select_related("plan", "plan__tier", "organization")
            .order_by("-created_at")
        )
        return (
            OrganizationMember.objects.filter(user=user)
            .select_related("organization", "tier")
            .prefetch_related(
                Prefetch(
                    "organization__membership_subscriptions",
                    queryset=active_subs,
                    to_attr="_caller_active_subs",
                )
            )
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
