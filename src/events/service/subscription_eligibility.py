"""Bridge between the membership eligibility gate stack and the paid subscription flow.

Owns the two places the paid path must consult the gates:

- ``subscribe_to_plan`` — the ``/subscribe`` workflow: gate check, Stripe
  Checkout, and linking the created subscription to the caller's application.
- ``ensure_tier_change_allowed`` — cross-tier ``change-plan`` targets run the
  gate stack against the destination tier; same-tier swaps are deliberately
  not re-gated (the member already passed that tier's gates once).

Lives apart from ``subscription_service`` / ``subscription_stripe_service`` so
neither money module grows a dependency on ``membership_manager``.
"""

from accounts.models import RevelUser
from events.models import MembershipSubscription, MembershipSubscriptionPlan, OrganizationMembershipRequest
from events.service import subscription_stripe_service
from events.service.membership_manager import MembershipApplicationIneligibleError, MembershipEligibilityService
from events.service.membership_manager.enums import MembershipReasonCode
from events.service.subscription_core import create_subscription


def _ensure_plan_eligibility(user: RevelUser, plan: MembershipSubscriptionPlan) -> MembershipEligibilityService:
    """Run the gate stack for *user* against *plan*'s tier; raise on refusal.

    ``DUPLICATE_ACTIVE_SUBSCRIPTION`` falls through on purpose: the
    subscription machinery gives the richer answer for that case — a PENDING
    checkout resumes (409 + fresh checkout_url) instead of dead-ending, a
    genuine duplicate still 400s in ``create_subscription``, and a change-plan
    caller necessarily holds a live subscription.

    Raises:
        MembershipApplicationIneligibleError: Rendered by the registered
            handler as a 400 carrying the serialized eligibility verdict —
            the same shape ``/apply`` returns.
    """
    eligibility_service = MembershipEligibilityService(
        user=user, organization=plan.tier.organization, tier=plan.tier, plan=plan
    )
    verdict = eligibility_service.check_eligibility()
    if not verdict.allowed and verdict.reason_code != MembershipReasonCode.DUPLICATE_ACTIVE_SUBSCRIPTION:
        raise MembershipApplicationIneligibleError(
            verdict.reason or "Refused by the membership eligibility gates.", verdict
        )
    return eligibility_service


def subscribe_to_plan(plan: MembershipSubscriptionPlan, user: RevelUser) -> tuple[MembershipSubscription, str | None]:
    """Gate-checked ``/subscribe`` workflow: eligibility, activation, application link.

    ONLINE plans go through Stripe Checkout: ``start_online_subscription``
    re-checks payment method, sales status, and Stripe readiness — the gate
    stack's answer can race a concurrent config change, so the authoritative
    enforcement stays there. When the caller has an application on file (the
    gated flow), the created subscription is linked to it so the Stripe
    activation can settle it COMPLETED.

    FREE plans have no Stripe object and no money to collect, so
    ``create_subscription`` lands the row ACTIVE (open-ended, ``LIFETIME``) and
    materializes the member in one transaction. No activation webhook will ever
    run for it, so the originating application is settled COMPLETED here
    instead — through the same status-filtered update the Stripe path defers to
    :mod:`events.service.subscription_stripe_sync`.

    Returns:
        The local subscription row and the hosted Checkout URL — ``None`` for a
        FREE plan, whose subscription is already ACTIVE.
    """
    eligibility_service = _ensure_plan_eligibility(user, plan)
    is_free = plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.FREE
    checkout_url: str | None
    if is_free:
        subscription = create_subscription(plan, user)
        checkout_url = None
    else:
        subscription, checkout_url = subscription_stripe_service.start_online_subscription(plan, user)
    application = eligibility_service.current_application
    if application is not None and application.status in (
        OrganizationMembershipRequest.Status.PENDING,
        OrganizationMembershipRequest.Status.APPROVED,
    ):
        application.subscription = subscription
        application.save(update_fields=["subscription", "updated_at"])
    if is_free:
        # lazy: subscription_stripe_sync -> ... -> subscription_service imports
        # this module back, so a top-level import would cycle.
        from events.service.subscription_stripe_sync import _settle_originating_application

        _settle_originating_application(subscription)
    return subscription, checkout_url


def ensure_tier_change_allowed(subscription: MembershipSubscription, new_plan: MembershipSubscriptionPlan) -> None:
    """Gate a cross-tier plan change; same-tier swaps are not re-gated.

    Without this, a member could subscribe to an ungated tier's plan and then
    self-service ``change-plan`` onto a gated tier's plan — landing at the
    gated tier having never passed its questionnaire or manual approval (the
    member-tier sync repoints ``member.tier`` straight off the plan).

    Raises:
        MembershipApplicationIneligibleError: When the destination tier's
            gates refuse the member.
    """
    if new_plan.tier_id == subscription.plan.tier_id:
        return
    _ensure_plan_eligibility(subscription.user, new_plan)
