"""Enums for the membership eligibility system."""

from enum import StrEnum

from django.utils.translation import gettext_noop


class MembershipNextStep(StrEnum):
    """Possible next steps for a user to obtain membership at a target tier."""

    SUBMIT_QUESTIONNAIRE = "submit_questionnaire"
    WAIT_FOR_QUESTIONNAIRE_EVALUATION = "wait_for_questionnaire_evaluation"
    WAIT_TO_RETAKE_QUESTIONNAIRE = "wait_to_retake_questionnaire"
    WAIT_FOR_APPROVAL = "wait_for_approval"
    WAIT_FOR_WHITELIST_APPROVAL = "wait_for_whitelist_approval"
    REQUIRES_INVITATION = "requires_invitation"
    PROCEED_TO_PAYMENT = "proceed_to_payment"
    ALREADY_MEMBER = "already_member"
    REAPPLY = "reapply"


class ReasonCode(StrEnum):
    """Stable machine-readable identifiers for membership-eligibility reasons.

    Mirrors :class:`Reasons` member-for-member but with snake_case values that
    are stable under prose changes and translations. The frontend (and the
    ``advance_application`` state machine) switch on this rather than matching
    the human-readable, localized ``reason`` string.
    """

    ORG_NOT_VISIBLE = "org_not_visible"
    BLACKLISTED = "blacklisted"
    REQUIRES_VERIFICATION = "requires_verification"
    WHITELIST_PENDING = "whitelist_pending"
    WHITELIST_REJECTED = "whitelist_rejected"
    ALREADY_ACTIVE_MEMBER = "already_active_member"
    NOT_ACCEPTING_REQUESTS = "not_accepting_requests"
    TIER_UNAVAILABLE = "tier_unavailable"
    PLAN_UNAVAILABLE = "plan_unavailable"
    APPLICATION_REJECTED = "application_rejected"
    MEMBERSHIP_QUESTIONNAIRE_MISSING = "membership_questionnaire_missing"
    MEMBERSHIP_QUESTIONNAIRE_PENDING = "membership_questionnaire_pending"
    MEMBERSHIP_QUESTIONNAIRE_FAILED = "membership_questionnaire_failed"
    MEMBERSHIP_QUESTIONNAIRE_RETAKE_COOLDOWN = "membership_questionnaire_retake_cooldown"
    REQUIRES_APPROVAL = "requires_approval"
    PLAN_NOT_ONLINE = "plan_not_online"
    ORG_NOT_STRIPE_CONNECTED = "org_not_stripe_connected"
    DUPLICATE_ACTIVE_SUBSCRIPTION = "duplicate_active_subscription"
    MEMBERSHIP_PAUSED = "membership_paused"


class Reasons(StrEnum):
    """Reasons for membership-eligibility decisions.

    Strings are marked with gettext_noop for translation extraction.
    The actual translation happens at gate-call time via _(Reasons.XXX).
    """

    ORG_NOT_VISIBLE = gettext_noop("This organization is not available.")
    BLACKLISTED = gettext_noop("You are not allowed to join this organization.")
    REQUIRES_VERIFICATION = gettext_noop("Additional verification required.")
    WHITELIST_PENDING = gettext_noop("Your verification request is pending approval.")
    WHITELIST_REJECTED = gettext_noop("Your verification request was rejected.")
    ALREADY_ACTIVE_MEMBER = gettext_noop("You are already a member at this tier.")
    NOT_ACCEPTING_REQUESTS = gettext_noop("This organization is not accepting new members.")
    TIER_UNAVAILABLE = gettext_noop("The requested tier is not available.")
    PLAN_UNAVAILABLE = gettext_noop("The requested plan is not available.")
    APPLICATION_REJECTED = gettext_noop("Your application was rejected.")
    MEMBERSHIP_QUESTIONNAIRE_MISSING = gettext_noop("Membership questionnaire has not been filled.")
    MEMBERSHIP_QUESTIONNAIRE_PENDING = gettext_noop("Waiting for questionnaire evaluation.")
    MEMBERSHIP_QUESTIONNAIRE_FAILED = gettext_noop("Membership questionnaire evaluation was insufficient.")
    MEMBERSHIP_QUESTIONNAIRE_RETAKE_COOLDOWN = gettext_noop(
        "Membership questionnaire evaluation was insufficient. You can try again later."
    )
    REQUIRES_APPROVAL = gettext_noop("Your application is awaiting staff approval.")
    PLAN_NOT_ONLINE = gettext_noop("This plan is not configured for online checkout.")
    ORG_NOT_STRIPE_CONNECTED = gettext_noop("This organization cannot accept online payments yet.")
    DUPLICATE_ACTIVE_SUBSCRIPTION = gettext_noop("You already have an active subscription in this organization.")
    MEMBERSHIP_PAUSED = gettext_noop("Your membership at this tier is paused. Contact the organization to resume.")

    @property
    def code(self) -> ReasonCode:
        """Return the stable machine-readable code for this reason.

        Pairs with the translated ``reason`` string so API consumers (and the
        application state machine) can switch on a stable identifier instead
        of matching localized prose.
        """
        return ReasonCode[self.name]
