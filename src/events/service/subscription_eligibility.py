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


def subscribe_to_plan(plan: MembershipSubscriptionPlan, user: RevelUser) -> tuple[MembershipSubscription, str]:
    """Gate-checked ``/subscribe`` workflow: eligibility, Checkout, application link.

    ``start_online_subscription`` re-checks payment method, sales status, and
    Stripe readiness — the gate stack's answer can race a concurrent config
    change, so the authoritative enforcement stays there. When the caller has
    an application on file (the gated flow), the created subscription is linked
    to it so the Stripe activation can settle it COMPLETED.

    Returns:
        The (PENDING) local subscription row and the hosted Checkout URL.
    """
    eligibility_service = _ensure_plan_eligibility(user, plan)
    subscription, checkout_url = subscription_stripe_service.start_online_subscription(plan, user)
    application = eligibility_service.current_application
    if application is not None and application.status in (
        OrganizationMembershipRequest.Status.PENDING,
        OrganizationMembershipRequest.Status.APPROVED,
    ):
        application.subscription = subscription
        application.save(update_fields=["subscription", "updated_at"])
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
