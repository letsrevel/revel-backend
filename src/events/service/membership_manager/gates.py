"""Eligibility gate classes for the membership eligibility system.

Each gate performs a specific membership-policy check. Gates are composed together
by MembershipEligibilityService to determine if a user can join an org at a target tier.
"""

from __future__ import annotations

import abc
import typing as t
import uuid

from django.utils import timezone
from django.utils.translation import gettext as _

from events.models import (
    MembershipSubscriptionPlan,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    WhitelistRequest,
)
from questionnaires.models import QuestionnaireEvaluation, QuestionnaireSubmission

from .enums import TERMINAL_REJECTION_CODES, MembershipNextStep, MembershipReasonCode, Reasons
from .resolvers import resolve_requires_membership_approval
from .types import MembershipEligibility

if t.TYPE_CHECKING:
    from accounts.models import RevelUser
    from events.models import MembershipTier, OrganizationQuestionnaire
    from questionnaires.models import Questionnaire

    from .service import MembershipEligibilityService


class BaseMembershipEligibilityGate(abc.ABC):
    """Abstract base class for a composable membership-eligibility check."""

    def __init__(self, handler: "MembershipEligibilityService") -> None:
        """Store a reference back to the orchestrator so gates can share prefetched state."""
        self.handler = handler
        self.user: RevelUser = handler.user
        self.organization: Organization = handler.organization
        self.tier: MembershipTier | None = handler.tier
        self.plan: MembershipSubscriptionPlan | None = handler.plan

    @abc.abstractmethod
    def check(self) -> MembershipEligibility | None:
        """Perform the gate check.

        Returns:
            MembershipEligibility (blocking or allowing) to short-circuit the chain,
            None to fall through to the next gate.
        """

    def _has_active_membership(self) -> bool:
        """True when the user has any ACTIVE membership in this org (at any tier)."""
        return any(
            m.status == OrganizationMember.MembershipStatus.ACTIVE for m in self.handler.user_memberships.values()
        )

    def _allow(self, **extra: t.Any) -> MembershipEligibility:
        """Build an allowing eligibility with this gate's (org, tier, plan) context preset.

        Extra kwargs are forwarded to MembershipEligibility (e.g. ``next_step=ALREADY_MEMBER``).
        """
        return MembershipEligibility(
            allowed=True,
            organization_id=self.organization.pk,
            tier_id=self.tier.pk if self.tier else None,
            plan_id=self.plan.pk if self.plan else None,
            **extra,
        )

    def _block(
        self,
        reason: Reasons,
        *,
        next_step: MembershipNextStep | None = None,
        **extra: t.Any,
    ) -> MembershipEligibility:
        """Build a blocking eligibility with this gate's (org, tier, plan) context preset.

        Takes the :class:`Reasons` member and derives both the translated,
        human-readable ``reason`` and the stable machine-readable
        ``reason_code`` — state-machine decisions must switch on the latter.
        Extra kwargs are forwarded to MembershipEligibility (e.g. ``application_id=``).
        """
        return MembershipEligibility(
            allowed=False,
            organization_id=self.organization.pk,
            tier_id=self.tier.pk if self.tier else None,
            plan_id=self.plan.pk if self.plan else None,
            reason=_(reason),
            reason_code=reason.code,
            next_step=next_step,
            **extra,
        )


class PrivilegedAccessGate(BaseMembershipEligibilityGate):
    """Gate #1: Owners and staff always pass."""

    def check(self) -> MembershipEligibility | None:
        """Allow immediately when the user owns or staffs the organization."""
        if self.handler.is_owner or self.user.id in self.handler.staff_ids:
            return self._allow()
        return None


class OrgVisibilityGate(BaseMembershipEligibilityGate):
    """Gate #2: Org must be visible to the user via Organization.for_user()."""

    def check(self) -> MembershipEligibility | None:
        """Block when the organization is not visible to the user."""
        if Organization.objects.for_user(self.user).filter(pk=self.organization.pk).exists():
            return None
        return self._block(Reasons.ORG_NOT_VISIBLE)


