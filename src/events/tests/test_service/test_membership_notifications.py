"""Verify membership-application notifications still fire end-to-end.

Uses ``transaction=True`` module-wide because every dispatch here happens via
``transaction.on_commit`` (``notifications.signals.membership`` for the created
notification, ``events.service.organization_service.membership`` for the approved
and rejected ones); in default pytest-django mode the wrapping transaction is
rolled back and those callbacks never fire.
"""

import typing as t
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone, translation
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
)
from events.service import organization_service
from events.service.subscription import notifications as subscription_notifications
from events.tasks import send_subscription_renewal_reminders
from events.utils.subscription_periods import REMINDER_DAYS
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.registry import get_template
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db(transaction=True)


def _client(user: RevelUser) -> Client:
    token = RefreshToken.for_user(user)
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token.access_token}"  # type: ignore[attr-defined]
    return c


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


def test_apply_fires_request_created_notification_for_staff(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.default_requires_membership_approval = True  # force PENDING (so we keep the row)
    organization.save(
        update_fields=[
            "visibility",
            "accept_membership_requests",
            "default_requires_membership_approval",
        ]
    )

    with patch("notifications.signals.notification_requested.send") as mock_send:
        client = _client(nonmember_user)
        url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
        response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")
        assert response.status_code == 201

    # The first POST creates a PENDING OMR → post_save signal fires.
    types_sent = [call.kwargs.get("notification_type") for call in mock_send.call_args_list]
    assert NotificationType.MEMBERSHIP_REQUEST_CREATED in types_sent


def test_approve_fires_approved_notification(
    organization_owner_user: RevelUser,
    organization: Organization,
    nonmember_user: RevelUser,
    tier: MembershipTier,
) -> None:
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    with patch("events.service.organization_service.membership.notification_requested.send") as mock_send:
        organization_service.approve_membership_request(app, organization_owner_user)
    types_sent = [call.kwargs.get("notification_type") for call in mock_send.call_args_list]
    assert NotificationType.MEMBERSHIP_REQUEST_APPROVED in types_sent


def test_reject_fires_rejected_notification(
    organization_owner_user: RevelUser,
    organization: Organization,
    nonmember_user: RevelUser,
    tier: MembershipTier,
) -> None:
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    with patch("events.service.organization_service.membership.notification_requested.send") as mock_send:
        organization_service.reject_membership_request(app, organization_owner_user)
    types_sent = [call.kwargs.get("notification_type") for call in mock_send.call_args_list]
    assert NotificationType.MEMBERSHIP_REQUEST_REJECTED in types_sent


# ---------------------------------------------------------------------------
# B5: payment requested, request-created tier/plan, links
# ---------------------------------------------------------------------------


def _bodies(notification: Notification) -> dict[str, str]:
    """Render every channel body of a notification (en)."""
    template = get_template(notification.notification_type)
    with translation.override("en"):
        return {
            "email_html": template.get_email_html_body(notification) or "",
            "email_txt": template.get_email_text_body(notification),
            "in_app": template.get_in_app_body(notification),
            "telegram": template.get_telegram_body(notification),
        }


@pytest.fixture
def offline_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Yearly",
        price=Decimal("50.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.YEAR,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def offline_sub(
    offline_plan: MembershipSubscriptionPlan, organization: Organization, nonmember_user: RevelUser
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=nonmember_user,
        plan=offline_plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
    )


def _payment_requested(sub: MembershipSubscription, *, application_accepted: bool = False) -> Notification:
    with patch("notifications.tasks.dispatch_notification.delay"):
        subscription_notifications.dispatch_membership_payment_requested(sub, application_accepted=application_accepted)
    return Notification.objects.get(user=sub.user, notification_type=NotificationType.MEMBERSHIP_PAYMENT_REQUESTED)


def test_payment_requested_renders_instructions_all_channels(
    offline_sub: MembershipSubscription, organization: Organization
) -> None:
    offline_sub.plan.payment_instructions = "Wire to IBAN AT00 1234"
    offline_sub.plan.save(update_fields=["payment_instructions"])
    notif = _payment_requested(offline_sub)
    assert notif.context["tier_name"] == "Standard"
    assert notif.context["payment_instructions"] == "Wire to IBAN AT00 1234"
    template = get_template(notif.notification_type)
    with translation.override("en"):
        assert template.get_email_subject(notif) == f"Pay to activate your Standard membership at {organization.name}"
    for channel, body in _bodies(notif).items():
        assert "Standard" in body, channel
        assert "Yearly" in body, channel
        assert "50.00 EUR" in body, channel
        assert "30 days" in body, channel
        assert "Your memberships" in body, channel
        assert "Wire to IBAN AT00 1234" in body, channel
        assert "/account/memberships" in body, channel
        assert "accepted your application" not in body, channel


def test_payment_requested_contact_fallback(offline_sub: MembershipSubscription, organization: Organization) -> None:
    notif = _payment_requested(offline_sub)
    assert notif.context["payment_instructions"] == ""
    for channel, body in _bodies(notif).items():
        assert "will tell you how to pay" in body, channel
        assert f"/org/{organization.slug}" in body, channel


def test_request_created_has_tier_and_plan(
    nonmember_user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
    offline_plan: MembershipSubscriptionPlan,
) -> None:
    with patch("notifications.signals.membership.notify_org_staff") as mock_notify:
        OrganizationMembershipRequest.objects.create(
            organization=organization, user=nonmember_user, tier=tier, plan=offline_plan
        )
    ctx = mock_notify.call_args.kwargs["context"]
    assert ctx["tier_name"] == "Standard"
    assert ctx["plan_name"] == "Yearly"
    assert ctx["plan_payment_method"] == "offline"
    notif = Notification.objects.create(
        user=nonmember_user, notification_type=NotificationType.MEMBERSHIP_REQUEST_CREATED, context=ctx
    )
    for channel, body in _bodies(notif).items():
        assert "Standard" in body, channel
        assert "Yearly" in body, channel


def test_approved_templates_render_link(nonmember_user: RevelUser, organization: Organization) -> None:
    url = f"https://app.example.com/org/{organization.slug}"
    notif = Notification.objects.create(
        user=nonmember_user,
        notification_type=NotificationType.MEMBERSHIP_REQUEST_APPROVED,
        context={"organization_id": str(organization.id), "organization_name": organization.name, "frontend_url": url},
    )
    for channel, body in _bodies(notif).items():
        assert url in body, channel


def test_membership_questionnaire_result_links_membership_page(
    organization: Organization, nonmember_user: RevelUser, questionnaire: Questionnaire, public_event: t.Any
) -> None:
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    oq.events.add(public_event)  # must be ignored for membership questionnaires
    submission = QuestionnaireSubmission.objects.create(
        user=nonmember_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )
    with patch("notifications.signals.questionnaire.notification_requested.send") as mock_send:
        QuestionnaireEvaluation.objects.create(
            submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
        )
    ctx = mock_send.call_args.kwargs["context"]
    assert ctx["membership_url"].endswith(f"/org/{organization.slug}/membership")
    assert "event_url" not in ctx
    assert "requires_ticket" not in ctx
    notif = Notification.objects.create(
        user=nonmember_user, notification_type=NotificationType.QUESTIONNAIRE_EVALUATION_RESULT, context=ctx
    )
    for channel, body in _bodies(notif).items():
        assert ctx["membership_url"] in body, channel
        assert f"Continue joining {organization.name}" in body, channel
        assert "RSVP" not in body, channel
        assert "ticket" not in body, channel
    assert (
        f'<a href="{ctx["membership_url"]}" class="button">Continue joining {organization.name}</a>'
        in (_bodies(notif)["email_html"])
    )


def test_offline_reminder_includes_instructions(offline_sub: MembershipSubscription) -> None:
    offline_sub.plan.payment_instructions = "Pay cash at the bar"
    offline_sub.plan.save(update_fields=["payment_instructions"])
    offline_sub.status = MembershipSubscription.SubscriptionStatus.ACTIVE
    offline_sub.current_period_start = timezone.now()
    offline_sub.current_period_end = timezone.make_aware(
        datetime.combine(timezone.localdate() + timedelta(days=REMINDER_DAYS), time(12, 0))
    )
    offline_sub.save()
    with patch("notifications.tasks.dispatch_notification.delay"):
        send_subscription_renewal_reminders()
    notif = Notification.objects.get(
        user=offline_sub.user, notification_type=NotificationType.SUBSCRIPTION_RENEWAL_REMINDER
    )
    for channel, body in _bodies(notif).items():
        if channel != "in_app":
            assert "To renew, pay" in body, channel
            assert "don't want to renew" not in body, channel
        assert "Pay cash at the bar" in body, channel


def test_payment_requested_cta_stays_on_platform_origin(
    offline_sub: MembershipSubscription, tier: MembershipTier, organization: Organization
) -> None:
    """/account/memberships is a platform route: it never follows the org's tenant domain."""
    from common.models import SiteSettings

    with patch.object(subscription_notifications, "frontend_base_url_for", return_value="https://tenant.example"):
        notif = _payment_requested(offline_sub)
    assert notif.context["memberships_url"] == f"{SiteSettings.get_solo().frontend_base_url}/account/memberships"
    assert "tenant.example" not in notif.context["memberships_url"]
    assert notif.context["application_accepted"] is False


def test_payment_requested_leads_with_accepted_application(
    offline_sub: MembershipSubscription, organization: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        plan=offline_sub.plan,
        subscription=offline_sub,
        status=OrganizationMembershipRequest.Status.APPROVED,
    )
    notif = _payment_requested(offline_sub, application_accepted=True)
    assert notif.context["application_accepted"] is True
    for channel, body in _bodies(notif).items():
        assert f"{organization.name}" in body.split("accepted your application")[0], channel
        assert "accepted your application" in body, channel


def test_lapsed_notification_links_to_membership_page(
    offline_sub: MembershipSubscription, organization: Organization
) -> None:
    with patch("notifications.tasks.dispatch_notification.delay"):
        subscription_notifications.dispatch_unpaid_offline_lapsed(offline_sub)
    notif = Notification.objects.get(user=offline_sub.user, notification_type=NotificationType.SUBSCRIPTION_EXPIRED)
    assert notif.context["membership_url"].endswith(f"/org/{organization.slug}/membership")
    for channel, body in _bodies(notif).items():
        assert f"/org/{organization.slug}/membership" in body, channel
        assert "join or apply again" in body.lower(), channel
