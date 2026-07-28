"""Member-facing endpoints for membership subscriptions."""

import typing as t
from uuid import UUID

from django.db.models import F, Prefetch, QuerySet
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.schema import ErrorDetail, ResponseMessage
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import schema
from events.models import MembershipSubscription, MembershipSubscriptionPlan, Organization, OrganizationMember
from events.service import subscription_service, subscription_stripe_service, subscription_uncancel


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
            .order_by("-created_at", "-id")
        )

    @route.get(
        "/organizations/{org_id}/subscription",
        url_name="get_my_organization_subscription",
        response={200: schema.MySubscriptionSchema, 404: ResponseMessage},
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
        response={
            201: schema.SubscribeResponseSchema,
            400: ResponseMessage,
            404: ErrorDetail,
            409: schema.SubscriptionActivationPendingSchema,
            502: ResponseMessage,
        },
        throttle=WriteThrottle(),
    )
    def subscribe(
        self,
        org_id: UUID,
        payload: schema.SubscribeRequestSchema,
    ) -> tuple[int, schema.SubscribeResponseSchema]:
        """Start a Stripe-backed subscription on an ONLINE plan.

        Returns the local subscription row plus a hosted Stripe Checkout
        ``checkout_url`` the frontend redirects the member to for payment.
        """
        # Visibility-aware load: hard-blacklisted users get a 404 (the org is
        # invisible to them) rather than a distinguishable 403, matching the
        # anti-enumeration posture of the other member-facing entry points.
        organization = get_object_or_404(Organization.objects.for_user(self.user()), pk=org_id)
        plan = get_object_or_404(
            MembershipSubscriptionPlan.objects.select_related("tier", "tier__organization"),
            pk=payload.plan_id,
            tier__organization=organization,
            is_active=True,
        )
        # ``start_online_subscription`` enforces ``payment_method == ONLINE``
        # and raises 400 if the plan is offline; no need to repeat the check.
        subscription, checkout_url = subscription_stripe_service.start_online_subscription(plan, self.user())
        # Return a dict (not a constructed schema): Ninja's response pipeline will
        # validate it via ``SubscribeResponseSchema``. Pre-validating the inner
        # ``MySubscriptionSchema`` would cause Ninja's wrap-validator to re-run
        # resolvers against the already-validated schema instance (which lacks
        # the original Django relations), breaking serialization.
        return 201, t.cast(
            schema.SubscribeResponseSchema,
            {"subscription": subscription, "checkout_url": checkout_url},
        )

    @route.post(
        "/organizations/{org_id}/subscription/cancel",
        url_name="cancel_my_membership_subscription",
        response={200: schema.MySubscriptionSchema, 400: ResponseMessage, 404: ResponseMessage},
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
        "/organizations/{org_id}/subscription/uncancel",
        url_name="uncancel_my_membership_subscription",
        response={
            200: schema.MySubscriptionSchema,
            400: ResponseMessage,
            404: ResponseMessage,
            502: ResponseMessage,
        },
        throttle=WriteThrottle(),
    )
    def uncancel_subscription(self, org_id: UUID) -> MembershipSubscription:
        """Undo a scheduled cancellation on the caller's subscription.

        Clears ``cancel_at_period_end`` so the subscription keeps renewing; for
        ONLINE plans the same flag is cleared on Stripe. Refuses once the row is
        terminal, or if the plan has since been archived.
        """
        qs = (
            MembershipSubscription.objects.filter(user=self.user(), organization_id=org_id)
            .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
            .select_related("plan", "plan__tier", "organization")
            .order_by("-created_at")
        )
        subscription = get_object_or_404(qs)
        return subscription_uncancel.uncancel_subscription(subscription)

    @route.post(
        "/organizations/{org_id}/subscription/change-plan",
        url_name="change_my_membership_plan",
        response={
            200: schema.MySubscriptionSchema,
            400: ResponseMessage,
            404: ResponseMessage,
            502: ResponseMessage,
        },
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
        "/organizations/{org_id}/subscription/revive",
        url_name="revive_my_membership_subscription",
        response={
            200: schema.RevivalResponseSchema,
            400: ResponseMessage,
            # ``_validate_revivable`` refuses BANNED / blacklisted members here.
            403: ErrorDetail,
            404: ErrorDetail,
            502: ResponseMessage,
        },
        throttle=WriteThrottle(),
    )
    def revive_subscription(
        self,
        org_id: UUID,
        payload: schema.RevivalRequestSchema,
    ) -> schema.RevivalResponseSchema:
        """Revive the caller's most recent EXPIRED subscription in an org.

        Only ONLINE plans can be revived self-service: the response includes
        ``checkout_url`` — a hosted Stripe Checkout the member completes to
        pay the new Stripe Subscription's first invoice. OFFLINE plans are
        revived by organization staff (who record the payment they actually
        received) — the member endpoint refuses them.
        """
        qs = (
            MembershipSubscription.objects.filter(
                user=self.user(),
                organization_id=org_id,
                status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            )
            .select_related("plan", "plan__tier", "organization")
            .order_by(F("expired_at").desc(nulls_last=True))
        )
        subscription = qs.first()
        if subscription is None:
            raise Http404("No expired subscription found.")

        # Trust boundary: members must never self-record money. A member-supplied
        # amount/currency here would let an EXPIRED offline subscriber grant
        # themselves an ACTIVE period with a self-authored ledger entry.
        if subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE:
            raise HttpError(
                400,
                str(_("This subscription is managed by the organization. Contact them to renew your membership.")),
            )

        revived, checkout_url = subscription_service.revive_subscription(
            subscription,
            initial_payment=None,
            revived_by=self.user(),
        )
        # See note on ``subscribe``: return a dict so Ninja's response pipeline
        # validates the Django model through ``MySubscriptionSchema`` resolvers.
        return t.cast(
            schema.RevivalResponseSchema,
            {"subscription": revived, "checkout_url": checkout_url},
        )

    @route.post(
        "/organizations/{org_id}/billing-portal",
        url_name="create_billing_portal_session",
        response={
            201: schema.BillingPortalSessionSchema,
            400: ResponseMessage,
            404: ResponseMessage,
            502: ResponseMessage,
        },
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