class BlacklistGate(BaseMembershipEligibilityGate):
    """Gate #3: Hard blacklist blocks; fuzzy match routes through whitelist UX."""

    def check(self) -> MembershipEligibility | None:
        """Block hard-blacklisted users; route fuzzy matches through whitelist verification."""
        if self.handler.is_hard_blacklisted:
            return self._block(Reasons.BLACKLISTED)

        if not self.handler.fuzzy_matched_blacklist_entries:
            return None
        if self.handler.is_whitelisted:
            return None

        whitelist_request = self.handler.whitelist_request
        if whitelist_request:
            if whitelist_request.status == WhitelistRequest.Status.PENDING:
                # Recoverable: the user is waiting on a staff whitelist decision.
                # The explicit next_step keeps advance-on-read from ever treating
                # this as a terminal verdict and lets the FE render a wait state.
                return self._block(
                    Reasons.WHITELIST_PENDING,
                    next_step=MembershipNextStep.WAIT_FOR_WHITELIST_APPROVAL,
                )
            if whitelist_request.status == WhitelistRequest.Status.REJECTED:
                return self._block(Reasons.WHITELIST_REJECTED)

        # No request yet: surface verification requirement; FE routes through existing whitelist endpoint.
        return self._block(Reasons.REQUIRES_VERIFICATION, next_step=MembershipNextStep.REQUIRES_INVITATION)


class AlreadyMemberGate(BaseMembershipEligibilityGate):
    """Gate #4: ACTIVE membership at target tier short-circuits ALREADY_MEMBER.

    ACTIVE membership at a different tier falls through so subsequent gates can
    re-gate tier upgrades (S7). A PAUSED membership hard-blocks *regardless of
    the target tier*: ``OrganizationMember`` is unique per (org, user), so the
    downstream ``update_or_create`` writes the same row whatever tier the user
    applies at — a tier-scoped check would let a paused member self-un-pause by
    applying at a different tier. PAUSED is admin/Stripe-imposed and the user
    must contact the org to resume.

    A **tier-less** application (``tier is None``, the legacy staff-approval
    path) from a user who is already an ACTIVE member likewise short-circuits
    ALREADY_MEMBER: such a row never auto-completes, so it just sits PENDING
    until staff approve it — at which point the member's existing tier is
    silently overwritten with whatever tier staff pick, with no signal that the
    "applicant" was already active. The legacy sibling endpoint
    (``organization_service.membership.create_membership_request``) refuses
    outright with ``AlreadyMemberError``; this mirrors it.

    When the user is applying via the free path (``plan is None``) but already has
    a non-terminal :class:`MembershipSubscription` in this org, block — they
    cannot self-promote a paid membership through the free flow.
    """

    def check(self) -> MembershipEligibility | None:
        """Allow with ALREADY_MEMBER at target tier (or tier-less); block PAUSED / free-promotion bypass."""
        # Free-apply bypass guard: a user with a non-terminal subscription must not
        # be able to clobber their paid membership via the free path.
        if self.plan is None and self.handler.has_non_terminal_subscription:
            return self._block(Reasons.DUPLICATE_ACTIVE_SUBSCRIPTION)

        # PAUSED blocks whatever the target tier is (see class docstring); the
        # user has no in-app recourse so we block with next_step=None.
        if any(m.status == OrganizationMember.MembershipStatus.PAUSED for m in self.handler.user_memberships.values()):
            return self._block(Reasons.MEMBERSHIP_PAUSED)

        if self.tier is None:
            # Tier-less: there is no target tier to compare against, so any
            # ACTIVE membership means there is nothing left to apply for.
            if self._has_active_membership():
                return self._allow(next_step=MembershipNextStep.ALREADY_MEMBER)
            return None
        membership = self.handler.user_memberships.get(self.tier.pk)
        if membership is None:
            return None
        if membership.status == OrganizationMember.MembershipStatus.ACTIVE:
            return self._allow(next_step=MembershipNextStep.ALREADY_MEMBER)
        return None


class AcceptRequestsGate(BaseMembershipEligibilityGate):
    """Gate #5: Org must have accept_membership_requests=True."""

    def check(self) -> MembershipEligibility | None:
        """Block when the org is not accepting new membership requests."""
        if self.organization.accept_membership_requests:
            return None
        return self._block(Reasons.NOT_ACCEPTING_REQUESTS, next_step=MembershipNextStep.REQUIRES_INVITATION)


