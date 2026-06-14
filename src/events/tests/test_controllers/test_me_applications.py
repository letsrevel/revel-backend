"""End-to-end tests for the membership application controller."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
)
from notifications.enums import NotificationType
from notifications.signals import notification_requested
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture(autouse=True)
def open_org(organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save(update_fields=["visibility", "accept_membership_requests"])


def _client(user: RevelUser) -> Client:
    token = RefreshToken.for_user(user)
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token.access_token}"  # type: ignore[attr-defined]
    return c


def test_unauthenticated_blocked(organization: Organization) -> None:
    url = reverse("api:get_join_eligibility", kwargs={"slug": organization.slug})
    response = Client().get(url)
    assert response.status_code == 401


def test_get_join_eligibility_no_gates(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    client = _client(nonmember_user)
    url = reverse("api:get_join_eligibility", kwargs={"slug": organization.slug})
    response = client.get(url, {"tier_id": str(tier.id)})
    assert response.status_code == 200
    data = response.json()
    assert data["allowed"] is True
    assert data["tier_id"] == str(tier.id)


def test_apply_free_no_gates_creates_member(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["application"]["status"] == "completed"
    assert OrganizationMember.objects.filter(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    ).exists()


def test_apply_idempotent_when_pending(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    q = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    organization.default_membership_questionnaire = oq
    organization.save(update_fields=["default_membership_questionnaire"])

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response_1 = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")
    response_2 = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")
    assert response_1.status_code == 201
    assert response_2.status_code == 201
    # Same application ID returned both times.
    assert response_1.json()["application"]["id"] == response_2.json()["application"]["id"]
    assert (
        OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user, tier=tier).count()
        == 1
    )


def test_apply_questionnaire_gate_returns_submit_step(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    q = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    organization.default_membership_questionnaire = oq
    organization.save(update_fields=["default_membership_questionnaire"])

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["application"]["status"] == "pending"
    assert body["eligibility"]["next_step"] == "submit_questionnaire"
    assert body["eligibility"]["questionnaire_id"] == str(q.id)


def test_apply_after_questionnaire_pass_advances_to_completed(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    q = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    organization.default_membership_questionnaire = oq
    organization.save(update_fields=["default_membership_questionnaire"])

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")
    assert response.status_code == 201

    submission = QuestionnaireSubmission.objects.create(
        user=nonmember_user,
        questionnaire=q,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
    )

    # Second POST advances state.
    response_2 = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")
    assert response_2.status_code == 201, response_2.content
    assert response_2.json()["application"]["status"] == "completed"


def test_apply_rejects_plan_id_in_phase_1(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier, name="M", price=Decimal("5.00"), currency="EUR", period_unit="month"
    )
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(
        url,
        data={"tier_id": str(tier.id), "plan_id": str(plan.id)},
        content_type="application/json",
    )
    assert response.status_code == 400


def test_cancel_application(nonmember_user: RevelUser, organization: Organization, tier: MembershipTier) -> None:
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    client = _client(nonmember_user)
    url = reverse("api:cancel_membership_application", kwargs={"application_id": app.id})
    response = client.post(url, content_type="application/json")
    assert response.status_code == 200
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.CANCELLED


def test_list_applications_only_returns_own(
    nonmember_user: RevelUser,
    member_user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
) -> None:
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=member_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    client = _client(nonmember_user)
    url = reverse("api:list_membership_applications")
    response = client.get(url)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1


# -- S1: Pre-gate creation hardening ----------------------------------------------------------


def test_apply_blacklisted_user_does_not_create_row_or_notification(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """Hard-blacklisted users must be refused BEFORE any OMR row is created.

    Otherwise the post_save signal queues MEMBERSHIP_REQUEST_CREATED notifications
    to every staff member with manage_members — a spam vector.
    """
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})

    received: list[dict[str, object]] = []

    def _collect(sender: object, **kwargs: object) -> None:
        received.append(kwargs)

    notification_requested.connect(_collect)
    try:
        with patch("events.service.blacklist_service.check_user_hard_blacklisted", return_value=True):
            response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")
    finally:
        notification_requested.disconnect(_collect)

    assert response.status_code == 403, response.content
    assert not OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user).exists()
    assert not any(kw.get("notification_type") == NotificationType.MEMBERSHIP_REQUEST_CREATED for kw in received)


def test_apply_unknown_or_invisible_org_returns_404(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """A PRIVATE org the user can't see must look like 404 — no enumeration, no OMR row."""
    organization.visibility = Organization.Visibility.PRIVATE
    organization.save(update_fields=["visibility"])

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")

    assert response.status_code == 404, response.content
    assert not OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user).exists()


def test_apply_org_not_accepting_requests_returns_403_no_row(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """When the org is closed to new members, /apply must refuse without creating a row."""
    organization.accept_membership_requests = False
    organization.save(update_fields=["accept_membership_requests"])

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")

    assert response.status_code == 403, response.content
    assert not OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user).exists()


# -- S2: PAUSED self-promotion + subscription bypass ------------------------------------------


