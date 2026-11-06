"""The register `source` (and acceptance evidence) is tagged at each member-creation site (#1318)."""

from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationToken,
    SubscriptionPaymentMethod,
)
from events.service import member_register_service
from events.service.membership_manager.service import _complete_free_application
from events.service.organization_service import membership as membership_service
from events.service.organization_service.tokens import claim_invitation
from events.service.subscription.core import create_subscription
from events.service.subscription.stripe.sync import _ensure_active_member
from questionnaires.models import Questionnaire, QuestionnaireSubmission

pytestmark = pytest.mark.django_db

Source = MembershipRecord.Source


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    """A tier in the organization."""
    return MembershipTier.objects.create(organization=organization, name="Gold")


def _open_record(organization: Organization, user: RevelUser) -> MembershipRecord:
    return MembershipRecord.objects.get(organization=organization, user=user, valid_until__isnull=True)


def _plan(tier: MembershipTier, method: SubscriptionPaymentMethod) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Plan",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        payment_method=method,
    )


def test_add_member_source_staff(organization: Organization, nonmember_user: RevelUser, tier: MembershipTier) -> None:
    membership_service.add_member(organization, nonmember_user, tier)
    assert _open_record(organization, nonmember_user).source == Source.STAFF


def test_approve_application_source_application_with_evidence(
    organization: Organization,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
    tier: MembershipTier,
) -> None:
    questionnaire = Questionnaire.objects.create(name="Vetting Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    submitted_at = timezone.now()
    submission = QuestionnaireSubmission.objects.create(
        user=nonmember_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=submitted_at,
    )
    application = OrganizationMembershipRequest.objects.create(
        organization=organization, user=nonmember_user, tier=tier, questionnaire_submission=submission
    )
    membership_service.approve_membership_request(application, decided_by=organization_owner_user)
    rec = _open_record(organization, nonmember_user)
    assert rec.source == Source.APPLICATION
    assert rec.questionnaire_submission_id == submission.pk
    assert rec.accepted_at == submitted_at
    assert rec.questionnaire_name == "Vetting Q"


def test_free_application_source_application(
    organization: Organization, nonmember_user: RevelUser, tier: MembershipTier
) -> None:
    application = OrganizationMembershipRequest.objects.create(
        organization=organization, user=nonmember_user, tier=tier
    )
    _complete_free_application(application)
    assert _open_record(organization, nonmember_user).source == Source.APPLICATION


def test_token_claim_source_token(
    organization: Organization, nonmember_user: RevelUser, organization_token: OrganizationToken
) -> None:
    assert claim_invitation(nonmember_user, organization_token.id) is not None
    assert _open_record(organization, nonmember_user).source == Source.TOKEN


def test_offline_subscription_source_subscription(
    organization: Organization, nonmember_user: RevelUser, tier: MembershipTier
) -> None:
    create_subscription(_plan(tier, SubscriptionPaymentMethod.OFFLINE), nonmember_user)
    assert _open_record(organization, nonmember_user).source == Source.SUBSCRIPTION


def test_online_first_payment_source_subscription(
    organization: Organization, nonmember_user: RevelUser, tier: MembershipTier
) -> None:
    subscription = MembershipSubscription.objects.create(
        user=nonmember_user,
        plan=_plan(tier, SubscriptionPaymentMethod.ONLINE),
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
    )
    assert _ensure_active_member(subscription) == "created"
    assert _open_record(organization, nonmember_user).source == Source.SUBSCRIPTION


def test_free_application_passes_evidence_through(
    organization: Organization, nonmember_user: RevelUser, tier: MembershipTier
) -> None:
    questionnaire = Questionnaire.objects.create(name="Free Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    submission = QuestionnaireSubmission.objects.create(
        user=nonmember_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    application = OrganizationMembershipRequest.objects.create(
        organization=organization, user=nonmember_user, tier=tier, questionnaire_submission=submission
    )
    _complete_free_application(application)
    rec = _open_record(organization, nonmember_user)
    assert rec.source == Source.APPLICATION
    assert rec.questionnaire_submission_id == submission.pk
    assert rec.questionnaire_name == "Free Q"
    assert rec.accepted_at == submission.submitted_at


def test_ensure_active_member_reactivates_cancelled_with_subscription_source(
    organization: Organization, nonmember_user: RevelUser, tier: MembershipTier
) -> None:
    member = OrganizationMember.objects.create(
        organization=organization, user=nonmember_user, tier=tier, status=OrganizationMember.MembershipStatus.CANCELLED
    )
    # PENDING is ignored by sync_member_from_subscription, so the member stays CANCELLED on create.
    subscription = MembershipSubscription.objects.create(
        user=nonmember_user,
        plan=_plan(tier, SubscriptionPaymentMethod.ONLINE),
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
    )
    # Flip to ACTIVE without signals so _ensure_active_member still sees the CANCELLED row.
    MembershipSubscription.objects.filter(pk=subscription.pk).update(
        status=MembershipSubscription.SubscriptionStatus.ACTIVE
    )
    subscription.refresh_from_db()
    member.refresh_from_db()
    assert member.status == OrganizationMember.MembershipStatus.CANCELLED

    assert _ensure_active_member(subscription) == "existing"
    member.refresh_from_db()
    assert member.status == OrganizationMember.MembershipStatus.ACTIVE
    assert _open_record(organization, nonmember_user).source == Source.SUBSCRIPTION


def test_subscription_save_reactivating_cancelled_member_tags_subscription(
    organization: Organization, nonmember_user: RevelUser, tier: MembershipTier
) -> None:
    subscription = create_subscription(_plan(tier, SubscriptionPaymentMethod.OFFLINE), nonmember_user)
    MembershipSubscription.objects.filter(pk=subscription.pk).update(
        status=MembershipSubscription.SubscriptionStatus.CANCELLED
    )
    member = OrganizationMember.objects.get(organization=organization, user=nonmember_user)
    member.status = OrganizationMember.MembershipStatus.CANCELLED
    with member_register_service.suppress_mirror():
        member.save()
    MembershipRecord.objects.filter(user=nonmember_user).delete()

    subscription.refresh_from_db()
    subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
    subscription.save()

    assert _open_record(organization, nonmember_user).source == Source.SUBSCRIPTION
