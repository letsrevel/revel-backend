"""Staff endpoints for managing subscription plans, subscriptions, and payments."""

import typing as t
from uuid import UUID

from django.db import transaction
from django.db.models import OuterRef, Prefetch, QuerySet, Subquery
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja import Query
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth
from common.schema import ErrorDetail, ValidationErrorResponse
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import OrganizationPermission
from events.service import (
    subscription_refunds,
    subscription_reporting,
    subscription_service,
    subscription_uncancel,
)
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

    # ---- Metrics ----

    @route.get(
        "/subscriptions/metrics",
        url_name="get_subscription_metrics",
        response=schema.SubscriptionMetricsSchema,
        throttle=UserDefaultThrottle(),
    )
    def get_subscription_metrics(self, slug: str) -> schema.SubscriptionMetricsSchema:
        """Per-organization subscription metrics (MRR, churn, status breakdown)."""
        organization = self.get_one(slug)
        metrics = subscription_reporting.get_organization_metrics(organization)
        return schema.SubscriptionMetricsSchema.model_validate(metrics)

    # ---- Plans ----

    @route.get(
        "/plans",
        url_name="list_organization_plans",
        response=list[schema.PlanSchema],
        throttle=UserDefaultThrottle(),
    )
    def list_organization_plans(
        self, slug: str, is_active: bool | None = None
    ) -> QuerySet[models.MembershipSubscriptionPlan]:
        """List all subscription plans across every tier in the organization."""
        organization = self.get_one(slug)
        qs = (
            models.MembershipSubscriptionPlan.objects.with_active_subscription_count()
            .filter(tier__organization=organization)
            .select_related("tier")
        )
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

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
        return (
            models.MembershipSubscriptionPlan.objects.with_active_subscription_count()
            .filter(tier=tier)
            .select_related("tier")
        )

    @route.post(
        "/tiers/{tier_id}/plans",
        url_name="create_subscription_plan",
        response={
            201: schema.PlanSchema,
            400: ValidationErrorResponse | ErrorDetail,
            404: ErrorDetail,
            # ONLINE plans sync to Stripe on save, so they inherit the online-payment
            # prerequisites: 400 when Stripe Connect is missing, 422 when platform
            # fees apply but the organization's billing info is incomplete.
            422: ErrorDetail,
            502: ErrorDetail,
        },
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
        response={
            200: schema.PlanSchema,
            400: ValidationErrorResponse | ErrorDetail,
            404: ErrorDetail,
            # Same online-payment prerequisites as create: patching a plan re-syncs
            # it to Stripe, so a missing Stripe Connect answers 400 and incomplete
            # billing info answers 422.
            422: ErrorDetail,
            502: ErrorDetail,
        },
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
        "/plans/{plan_id}/migrate-subscribers",
        url_name="migrate_plan_subscribers",
        response={202: schema.MigrationAcceptedSchema, 404: ErrorDetail},
    )
    def migrate_plan_subscribers(self, slug: str, plan_id: UUID) -> tuple[int, schema.MigrationAcceptedSchema]:
        """Queue a force-migrate of all non-terminal subscribers to the plan's current price.

        Runs asynchronously: the migration issues one Stripe call per ONLINE
        subscriber, so a large plan would blow the request timeout. Returns 202
        with the number of subscribers queued; per-subscriber outcomes (migrated /
        skipped / failed) are recorded in the worker logs. No proration is applied;
        the new price takes effect at each subscriber's next renewal.
        """
        from events.tasks.subscriptions import migrate_plan_subscribers as migrate_task

        organization = self.get_one(slug)
        plan = get_object_or_404(
            models.MembershipSubscriptionPlan.objects.select_related("tier"),
            pk=plan_id,
            tier__organization=organization,
        )
        queued = (
            models.MembershipSubscription.objects.filter(plan=plan)
            .exclude(status__in=models.MembershipSubscription.TERMINAL_STATUSES)
            .count()
        )
        # Dispatch after commit so the worker sees the plan's committed price
        # (request-path dispatch under ATOMIC_REQUESTS — see engineering-notes).
        plan_id_str, user_id_str = str(plan.pk), str(self.user().pk)
        transaction.on_commit(lambda: migrate_task.delay(plan_id_str, user_id_str))
        return 202, schema.MigrationAcceptedSchema(queued=queued)

    @route.post(
        "/plans/{plan_id}/archive",
        url_name="archive_subscription_plan",
        # No 502: unlike the other Stripe-touching routes, ``archive_stripe_price``
        # swallows InvalidRequestError and maps nothing else, so a Stripe failure
        # here surfaces as a 500 — never a 502. Declaring one was dead weight.
        response={200: schema.PlanSchema, 404: ErrorDetail},
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
        response={204: None, 400: ErrorDetail, 404: ErrorDetail},
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
        search_fields=[
            "user__email",
            "user__first_name",
            "user__last_name",
            "user__preferred_name",
            "status",
            # Reverse lookup from a Stripe dashboard id back to the member it belongs to.
            "stripe_subscription_id",
            "stripe_checkout_session_id",
        ],
    )
    def list_subscriptions(
        self,
        slug: str,
        status: t.Annotated[models.MembershipSubscription.SubscriptionStatus | None, Query(None)] = None,
    ) -> QuerySet[models.MembershipSubscription]:
        """List all subscriptions for the organization, optionally filtered by ``status``."""
        organization = self.get_one(slug)
        # ``plan`` rides a Prefetch (not select_related) so the nested
        # ``PlanSchema.active_subscription_count`` reads an annotation instead
        # of issuing one COUNT per page row.
        plan_qs = models.MembershipSubscriptionPlan.objects.with_active_subscription_count().select_related("tier")
        qs = (
            models.MembershipSubscription.objects.filter(organization=organization)
            .select_related("user", "organization")
            .prefetch_related(Prefetch("plan", queryset=plan_qs))
            # ``SubscriptionSchema.member_status`` reads this annotation so the
            # list stays one query; without it the resolver falls back to a
            # lookup per row.
            .annotate(
                member_status=Subquery(
                    models.OrganizationMember.objects.filter(
                        organization_id=OuterRef("organization_id"),
                        user_id=OuterRef("user_id"),
                    ).values("status")[:1]
                )
            )
            .order_by("-created_at")
        )
        if status is not None:
            qs = qs.filter(status=status)
        return qs

    @route.get(
        "/subscriptions/{sub_id}",
        url_name="get_subscription",
        response={200: schema.SubscriptionSchema, 404: ErrorDetail},
        throttle=UserDefaultThrottle(),
    )
    def get_subscription(self, slug: str, sub_id: UUID) -> models.MembershipSubscription:
        """Get a single subscription by id."""
        organization = self.get_one(slug)
        return get_object_or_404(
            models.MembershipSubscription.objects.select_related("user", "plan", "plan__tier", "organization"),
            pk=sub_id,
            organization=organization,
        )

    @route.post(
        "/subscriptions",
        url_name="create_subscription",
        response={
            201: schema.SubscriptionSchema,
            400: ValidationErrorResponse | ErrorDetail,
            403: ErrorDetail,
            404: ErrorDetail,
        },
    )
    def create_subscription(
        self,
        slug: str,
        payload: schema.SubscriptionCreateSchema,
    ) -> tuple[int, models.MembershipSubscription]:
        """Create a subscription on behalf of a user (OFFLINE flow only).

        ONLINE (Stripe) plans must be subscribed to by the member themselves
        via ``POST /api/me/organizations/{org_id}/subscribe`` so the user can
        confirm the first payment.
        """
        organization = self.get_one(slug)
        plan = get_object_or_404(
            models.MembershipSubscriptionPlan.objects.select_related("tier"),
            pk=payload.plan_id,
            tier__organization=organization,
        )
        if plan.payment_method == models.MembershipSubscriptionPlan.PaymentMethod.ONLINE:
            raise HttpError(
                400,
                str(
                    _(
                        "ONLINE plans must be subscribed to by the member directly. "
                        "Send them to the member-facing subscribe endpoint."
                    )
                ),
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

    @route.get(
        "/subscriptions/{sub_id}/payments",
        url_name="list_subscription_payments",
        response=PaginatedResponseSchema[schema.MembershipPaymentSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_subscription_payments(self, slug: str, sub_id: UUID) -> QuerySet[models.MembershipPayment]:
        """List payments recorded against a subscription, newest first."""
        organization = self.get_one(slug)
        subscription = get_object_or_404(models.MembershipSubscription, pk=sub_id, organization=organization)
        return (
            models.MembershipPayment.objects.filter(subscription=subscription)
            .select_related("recorded_by")
            .order_by("-created_at", "-id")
        )

    @route.post(
        "/subscriptions/{sub_id}/payments",
        url_name="record_subscription_payment",
        response={
            201: schema.MembershipPaymentSchema,
            400: ValidationErrorResponse | ErrorDetail,
            404: ErrorDetail,
        },
    )
    def record_payment(
        self,
        slug: str,
        sub_id: UUID,
        payload: schema.PaymentRecordSchema,
    ) -> tuple[int, models.MembershipPayment]:
        """Record a manual payment against an OFFLINE subscription.

        ONLINE (Stripe) payments arrive via the ``invoice.paid`` webhook and
        must not be hand-recorded — that would create duplicates.
        """
        organization = self.get_one(slug)
        subscription = get_object_or_404(
            models.MembershipSubscription.objects.select_related("plan"),
            pk=sub_id,
            organization=organization,
        )
        if subscription.plan.payment_method == models.MembershipSubscriptionPlan.PaymentMethod.ONLINE:
            raise HttpError(
                400,
                str(_("ONLINE subscription payments are recorded automatically via Stripe webhooks.")),
            )
        payment = subscription_service.record_payment(
            subscription,
            amount=payload.amount,
            currency=payload.currency,
            recorded_by=self.user(),
            notes=payload.notes,
            status=payload.status,
            occurred_at=payload.occurred_at,
        )
        return 201, payment

    @route.post(
        "/subscriptions/{sub_id}/cancel",
        url_name="cancel_subscription",
        response={
            200: schema.SubscriptionSchema,
            400: ErrorDetail,
            404: ErrorDetail,
            409: schema.SubscriptionActivationPendingSchema,
            502: ErrorDetail,
        },
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
        "/subscriptions/{sub_id}/uncancel",
        url_name="uncancel_subscription",
        # 403: refused while the membership is PAUSED / BANNED — restore it first.
        response={
            200: schema.SubscriptionSchema,
            400: ErrorDetail,
            403: ErrorDetail,
            404: ErrorDetail,
            502: ErrorDetail,
        },
    )
    def uncancel_subscription(self, slug: str, sub_id: UUID) -> models.MembershipSubscription:
        """Undo a scheduled cancellation, so the subscription keeps renewing."""
        organization = self.get_one(slug)
        subscription = get_object_or_404(
            models.MembershipSubscription.objects.select_related("plan", "plan__tier", "user"),
            pk=sub_id,
            organization=organization,
        )
        return subscription_uncancel.uncancel_subscription(subscription, staff=True)

    @route.post(
        "/subscriptions/{sub_id}/pause",
        url_name="pause_subscription",
        response={200: schema.SubscriptionSchema, 400: ErrorDetail, 404: ErrorDetail, 502: ErrorDetail},
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
        response={200: schema.SubscriptionSchema, 400: ErrorDetail, 404: ErrorDetail, 502: ErrorDetail},
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

    @route.post(
        "/subscriptions/{sub_id}/revive",
        url_name="revive_subscription",
        response={
            200: schema.StaffRevivalResponseSchema,
            400: ValidationErrorResponse | ErrorDetail,
            403: ErrorDetail,
            404: ErrorDetail,
            502: ErrorDetail,
        },
    )
    def revive_subscription(
        self,
        slug: str,
        sub_id: UUID,
        payload: schema.RevivalRequestSchema,
    ) -> schema.StaffRevivalResponseSchema:
        """Force-revive an EXPIRED subscription within the org's revival window.

        For OFFLINE plans, include the payment amount/currency. For ONLINE
        plans, a hosted Stripe Checkout for a fresh Stripe Subscription is
        created — the member is emailed the checkout link (staff cannot pay on
        their behalf) and the response also returns ``checkout_url`` so staff
        can hand it over out-of-band.
        """
        organization = self.get_one(slug)
        subscription = get_object_or_404(
            models.MembershipSubscription.objects.select_related("plan", "plan__tier", "organization", "user"),
            pk=sub_id,
            organization=organization,
        )
        initial: InitialPayment | None = None
        if payload.amount is not None and payload.currency is not None:
            initial = InitialPayment(
                amount=payload.amount,
                currency=payload.currency,
                recorded_by=self.user(),
                notes=payload.notes,
            )
        revived, checkout_url = subscription_service.revive_subscription(
            subscription,
            initial_payment=initial,
            revived_by=self.user(),
            # Staff bypass the PAUSED-sales gate (they ARE the org); the
            # capacity cap still applies inside the service.
            enforce_sales_status=False,
        )
        # Return a dict (not a constructed schema): Ninja's response pipeline
        # validates it via ``StaffRevivalResponseSchema``. Pre-validating the
        # inner ``SubscriptionSchema`` would cause Ninja's wrap-validator to
        # re-run resolvers against the already-validated schema instance
        # (which lacks ``obj.user``/``obj.organization``), breaking serialization.
        return t.cast(
            schema.StaffRevivalResponseSchema,
            {"subscription": revived, "checkout_url": checkout_url},
        )

    # ---- Payments ----

    @route.get(
        "/subscription-payments",
        url_name="list_organization_subscription_payments",
        response=PaginatedResponseSchema[schema.OrganizationMembershipPaymentSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(
        Searching,
        search_fields=[
            "subscription__user__email",
            "subscription__user__first_name",
            "subscription__user__last_name",
            "subscription__user__preferred_name",
            # Reverse lookup from a Stripe payout line back to the member it paid for.
            "stripe_invoice_id",
            "stripe_payment_intent_id",
            "stripe_refund_id",
        ],
    )
    def list_organization_subscription_payments(
        self,
        slug: str,
        status: t.Annotated[models.MembershipPayment.PaymentStatus | None, Query(None)] = None,
        plan_id: t.Annotated[UUID | None, Query(None)] = None,
    ) -> QuerySet[models.MembershipPayment]:
        """Org-wide membership payment ledger, newest first — the reconciliation surface."""
        organization = self.get_one(slug)
        return subscription_reporting.organization_payments(organization, status=status, plan_id=plan_id)

    @route.post(
        "/payments/{payment_id}/refund",
        url_name="refund_subscription_payment",
        response={200: schema.MembershipPaymentSchema, 400: ErrorDetail, 404: ErrorDetail},
    )
    def refund_payment(
        self,
        slug: str,
        payment_id: UUID,
        payload: schema.RefundSchema,
    ) -> models.MembershipPayment:
        """Mark a recorded payment as refunded (record-only in MVP).

        ONLINE (Stripe) payments are refused: this endpoint never moves money,
        so accepting one would flip the ledger to REFUNDED (and auto-cancel the
        subscription) while the member's charge stays captured on Stripe.
        Refund those from the Stripe Dashboard — the ``charge.refunded``
        webhook records the refund here automatically.
        """
        organization = self.get_one(slug)
        payment = get_object_or_404(
            models.MembershipPayment.objects.select_related("subscription", "subscription__plan"),
            pk=payment_id,
            subscription__organization=organization,
        )
        if payment.subscription.plan.payment_method == models.MembershipSubscriptionPlan.PaymentMethod.ONLINE:
            raise HttpError(
                400,
                str(
                    _(
                        "ONLINE payments must be refunded from the Stripe Dashboard; "
                        "the refund is recorded here automatically."
                    )
                ),
            )
        return subscription_refunds.refund_payment(payment, recorded_by=self.user(), notes=payload.notes)