def test_apply_paused_membership_blocks_self_promotion(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """A PAUSED member must not be able to self-flip back to ACTIVE via free /apply."""
    member = OrganizationMember.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")

    # next_step=None → pre-gate hard-block → 403.
    assert response.status_code == 403, response.content
    member.refresh_from_db()
    assert member.status == OrganizationMember.MembershipStatus.PAUSED


def test_apply_user_with_active_subscription_cannot_self_promote_via_free_apply(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """A user with a non-terminal subscription must not bypass payment via free /apply."""
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier, name="M", price=Decimal("5.00"), currency="EUR", period_unit="month"
    )
    MembershipSubscription.objects.create(
        user=nonmember_user,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
    )
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")

    # AlreadyMemberGate blocks with DUPLICATE_ACTIVE_SUBSCRIPTION (next_step=None) → 403.
    assert response.status_code == 403, response.content
    # No member row should be created (or upgraded) by this attempt.
    assert not OrganizationMember.objects.filter(
        organization=organization,
        user=nonmember_user,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    ).exists()


# -- T1: cancel endpoint coverage gaps --------------------------------------------------------


def test_cancel_other_users_application_returns_404(
    nonmember_user: RevelUser,
    member_user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
) -> None:
    """User A must not be able to cancel User B's application.

    The controller scopes by ``user=self.user()`` — a cross-user attempt
    must surface as a 404 (no enumeration of foreign application IDs).
    """
    # Arrange: application owned by member_user.
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=member_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )

    # Act: nonmember_user tries to cancel it.
    client = _client(nonmember_user)
    url = reverse("api:cancel_membership_application", kwargs={"application_id": app.id})
    response = client.post(url, content_type="application/json")

    # Assert: 404, row untouched.
    assert response.status_code == 404, response.content
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.PENDING


def test_cancel_already_cancelled_is_noop(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """Re-cancelling an already-CANCELLED application is idempotent (200, unchanged)."""
    # Arrange
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.CANCELLED,
    )
    updated_at_before = app.updated_at

    # Act
    client = _client(nonmember_user)
    url = reverse("api:cancel_membership_application", kwargs={"application_id": app.id})
    response = client.post(url, content_type="application/json")

    # Assert
    assert response.status_code == 200, response.content
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.CANCELLED
    # No-op: updated_at should not change for terminal-status rows.
    assert app.updated_at == updated_at_before


def test_cancel_completed_application_is_noop(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """Cancelling a COMPLETED application is a no-op — status stays COMPLETED."""
    # Arrange
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.COMPLETED,
    )

    # Act
    client = _client(nonmember_user)
    url = reverse("api:cancel_membership_application", kwargs={"application_id": app.id})
    response = client.post(url, content_type="application/json")

    # Assert
    assert response.status_code == 200, response.content
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.COMPLETED


def test_cancel_rejected_application_is_noop(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """Cancelling a REJECTED application is a no-op — REJECTED is terminal."""
    # Arrange
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.REJECTED,
    )

    # Act
    client = _client(nonmember_user)
    url = reverse("api:cancel_membership_application", kwargs={"application_id": app.id})
    response = client.post(url, content_type="application/json")

    # Assert
    assert response.status_code == 200, response.content
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.REJECTED


def test_cancel_approved_application_transitions_to_cancelled(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """An APPROVED-but-not-yet-completed application can still be cancelled by the user."""
    # Arrange
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.APPROVED,
    )

    # Act
    client = _client(nonmember_user)
    url = reverse("api:cancel_membership_application", kwargs={"application_id": app.id})
    response = client.post(url, content_type="application/json")

    # Assert
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["status"] == OrganizationMembershipRequest.Status.CANCELLED
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.CANCELLED


def test_cancel_anonymous_returns_401(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """Unauthenticated cancel attempts must be rejected with 401."""
    # Arrange
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )

    # Act
    url = reverse("api:cancel_membership_application", kwargs={"application_id": app.id})
    response = Client().post(url, content_type="application/json")

    # Assert
    assert response.status_code == 401
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.PENDING


# -- T2: get_application endpoint coverage ----------------------------------------------------


def test_get_application_returns_app_and_eligibility(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """GET /me/applications/{id} returns the application plus a fresh eligibility verdict."""
    # Arrange
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )

    # Act
    client = _client(nonmember_user)
    url = reverse("api:get_membership_application", kwargs={"application_id": app.id})
    response = client.get(url)

    # Assert
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["application"]["id"] == str(app.id)
    assert "eligibility" in body
    assert body["eligibility"]["organization_id"] == str(organization.id)
    assert body["eligibility"]["application_id"] == str(app.id)


