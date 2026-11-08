"""Tests for the STAFF_MEMBER_RECORD_AUTO_LINKED notification (#1319)."""

import pytest

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Organization, OrganizationStaff
from notifications.context_schemas import NOTIFICATION_CONTEXT_SCHEMAS
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.eligibility import NOTIFICATION_REQUIRED_PERMISSIONS, get_staff_for_notification
from notifications.service.preferences import get_guest_notification_type_settings
from notifications.service.templates.membership_templates import StaffMemberRecordAutoLinkedTemplate
from notifications.service.templates.registry import get_template

pytestmark = pytest.mark.django_db

NTYPE = NotificationType.STAFF_MEMBER_RECORD_AUTO_LINKED
FRONTEND_URL = "https://example.test/org/acme/admin/members?tab=register"


@pytest.fixture
def linked_notification(regular_user: RevelUser) -> Notification:
    """A notification populated with the context the caller sends."""
    return Notification.objects.create(
        user=regular_user,
        notification_type=NTYPE,
        context={
            "organization_id": "00000000-0000-0000-0000-000000000001",
            "organization_name": "Acme Org",
            "record_id": "00000000-0000-0000-0000-000000000002",
            "record_full_name": "Jane Roe",
            "member_display_name": "Janie",
            "member_email": "jane@example.com",
            "frontend_url": FRONTEND_URL,
        },
    )


def _assert_rendered(body: str) -> None:
    assert "Janie" in body
    assert "Jane Roe" in body
    assert "Acme Org" in body
    assert "verified email matched" in body
    assert FRONTEND_URL in body


def test_registered_and_schema_declared() -> None:
    assert isinstance(get_template(NTYPE), StaffMemberRecordAutoLinkedTemplate)
    assert NTYPE in NOTIFICATION_CONTEXT_SCHEMAS


def test_title_and_subject(linked_notification: Notification) -> None:
    template = StaffMemberRecordAutoLinkedTemplate()
    title = template.get_in_app_title(linked_notification)
    assert "Janie" in title and "Acme Org" in title
    assert "Acme Org" in template.get_email_subject(linked_notification)


def test_in_app_body(linked_notification: Notification) -> None:
    _assert_rendered(StaffMemberRecordAutoLinkedTemplate().get_in_app_body(linked_notification))


def test_telegram_body(linked_notification: Notification) -> None:
    body = StaffMemberRecordAutoLinkedTemplate().get_telegram_body(linked_notification)
    _assert_rendered(body)
    assert "jane@example.com" in body


def test_email_text_body(linked_notification: Notification) -> None:
    body = StaffMemberRecordAutoLinkedTemplate().get_email_text_body(linked_notification)
    _assert_rendered(body)
    assert "jane@example.com" in body


def test_email_html_body(linked_notification: Notification) -> None:
    body = StaffMemberRecordAutoLinkedTemplate().get_email_html_body(linked_notification)
    assert body is not None
    _assert_rendered(body)
    assert "jane@example.com" in body


def test_html_body_escapes_user_content(regular_user: RevelUser) -> None:
    notification = Notification.objects.create(
        user=regular_user,
        notification_type=NTYPE,
        context={
            "organization_id": "id",
            "organization_name": "Acme Org",
            "record_id": "id",
            "record_full_name": "<script>alert(1)</script>",
            "member_display_name": "Janie",
            "member_email": "jane@example.com",
            "frontend_url": FRONTEND_URL,
        },
    )
    body = StaffMemberRecordAutoLinkedTemplate().get_email_html_body(notification)
    assert body is not None
    assert "<script>alert(1)</script>" not in body


def test_requires_manage_members_permission(organization: Organization, revel_user_factory: RevelUserFactory) -> None:
    assert NOTIFICATION_REQUIRED_PERMISSIONS[NTYPE] == "manage_members"

    plain_staff = revel_user_factory(username="plain@example.com", email="plain@example.com")
    OrganizationStaff.objects.create(organization=organization, user=plain_staff)
    members_staff = revel_user_factory(username="mm@example.com", email="mm@example.com")
    OrganizationStaff.objects.create(
        organization=organization,
        user=members_staff,
        permissions={"default": {"manage_members": True}},
    )

    recipients = set(get_staff_for_notification(organization.id, NTYPE))

    assert organization.owner in recipients
    assert members_staff in recipients
    assert plain_staff not in recipients


def test_disabled_for_guest_defaults() -> None:
    assert get_guest_notification_type_settings()[NTYPE]["enabled"] is False


def test_email_text_body_is_not_html_escaped(regular_user: RevelUser) -> None:
    """Names with ``&`` or apostrophes read as typed in the plain-text part."""
    notification = Notification.objects.create(
        user=regular_user,
        notification_type=NTYPE,
        context={
            "organization_id": "id",
            "organization_name": "Acme & Co",
            "record_id": "id",
            "record_full_name": "Rock & Roll O'Brien",
            "member_display_name": "Janie",
            "member_email": "jane@example.com",
            "frontend_url": FRONTEND_URL,
        },
    )
    body = StaffMemberRecordAutoLinkedTemplate().get_email_text_body(notification)
    assert body is not None
    assert "Rock & Roll O'Brien" in body
    assert "&amp;" not in body
