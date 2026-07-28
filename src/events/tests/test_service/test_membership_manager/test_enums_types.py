"""Smoke tests for the membership-eligibility enums and types."""

from events.service.membership_manager.enums import MembershipNextStep, Reasons
from events.service.membership_manager.types import MembershipEligibility


def test_membership_next_step_values() -> None:
    assert MembershipNextStep.SUBMIT_QUESTIONNAIRE.value == "submit_questionnaire"
    assert MembershipNextStep.WAIT_FOR_APPROVAL.value == "wait_for_approval"
    assert MembershipNextStep.PROCEED_TO_PAYMENT.value == "proceed_to_payment"
    assert MembershipNextStep.ALREADY_MEMBER.value == "already_member"


def test_reasons_translatable() -> None:
    # Reasons are gettext_noop strings; raw access should yield the English message.
    assert "questionnaire" in str(Reasons.MEMBERSHIP_QUESTIONNAIRE_MISSING).lower()


def test_membership_eligibility_minimal_construction() -> None:
    from uuid import uuid4

    org_id = uuid4()
    e = MembershipEligibility(allowed=True, organization_id=org_id)
    assert e.allowed is True
    assert e.organization_id == org_id
    assert e.next_step is None
    assert e.tier_id is None