def test_get_application_advances_state_on_read(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """Read-side state-advance: GET re-runs the gate chain and may flip PENDING -> COMPLETED."""
    # Arrange: questionnaire-gated org with a PENDING application and no submission yet.
    q = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    organization.default_membership_questionnaire = oq
    organization.save(update_fields=["default_membership_questionnaire"])

    client = _client(nonmember_user)
    apply_url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    apply_resp = client.post(apply_url, data={"tier_id": str(tier.id)}, content_type="application/json")
    assert apply_resp.status_code == 201, apply_resp.content
    app_id = apply_resp.json()["application"]["id"]

    # Sanity: still PENDING, gate says submit_questionnaire.
    app = OrganizationMembershipRequest.objects.get(pk=app_id)
    assert app.status == OrganizationMembershipRequest.Status.PENDING

    # The user now submits and the evaluation is APPROVED.
    submission = QuestionnaireSubmission.objects.create(
        user=nonmember_user,
        questionnaire=q,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
    )

    # Act: a plain GET should advance the application to COMPLETED.
    get_url = reverse("api:get_membership_application", kwargs={"application_id": app_id})
    response = client.get(get_url)

    # Assert
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["application"]["status"] == OrganizationMembershipRequest.Status.COMPLETED
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.COMPLETED


def test_get_application_404_for_other_users_app(
    nonmember_user: RevelUser,
    member_user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
) -> None:
    """GET on an application owned by another user must return 404 (no enumeration)."""
    # Arrange
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=member_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )

    # Act
    client = _client(nonmember_user)
    url = reverse("api:get_membership_application", kwargs={"application_id": app.id})
    response = client.get(url)

    # Assert
    assert response.status_code == 404


def test_get_application_404_for_unknown_id(nonmember_user: RevelUser) -> None:
    """GET on a random UUID returns 404."""
    # Arrange
    from uuid import uuid4

    client = _client(nonmember_user)
    url = reverse("api:get_membership_application", kwargs={"application_id": uuid4()})

    # Act
    response = client.get(url)

    # Assert
    assert response.status_code == 404


def test_get_application_includes_application_id_in_eligibility(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """The eligibility block must always carry the application_id so the FE can re-poll."""
    # Arrange
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )

    # Act
    client = _client(nonmember_user)
    url = reverse("api:get_membership_application", kwargs={"application_id": app.id})
    response = client.get(url)

    # Assert
    assert response.status_code == 200, response.content
    assert response.json()["eligibility"]["application_id"] == str(app.id)


def test_get_application_anonymous_returns_401(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """Unauthenticated GET on an application returns 401."""
    # Arrange
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )

    # Act
    url = reverse("api:get_membership_application", kwargs={"application_id": app.id})
    response = Client().get(url)

    # Assert
    assert response.status_code == 401


# -- T3: get_join_eligibility error paths -----------------------------------------------------


def test_join_eligibility_unknown_org_slug_returns_404(nonmember_user: RevelUser) -> None:
    """An unknown org slug must surface as 404 from /join-eligibility."""
    # Arrange
    client = _client(nonmember_user)
    url = reverse("api:get_join_eligibility", kwargs={"slug": "this-org-does-not-exist"})

    # Act
    response = client.get(url)

    # Assert
    assert response.status_code == 404


def test_join_eligibility_tier_from_other_org_returns_404(
    nonmember_user: RevelUser,
    organization: Organization,
    organization_owner_user: RevelUser,
) -> None:
    """Passing a tier_id belonging to a different org must 404 (cross-org leak guard)."""
    # Arrange: an unrelated org with a tier the caller should not be able to reference.
    other_org = Organization.objects.create(
        name="Other Org",
        slug="other-org-eligibility",
        owner=organization_owner_user,
        visibility=Organization.Visibility.PUBLIC,
        accept_membership_requests=True,
    )
    other_tier = MembershipTier.objects.create(organization=other_org, name="Other Standard")

    client = _client(nonmember_user)
    url = reverse("api:get_join_eligibility", kwargs={"slug": organization.slug})

    # Act
    response = client.get(url, {"tier_id": str(other_tier.id)})

    # Assert
    assert response.status_code == 404


def test_join_eligibility_plan_from_other_org_returns_404(
    nonmember_user: RevelUser,
    organization: Organization,
    organization_owner_user: RevelUser,
) -> None:
    """Passing a plan_id belonging to a different org must 404."""
    # Arrange: unrelated org + tier + plan.
    other_org = Organization.objects.create(
        name="Other Org Plan",
        slug="other-org-eligibility-plan",
        owner=organization_owner_user,
        visibility=Organization.Visibility.PUBLIC,
        accept_membership_requests=True,
    )
    other_tier = MembershipTier.objects.create(organization=other_org, name="Other Standard")
    other_plan = MembershipSubscriptionPlan.objects.create(
        tier=other_tier,
        name="M",
        price=Decimal("5.00"),
        currency="EUR",
        period_unit="month",
    )

    client = _client(nonmember_user)
    url = reverse("api:get_join_eligibility", kwargs={"slug": organization.slug})

    # Act
    response = client.get(url, {"plan_id": str(other_plan.id)})

    # Assert
    assert response.status_code == 404


def test_join_eligibility_anonymous_returns_401(organization: Organization, tier: MembershipTier) -> None:
    """Anonymous callers cannot probe join eligibility."""
    # Act
    url = reverse("api:get_join_eligibility", kwargs={"slug": organization.slug})
    response = Client().get(url, {"tier_id": str(tier.id)})

    # Assert
    assert response.status_code == 401