class TierAvailabilityGate(BaseMembershipEligibilityGate):
    """Gate #6: Target tier must belong to this org. When plan is provided, plan must be active and on tier.

    A tier that carries at least one active subscription plan is monetized:
    the free path must not hand it out (a free /apply would otherwise grant a
    paid tier's benefits without any payment). Plan-bearing applications fall
    through to :class:`PaymentReadyGate` instead.
    """

    def check(self) -> MembershipEligibility | None:
        """Block when the target tier or plan is not available for this organization."""
        if self.tier is not None and self.tier.organization_id != self.organization.pk:
            return self._block(Reasons.TIER_UNAVAILABLE)
        if self.plan is None:
            if self.tier is not None and self.handler.tier_has_active_plan:
                return self._block(Reasons.TIER_REQUIRES_SUBSCRIPTION)
            return None
        if not self.plan.is_active:
            return self._block(Reasons.PLAN_UNAVAILABLE)
        if self.tier is not None and self.plan.tier_id != self.tier.pk:
            return self._block(Reasons.PLAN_UNAVAILABLE)
        return None


class ApplicationStatusGate(BaseMembershipEligibilityGate):
    """Gate #7: Block on terminal REJECTED application; let other states pass through.

    REJECTED is terminal for **this** application instance — once an OMR row
    flips to REJECTED it never advances again (see ``advance_application``).
    The user, however, may submit a fresh ``POST /apply`` which creates a NEW
    PENDING row (the partial unique constraint
    ``unique_pending_application_per_user_org_tier`` only blocks duplicates
    among status=PENDING rows). This gate inspects only the **most-recent**
    application for (user, org, tier) — see
    ``MembershipEligibilityService.applications_by_tier`` — so a new PENDING
    row supersedes the old REJECTED one and the gate falls through to let the
    remaining gates evaluate the fresh attempt from scratch.

    Practical effect: REJECTED is terminal for the **row**, but not for the
    user's ability to reapply — *unless* the cause of the rejection is itself
    still terminal (see ``check``). Orgs that want to permanently block a user
    must add them to the blacklist.
    """

    def check(self) -> MembershipEligibility | None:
        """Block when the current application is REJECTED; fall through otherwise."""
        app = self.handler.current_application
        if app is None:
            return None
        if app.status != OrganizationMembershipRequest.Status.REJECTED:
            return None

        # Offering REAPPLY is only honest while the cause is recoverable. A
        # still-terminal questionnaire verdict re-fires on the fresh PENDING row
        # at its very first read: ``advance_application`` flips it straight back
        # to REJECTED and sends another MEMBERSHIP_REQUEST_REJECTED notification.
        # Unthrottled that is an unbounded junk-row + notification loop, and the
        # rows PROTECT the tier so they also wedge tier deletion. Surfacing the
        # terminal verdict (next_step=None) makes the controller's hard-block set
        # 403 instead, as its comment promises.
        questionnaire_verdict = MembershipQuestionnaireGate(self.handler).check()
        if questionnaire_verdict is not None and questionnaire_verdict.reason_code in TERMINAL_REJECTION_CODES:
            questionnaire_verdict.application_id = app.pk
            return questionnaire_verdict

        # REAPPLY tells the controller (and FE) that a fresh POST /apply is
        # the recourse: the new PENDING row supersedes this REJECTED one.
        # Without it the controller's hard-block set (next_step=None) made
        # the documented re-apply path unreachable via the API.
        return self._block(
            Reasons.APPLICATION_REJECTED,
            next_step=MembershipNextStep.REAPPLY,
            application_id=app.pk,
        )


