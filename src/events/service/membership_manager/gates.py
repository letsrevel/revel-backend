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

from .enums import MembershipNextStep, Reasons
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
        reason: str,
        *,
        next_step: MembershipNextStep | None = None,
        **extra: t.Any,
    ) -> MembershipEligibility:
        """Build a blocking eligibility with this gate's (org, tier, plan) context preset.

        Extra kwargs are forwarded to MembershipEligibility (e.g. ``application_id=``).
        """
        return MembershipEligibility(
            allowed=False,
            organization_id=self.organization.pk,
            tier_id=self.tier.pk if self.tier else None,
            plan_id=self.plan.pk if self.plan else None,
            reason=reason,
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
        return self._block(_(Reasons.ORG_NOT_VISIBLE))


class BlacklistGate(BaseMembershipEligibilityGate):
    """Gate #3: Hard blacklist blocks; fuzzy match routes through whitelist UX."""

    def check(self) -> MembershipEligibility | None:
        """Block hard-blacklisted users; route fuzzy matches through whitelist verification."""
        if self.handler.is_hard_blacklisted:
            return self._block(_(Reasons.BLACKLISTED))

        if not self.handler.fuzzy_matched_blacklist_entries:
            return None
        if self.handler.is_whitelisted:
            return None

        whitelist_request = self.handler.whitelist_request
        if whitelist_request:
            if whitelist_request.status == WhitelistRequest.Status.PENDING:
                return self._block(_(Reasons.WHITELIST_PENDING))
            if whitelist_request.status == WhitelistRequest.Status.REJECTED:
                return self._block(_(Reasons.WHITELIST_REJECTED))

        # No request yet: surface verification requirement; FE routes through existing whitelist endpoint.
        return self._block(_(Reasons.REQUIRES_VERIFICATION), next_step=MembershipNextStep.REQUIRES_INVITATION)


class AlreadyMemberGate(BaseMembershipEligibilityGate):
    """Gate #4: ACTIVE membership at target tier short-circuits ALREADY_MEMBER.

    ACTIVE membership at a different tier falls through so subsequent gates can
    re-gate tier upgrades (S7). PAUSED memberships at the target tier hard-block:
    PAUSED is admin/Stripe-imposed and the user must contact the org to resume.

    When the user is applying via the free path (``plan is None``) but already has
    a non-terminal :class:`MembershipSubscription` in this org, block — they
    cannot self-promote a paid membership through the free flow.
    """

    def check(self) -> MembershipEligibility | None:
        """Allow with ALREADY_MEMBER at target tier; block PAUSED / free-promotion bypass."""
        # Free-apply bypass guard: a user with a non-terminal subscription must not
        # be able to clobber their paid membership via the free path.
        if self.plan is None and self.handler.has_non_terminal_subscription:
            return self._block(_(Reasons.DUPLICATE_ACTIVE_SUBSCRIPTION))

        if self.tier is None:
            return None
        membership = self.handler.user_memberships.get(self.tier.pk)
        if membership is None:
            return None
        if membership.status == OrganizationMember.MembershipStatus.ACTIVE:
            return self._allow(next_step=MembershipNextStep.ALREADY_MEMBER)
        if membership.status == OrganizationMember.MembershipStatus.PAUSED:
            # PAUSED is admin/Stripe-imposed; user has no in-app recourse so we
            # block with next_step=None.
            return self._block(_(Reasons.MEMBERSHIP_PAUSED))
        return None


class AcceptRequestsGate(BaseMembershipEligibilityGate):
    """Gate #5: Org must have accept_membership_requests=True."""

    def check(self) -> MembershipEligibility | None:
        """Block when the org is not accepting new membership requests."""
        if self.organization.accept_membership_requests:
            return None
        return self._block(_(Reasons.NOT_ACCEPTING_REQUESTS), next_step=MembershipNextStep.REQUIRES_INVITATION)


class TierAvailabilityGate(BaseMembershipEligibilityGate):
    """Gate #6: Target tier must belong to this org. When plan is provided, plan must be active and on tier."""

    def check(self) -> MembershipEligibility | None:
        """Block when the target tier or plan is not available for this organization."""
        if self.tier is not None and self.tier.organization_id != self.organization.pk:
            return self._block(_(Reasons.TIER_UNAVAILABLE))
        if self.plan is not None:
            if not self.plan.is_active:
                return self._block(_(Reasons.PLAN_UNAVAILABLE))
            if self.tier is not None and self.plan.tier_id != self.tier.pk:
                return self._block(_(Reasons.PLAN_UNAVAILABLE))
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
    user's ability to reapply. Orgs that want to permanently block a user must
    add them to the blacklist.
    """

    def check(self) -> MembershipEligibility | None:
        """Block when the current application is REJECTED; fall through otherwise."""
        app = self.handler.current_application
        if app is None:
            return None
        if app.status == OrganizationMembershipRequest.Status.REJECTED:
            return self._block(_(Reasons.APPLICATION_REJECTED), application_id=app.pk)
        return None


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
        if questionnaire_oq.members_exempt and self._user_is_active_member():
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

    def _user_is_active_member(self) -> bool:
        """True when the user has any ACTIVE membership in this org."""
        return any(
            m.status == OrganizationMember.MembershipStatus.ACTIVE for m in self.handler.user_memberships.values()
        )

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
        """Apply retake cooldown logic for a rejected evaluation."""
        if questionnaire.can_retake_after is not None and submission.submitted_at is not None:
            retry_on = submission.submitted_at + questionnaire.can_retake_after
            if retry_on > timezone.now():
                return self._block(
                    _(Reasons.MEMBERSHIP_QUESTIONNAIRE_RETAKE_COOLDOWN),
                    next_step=MembershipNextStep.WAIT_TO_RETAKE_QUESTIONNAIRE,
                    questionnaire_id=questionnaire.pk,
                    retry_on=retry_on,
                )
            return self._block_submit(questionnaire.pk)

        # No retake → terminal failure.
        return self._block(
            _(Reasons.MEMBERSHIP_QUESTIONNAIRE_FAILED),
            questionnaire_id=questionnaire.pk,
        )

    def _block_submit(self, questionnaire_id: uuid.UUID) -> MembershipEligibility:
        """Build the SUBMIT_QUESTIONNAIRE blocking verdict."""
        return self._block(
            _(Reasons.MEMBERSHIP_QUESTIONNAIRE_MISSING),
            next_step=MembershipNextStep.SUBMIT_QUESTIONNAIRE,
            questionnaire_id=questionnaire_id,
        )

    def _block_pending(self, questionnaire_id: uuid.UUID) -> MembershipEligibility:
        """Build the WAIT_FOR_QUESTIONNAIRE_EVALUATION blocking verdict."""
        return self._block(
            _(Reasons.MEMBERSHIP_QUESTIONNAIRE_PENDING),
            next_step=MembershipNextStep.WAIT_FOR_QUESTIONNAIRE_EVALUATION,
            questionnaire_id=questionnaire_id,
        )


class ManualApprovalGate(BaseMembershipEligibilityGate):
    """Gate #9: When manual-approval policy is on, only APPROVED applications pass through."""

    def check(self) -> MembershipEligibility | None:
        """Block unless the user already has an APPROVED application."""
        if not resolve_requires_membership_approval(self.organization, self.tier):
            return None
        app = self.handler.current_application
        if app and app.status == OrganizationMembershipRequest.Status.APPROVED:
            return None
        return self._block(
            _(Reasons.REQUIRES_APPROVAL),
            next_step=MembershipNextStep.WAIT_FOR_APPROVAL,
            application_id=app.pk if app else None,
        )


class PaymentReadyGate(BaseMembershipEligibilityGate):
    """Gate #10: Final pre-payment readiness check. No-op when plan is not provided.

    Phase 1 ships OFFLINE plans only: ``MembershipSubscriptionPlan`` has no
    ``payment_method`` field until Phase 2 (dev/subscriptions), so no plan can
    be paid for online and any plan-bearing application blocks here. Phase 2
    restores the full readiness check (ONLINE plan, Stripe-connected org, no
    duplicate non-terminal subscription, PROCEED_TO_PAYMENT next step).
    """

    def check(self) -> MembershipEligibility | None:
        """Block any plan-bearing application until online payments ship (Phase 2)."""
        if self.plan is None:
            return None
        return self._block(_(Reasons.PLAN_NOT_ONLINE))


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
