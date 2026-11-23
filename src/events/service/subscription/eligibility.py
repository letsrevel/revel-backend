"""Bridge between the membership eligibility gate stack and the paid subscription flow.

Owns the two places the plan-bearing path must consult the gates:

- ``subscribe_to_plan`` — the ``/subscribe`` workflow: gate check, activation,
  and linking the created subscription to the caller's application. Activation
  is Stripe Checkout for an ONLINE plan; for a FREE plan it is a direct,
  already-ACTIVE subscription with no checkout URL at all (#832).
- ``ensure_tier_change_allowed`` — cross-tier ``change-plan`` targets run the
  gate stack against the destination tier; same-tier swaps are deliberately
  not re-gated (the member already passed that tier's gates once).

Lives apart from ``subscription.lifecycle`` / ``subscription.stripe.checkout`` so
neither money module grows a dependency on ``membership_manager``.
"""

from accounts.models import RevelUser
from events.exceptions import AlreadyMemberError
from events.models import MembershipSubscription, MembershipSubscriptionPlan, OrganizationMembershipRequest
from events.service.membership_manager import MembershipApplicationIneligibleError, MembershipEligibilityService
from events.service.membership_manager.enums import MembershipReasonCode
from events.service.subscription.core import create_subscription
from events.service.subscription.offline import open_offline_subscription
from events.service.subscription.stripe import checkout as stripe_checkout
from events.utils.membership_access import active_member_refused_plan


def _ensure_plan_eligibility(
    user: RevelUser, plan: MembershipSubscriptionPlan, *, changing_plan: bool = False
) -> MembershipEligibilityService:
    """Run the gate stack for *user* against *plan*'s tier; raise on refusal.

    ``DUPLICATE_ACTIVE_SUBSCRIPTION`` falls through on purpose: the
    subscription machinery gives the richer answer for that case — a PENDING
    checkout resumes (409 + fresh checkout_url) instead of dead-ending, a
    genuine duplicate still 400s in ``create_subscription``, and a change-plan
    caller necessarily holds a live subscription.

    OFFLINE plans are the exception: nothing downstream reuses a subscription held on another plan, so a
    duplicate is refused here with the structured verdict (only ``AWAITING_OFFLINE_PAYMENT`` falls through).
    A plan change (``changing_plan``) always holds a live subscription, so it keeps the fall-through.

    Raises:
        MembershipApplicationIneligibleError: Rendered by the registered
            handler as a 400 carrying the serialized eligibility verdict —
            the same shape ``/apply`` returns.
    """
    eligibility_service = MembershipEligibilityService(
        user=user, organization=plan.tier.organization, tier=plan.tier, plan=plan
    )
    verdict = eligibility_service.check_eligibility()
    # AWAITING_OFFLINE_PAYMENT on an OFFLINE plan also falls through: a repeated /subscribe returns
    # the same awaiting subscription (``open_offline_subscription`` is idempotent).
    is_offline = plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE
    passes_through = (
        verdict.reason_code == MembershipReasonCode.DUPLICATE_ACTIVE_SUBSCRIPTION and (changing_plan or not is_offline)
    ) or (verdict.reason_code == MembershipReasonCode.AWAITING_OFFLINE_PAYMENT and is_offline)
    if not verdict.allowed and not passes_through:
        raise MembershipApplicationIneligibleError(
            verdict.reason or "Refused by the membership eligibility gates.", verdict
        )
    return eligibility_service


def _refuse_active_member(plan: MembershipSubscriptionPlan, user: RevelUser) -> None:
    """Refuse a FREE plan subscription from an ACTIVE member (#1518).

    Members without a subscription row (owner-added, imported, open-ended) would otherwise have their
    tier overwritten by a FREE plan. The member row is read directly, not through the gate verdict:
    owners and staff pass the gates first.
    A paid (ONLINE / OFFLINE) plan at any tier, including the member's own, is allowed and follows the gates.
    A live subscription is skipped: that case keeps its own answer (resume a pending checkout,
    idempotent OFFLINE repeat, duplicate refusal). See ``active_member_refused_plan``.
    Staff-created subscriptions do not come through here.

    Raises:
        AlreadyMemberError: Rendered as a 400 with ``code="already_member"``.
    """
    # ponytail: plain read before create_subscription's transaction. A concurrent staff activation
    # or cancel can slip between this check and the write; the window is narrow. Upgrade path: repeat
    # the check inside create_subscription under select_for_update of the member row (member-facing flag).
    if active_member_refused_plan(user, plan):
        raise AlreadyMemberError


def subscribe_to_plan(plan: MembershipSubscriptionPlan, user: RevelUser) -> tuple[MembershipSubscription, str | None]:
    """Gate-checked ``/subscribe`` workflow: eligibility, activation, application link.

    ONLINE plans go through Stripe Checkout: ``start_online_subscription``
    re-checks payment method, sales status, and Stripe readiness — the gate
    stack's answer can race a concurrent config change, so the authoritative
    enforcement stays there. When the caller has an application on file (the
    gated flow), the created subscription is linked to it so the Stripe
    activation can settle it COMPLETED.

    OFFLINE plans open a PENDING subscription (the member is held as AWAITING_PAYMENT) and
    notify them how to pay; the first payment recorded by staff activates it. Repeating the
    call returns the same subscription.

    FREE plans have no Stripe object and no money to collect, so
    ``create_subscription`` lands the row ACTIVE (open-ended, ``LIFETIME``) and
    materializes the member in one transaction. No activation webhook will ever
    run for it, so the originating application is settled COMPLETED here
    instead — through the same status-filtered update the Stripe path defers to
    :mod:`events.service.subscription.stripe.sync`.

    An ACTIVE member who holds no live subscription is refused first with ``AlreadyMemberError``
    (400 ``already_member``) for a FREE plan, before any member write (#1518). A paid plan at any tier,
    including their own, goes on.

    Returns:
        The local subscription row and the hosted Checkout URL — ``None`` for a
        FREE plan (already ACTIVE) or an OFFLINE plan (PENDING until paid).
    """
    _refuse_active_member(plan, user)
    eligibility_service = _ensure_plan_eligibility(user, plan)
    application = eligibility_service.current_application
    if application is not None and application.status not in (
        OrganizationMembershipRequest.Status.PENDING,
        OrganizationMembershipRequest.Status.APPROVED,
    ):
        application = None
    payment_method = plan.payment_method
    checkout_url: str | None = None
    if payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE:
        # Opens the awaiting-payment subscription and links/approves the application itself.
        return open_offline_subscription(plan, user, application=application, member_facing=True), None
    if payment_method == MembershipSubscriptionPlan.PaymentMethod.FREE:
        subscription = create_subscription(plan, user)
    else:
        subscription, checkout_url = stripe_checkout.start_online_subscription(plan, user)
    if application is not None:
        application.subscription = subscription
        application.save(update_fields=["subscription", "updated_at"])
    if payment_method == MembershipSubscriptionPlan.PaymentMethod.FREE:
        # lazy: subscription.stripe.sync -> ... -> subscription.lifecycle imports
        # this module back, so a top-level import would cycle.
        from events.service.subscription.stripe.sync import settle_originating_application

        settle_originating_application(subscription)
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
    _ensure_plan_eligibility(subscription.user, new_plan, changing_plan=True)