class MembershipQuestionnaireGate(BaseMembershipEligibilityGate):
    """Gate #8: Enforce the resolved membership questionnaire (tier override or org default)."""

    def check(self) -> MembershipEligibility | None:
        """Evaluate the resolved membership questionnaire against the user's latest submission."""
        questionnaire_oq = self.handler.membership_questionnaire_oq
        if questionnaire_oq is None:
            return None

        # members_exempt grandfathers existing members: any user with an ACTIVE membership
        # at any tier in this org skips the questionnaire. Orgs that want to re-gate tier
        # upgrades set members_exempt=False on the resolved questionnaire.
        if questionnaire_oq.members_exempt and self._has_active_membership():
            return None

        questionnaire = questionnaire_oq.questionnaire
        submission = self.handler.latest_questionnaire_submission

        if submission is None:
            return self._block_submit(questionnaire.pk)

        # Submissions exist but evaluation isn't required → pass.
        if not questionnaire_oq.requires_evaluation:
            return None

        evaluation: QuestionnaireEvaluation | None = getattr(submission, "evaluation", None)
        if evaluation is None:
            return self._block_pending(questionnaire.pk)

        return self._evaluate(questionnaire_oq, questionnaire, submission, evaluation)

    def _evaluate(
        self,
        questionnaire_oq: "OrganizationQuestionnaire",
        questionnaire: "Questionnaire",
        submission: QuestionnaireSubmission,
        evaluation: QuestionnaireEvaluation,
    ) -> MembershipEligibility | None:
        """Branch on the evaluation status to produce the final verdict."""
        status = evaluation.status
        approved = QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
        pending = QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW

        if status == pending:
            return self._block_pending(questionnaire.pk)

        if status == approved:
            if (
                questionnaire_oq.max_submission_age
                and (evaluation.updated_at + questionnaire_oq.max_submission_age) < timezone.now()
            ):
                return self._block_submit(questionnaire.pk)
            return None

        # Rejected: branch on retake policy.
        return self._handle_rejected(questionnaire, submission)

    def _handle_rejected(
        self, questionnaire: "Questionnaire", submission: QuestionnaireSubmission
    ) -> MembershipEligibility:
        """Apply the attempts cap, then the retake cooldown, for a rejected evaluation.

        Order mirrors ``_validate_resubmission`` in
        :mod:`events.service.membership_questionnaire_service` exactly: the cap
        outranks the retake policy there, so it must outrank it here too — a
        gate that promised SUBMIT_QUESTIONNAIRE (or a cooldown that expires into
        one) to a user at the cap would send them to a guaranteed 400.
        """
        if 0 < questionnaire.max_attempts <= self.handler.questionnaire_attempt_count:
            return self._block(
                Reasons.MEMBERSHIP_QUESTIONNAIRE_ATTEMPTS_EXHAUSTED,
                questionnaire_id=questionnaire.pk,
            )

        if questionnaire.can_retake_after is not None and submission.submitted_at is not None:
            retry_on = submission.submitted_at + questionnaire.can_retake_after
            if retry_on > timezone.now():
                return self._block(
                    Reasons.MEMBERSHIP_QUESTIONNAIRE_RETAKE_COOLDOWN,
                    next_step=MembershipNextStep.WAIT_TO_RETAKE_QUESTIONNAIRE,
                    questionnaire_id=questionnaire.pk,
                    retry_on=retry_on,
                )
            return self._block_submit(questionnaire.pk)

        # No retake → terminal failure.
        return self._block(
            Reasons.MEMBERSHIP_QUESTIONNAIRE_FAILED,
            questionnaire_id=questionnaire.pk,
        )

    def _block_submit(self, questionnaire_id: uuid.UUID) -> MembershipEligibility:
        """Build the SUBMIT_QUESTIONNAIRE blocking verdict."""
        return self._block(
            Reasons.MEMBERSHIP_QUESTIONNAIRE_MISSING,
            next_step=MembershipNextStep.SUBMIT_QUESTIONNAIRE,
            questionnaire_id=questionnaire_id,
        )

    def _block_pending(self, questionnaire_id: uuid.UUID) -> MembershipEligibility:
        """Build the WAIT_FOR_QUESTIONNAIRE_EVALUATION blocking verdict."""
        return self._block(
            Reasons.MEMBERSHIP_QUESTIONNAIRE_PENDING,
            next_step=MembershipNextStep.WAIT_FOR_QUESTIONNAIRE_EVALUATION,
            questionnaire_id=questionnaire_id,
        )


