"""Staff fan-outs must honour the recipient's per-type notification preference.

Regression for the staff fan-outs that looped over ``get_staff_for_notification``
and sent unconditionally: a staff member who switched the notification type off
still received every one of them. The ticket/payment/series-pass fan-outs always
gated on the preference; these did not.
"""

import typing as t
from unittest.mock import MagicMock, patch

import pytest

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitationRequest,
    Organization,
    OrganizationContactMessage,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
)
from notifications.enums import NotificationType
from questionnaires.models import Questionnaire, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


def _set_enabled(user: RevelUser, notification_type: NotificationType, enabled: bool) -> None:
    """Turn one notification type on or off for a user."""
    prefs = user.notification_preferences
    prefs.notification_type_settings[notification_type] = {"enabled": enabled}
    prefs.save(update_fields=["notification_type_settings"])


def _was_notified(send_mock: MagicMock, notification_type: NotificationType, user: RevelUser) -> bool:
    """Whether ``user`` received ``notification_type`` on the patched signal."""
    return any(
        call.kwargs.get("notification_type") == notification_type and call.kwargs.get("user") == user
        for call in send_mock.call_args_list
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_membership_request_respects_staff_preference(
    organization: Organization,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
    enabled: bool,
) -> None:
    """MEMBERSHIP_REQUEST_CREATED reaches the owner only while they have it enabled."""
    _set_enabled(organization.owner, NotificationType.MEMBERSHIP_REQUEST_CREATED, enabled)

    with patch("notifications.signals.notification_requested.send") as send_mock:
        with django_capture_on_commit_callbacks(execute=True):
            OrganizationMembershipRequest.objects.create(organization=organization, user=member_user)

    assert _was_notified(send_mock, NotificationType.MEMBERSHIP_REQUEST_CREATED, organization.owner) is enabled


@pytest.mark.parametrize("enabled", [True, False])
def test_invitation_request_respects_staff_preference(
    organization: Organization,
    public_event: Event,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
    enabled: bool,
) -> None:
    """INVITATION_REQUEST_CREATED reaches the owner only while they have it enabled."""
    _set_enabled(organization.owner, NotificationType.INVITATION_REQUEST_CREATED, enabled)

    with patch("notifications.signals.notification_requested.send") as send_mock:
        with django_capture_on_commit_callbacks(execute=True):
            EventInvitationRequest.objects.create(event=public_event, user=member_user)

    assert _was_notified(send_mock, NotificationType.INVITATION_REQUEST_CREATED, organization.owner) is enabled


@pytest.mark.parametrize("enabled", [True, False])
def test_questionnaire_submission_respects_staff_preference(
    organization: Organization,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
    enabled: bool,
) -> None:
    """QUESTIONNAIRE_SUBMITTED reaches the owner only while they have it enabled."""
    _set_enabled(organization.owner, NotificationType.QUESTIONNAIRE_SUBMITTED, enabled)
    questionnaire = Questionnaire.objects.create(name="Gate Questionnaire", min_score=0)
    OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    with patch("notifications.signals.notification_requested.send") as send_mock:
        with django_capture_on_commit_callbacks(execute=True):
            QuestionnaireSubmission.objects.create(questionnaire=questionnaire, user=member_user)

    assert _was_notified(send_mock, NotificationType.QUESTIONNAIRE_SUBMITTED, organization.owner) is enabled


@pytest.mark.parametrize("enabled", [True, False])
def test_contact_message_respects_staff_preference(
    organization: Organization,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
    enabled: bool,
) -> None:
    """ORG_CONTACT_MESSAGE_RECEIVED reaches the owner only while they have it enabled."""
    _set_enabled(organization.owner, NotificationType.ORG_CONTACT_MESSAGE_RECEIVED, enabled)

    with (
        patch("events.tasks.send_organization_contact_message_email.delay"),
        patch("notifications.signals.notification_requested.send") as send_mock,
    ):
        with django_capture_on_commit_callbacks(execute=True):
            OrganizationContactMessage.objects.create(
                organization=organization,
                sender=member_user,
                sender_email_snapshot=member_user.email,
                subject="Gate check",
                message="Hello there",
            )

    assert _was_notified(send_mock, NotificationType.ORG_CONTACT_MESSAGE_RECEIVED, organization.owner) is enabled