class ManualApprovalGate(BaseMembershipEligibilityGate):
    """Gate #9: Enforce the manual-approval policy against the user's latest application.

    When approval is required:

    - APPROVED application → fall through (the staff decision is in).
    - PENDING application → block with ``WAIT_FOR_APPROVAL`` + ``application_id``.
    - No application (or a terminal CANCELLED/COMPLETED one) → fall through so the
      user can actually apply, flagging
      ``handler.approval_required_annotation`` so ``check_eligibility``'s final
      allowed verdict carries ``reason_code=REQUIRES_APPROVAL`` (no prose, no
      ``next_step``). Blocking here soft-locked users who had never applied: the
      FE rendered a disabled "application pending" state with no path to the
      Join CTA (#787).

    REJECTED never reaches this gate — :class:`ApplicationStatusGate` blocks it
    with ``next_step=REAPPLY``.

    The fall-through (rather than an ``_allow``) is load-bearing: a returned
    verdict short-circuits the chain, and :class:`PaymentReadyGate` runs *after*
    this gate, so a plan-bearing check with no application must still reach its
    ``SUBMIT_APPLICATION`` block (the annotation is how it knows approval is
    outstanding).
    """

    def check(self) -> MembershipEligibility | None:
        """Block a PENDING application; pass through when approved or not yet applied."""
        if not resolve_requires_membership_approval(self.organization, self.tier):
            return None
        app = self.handler.current_application
        if app and app.status == OrganizationMembershipRequest.Status.PENDING:
            return self._block(
                Reasons.REQUIRES_APPROVAL,
                next_step=MembershipNextStep.WAIT_FOR_APPROVAL,
                application_id=app.pk,
            )
        if app and app.status == OrganizationMembershipRequest.Status.APPROVED:
            return None
        # No application on file (or a terminal one): the user must be able to
        # apply. Annotate so the final verdict still tells the FE that joining
        # goes through staff approval.
        self.handler.approval_required_annotation = True
        return None


class PaymentReadyGate(BaseMembershipEligibilityGate):
    """Gate #10: Final pre-payment readiness check. No-op when plan is not provided.

    A plan-bearing check that survives every prior gate ends here with an
    *allowing* ``PROCEED_TO_PAYMENT`` verdict — ``/subscribe`` opens Checkout on
    it. Blocks when the plan is not actually payable (offline plan, org not
    Stripe-connected, plan paused or at its sales cap, duplicate non-terminal
    subscription), or when approval is required but the user has nothing on
    file yet (``SUBMIT_APPLICATION``: :class:`ManualApprovalGate` deliberately
    falls through in that state so the free path can apply — the paid path must
    not slip past staff approval through the same hole).
    """

    def check(self) -> MembershipEligibility | None:
        """Readiness check for a plan-bearing verdict; allow ends in PROCEED_TO_PAYMENT."""
        if self.plan is None:
            return None
        if self.handler.approval_required_annotation:
            # Approval required but nothing on file: the user must create the
            # application first so staff have something to approve. No prose —
            # "your application is awaiting staff approval" would be a lie when
            # no application exists (mirrors check_eligibility's annotation
            # shaping); the code + next_step alone drive the FE.
            return MembershipEligibility(
                allowed=False,
                organization_id=self.organization.pk,
                tier_id=self.tier.pk if self.tier else None,
                plan_id=self.plan.pk if self.plan else None,
                reason_code=MembershipReasonCode.REQUIRES_APPROVAL,
                next_step=MembershipNextStep.SUBMIT_APPLICATION,
            )
        if self.plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
            return self._block(Reasons.PLAN_NOT_ONLINE)
        if not self.organization.is_stripe_connected:
            return self._block(Reasons.ORG_NOT_STRIPE_CONNECTED)
        if self.plan.sales_status != MembershipSubscriptionPlan.SalesStatus.OPEN:
            return self._block(Reasons.PLAN_UNAVAILABLE)
        if self.plan.max_subscriptions is not None and self.plan.occupied_slot_count() >= self.plan.max_subscriptions:
            return self._block(Reasons.PLAN_UNAVAILABLE)
        if self.handler.has_non_terminal_subscription:
            return self._block(Reasons.DUPLICATE_ACTIVE_SUBSCRIPTION)
        return self._allow(next_step=MembershipNextStep.PROCEED_TO_PAYMENT)


MEMBERSHIP_ELIGIBILITY_GATES: list[type[BaseMembershipEligibilityGate]] = [
    PrivilegedAccessGate,
    OrgVisibilityGate,
    BlacklistGate,
    AlreadyMemberGate,
    AcceptRequestsGate,
    TierAvailabilityGate,
    ApplicationStatusGate,
    MembershipQuestionnaireGate,
    ManualApprovalGate,
    PaymentReadyGate,
]
