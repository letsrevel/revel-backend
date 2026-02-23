"""Tests for system announcement notification and admin view."""

import typing as t
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import RevelUser
from notifications.context_schemas import SystemAnnouncementContext, validate_notification_context
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.dispatcher import NotificationData, bulk_create_notifications
from notifications.service.templates.registry import get_template, is_template_registered
from notifications.service.templates.system_templates import SystemAnnouncementTemplate

pytestmark = pytest.mark.django_db

SEND_ANNOUNCEMENT_URL = "admin:notifications_notification_send_announcement"


# ===== Helpers =====


def _create_system_announcement(
    user: RevelUser,
    title: str = "Test Title",
    body: str = "Test body content",
    policy_url: str | None = None,
) -> Notification:
    """Create a system announcement notification directly."""
    context: dict[str, t.Any] = {
        "announcement_title": title,
        "announcement_body": body,
    }
    if policy_url:
        context["policy_url"] = policy_url
    return Notification.objects.create(
        user=user,
        notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
        context=context,
    )


# ===== Context Schema Tests =====


class TestSystemAnnouncementContext:
    """Tests for SystemAnnouncementContext schema validation."""

    def test_valid_context_with_required_fields(self, regular_user: RevelUser) -> None:
        """Should accept context with all required fields."""
        context: dict[str, t.Any] = {
            "announcement_title": "Privacy Updated",
            "announcement_body": "We changed our policy.",
        }
        # Should not raise
        validate_notification_context(NotificationType.SYSTEM_ANNOUNCEMENT, context)

    def test_valid_context_with_optional_policy_url(self, regular_user: RevelUser) -> None:
        """Should accept context with optional policy_url."""
        context: dict[str, t.Any] = {
            "announcement_title": "Privacy Updated",
            "announcement_body": "We changed our policy.",
            "policy_url": "https://example.com/privacy",
        }
        validate_notification_context(NotificationType.SYSTEM_ANNOUNCEMENT, context)

    def test_rejects_missing_required_fields(self, regular_user: RevelUser) -> None:
        """Should reject context missing required fields."""
        context: dict[str, t.Any] = {
            "announcement_title": "Title only",
            # Missing announcement_body
        }
        with pytest.raises(ValueError, match="Missing required context keys"):
            validate_notification_context(NotificationType.SYSTEM_ANNOUNCEMENT, context)

    def test_schema_registered_in_registry(self) -> None:
        """SYSTEM_ANNOUNCEMENT should be registered in context schema registry."""
        from notifications.context_schemas import NOTIFICATION_CONTEXT_SCHEMAS

        assert NotificationType.SYSTEM_ANNOUNCEMENT in NOTIFICATION_CONTEXT_SCHEMAS
        assert NOTIFICATION_CONTEXT_SCHEMAS[NotificationType.SYSTEM_ANNOUNCEMENT] is SystemAnnouncementContext


# ===== Template Tests =====


class TestSystemAnnouncementTemplate:
    """Tests for SystemAnnouncementTemplate."""

    def test_template_is_registered(self) -> None:
        """Should be registered in the global template registry."""
        assert is_template_registered(NotificationType.SYSTEM_ANNOUNCEMENT)

    def test_get_template_returns_correct_type(self) -> None:
        """Should return a SystemAnnouncementTemplate instance."""
        template = get_template(NotificationType.SYSTEM_ANNOUNCEMENT)
        assert isinstance(template, SystemAnnouncementTemplate)

    def test_get_in_app_title(self, regular_user: RevelUser) -> None:
        """Should return the announcement title."""
        notification = _create_system_announcement(
            user=regular_user,
            title="Privacy Policy Updated",
        )
        template = SystemAnnouncementTemplate()

        title = template.get_in_app_title(notification)

        assert title == "Privacy Policy Updated"

    def test_get_in_app_title_fallback(self, regular_user: RevelUser) -> None:
        """Should return fallback when title is missing from context."""
        notification = Notification.objects.create(
            user=regular_user,
            notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
            context={"announcement_body": "body"},
        )
        template = SystemAnnouncementTemplate()

        title = template.get_in_app_title(notification)

        assert title  # Should have a fallback value

    def test_get_email_subject(self, regular_user: RevelUser) -> None:
        """Should return subject with Revel prefix."""
        notification = _create_system_announcement(
            user=regular_user,
            title="Privacy Policy Updated",
        )
        template = SystemAnnouncementTemplate()

        subject = template.get_email_subject(notification)

        assert "Privacy Policy Updated" in subject
        assert "Revel" in subject

    def test_get_email_subject_fallback(self, regular_user: RevelUser) -> None:
        """Should return fallback subject when title is missing."""
        notification = Notification.objects.create(
            user=regular_user,
            notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
            context={"announcement_body": "body"},
        )
        template = SystemAnnouncementTemplate()

        subject = template.get_email_subject(notification)

        assert "Revel" in subject


# ===== Bulk Creation Tests =====


class TestSystemAnnouncementBulkCreate:
    """Tests for bulk creating system announcement notifications."""

    def test_bulk_create_system_announcements(self, regular_user: RevelUser) -> None:
        """Should bulk create notifications for multiple users."""
        context: dict[str, t.Any] = {
            "announcement_title": "Update",
            "announcement_body": "Body text",
        }
        data = [
            NotificationData(
                notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
                user=regular_user,
                context=context,
            ),
        ]

        created = bulk_create_notifications(data)

        assert len(created) == 1
        assert created[0].notification_type == NotificationType.SYSTEM_ANNOUNCEMENT
        assert created[0].user == regular_user
        assert created[0].context == context

    def test_bulk_create_rejects_invalid_context(self, regular_user: RevelUser) -> None:
        """Should reject invalid context in bulk creation."""
        data = [
            NotificationData(
                notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
                user=regular_user,
                context={"announcement_title": "No body"},
            ),
        ]

        with pytest.raises(ValueError, match="Missing required context keys"):
            bulk_create_notifications(data)


# ===== Admin View Tests =====


@pytest.fixture
def superuser(django_user_model: type[RevelUser]) -> RevelUser:
    """A superuser for admin access."""
    return django_user_model.objects.create_superuser(
        username="admin@example.com",
        email="admin@example.com",
        password="admin-password",
    )


@pytest.fixture
def staff_user(django_user_model: type[RevelUser]) -> RevelUser:
    """A staff (non-superuser) user."""
    return django_user_model.objects.create_user(
        username="staff@example.com",
        email="staff@example.com",
        password="staff-password",
        is_staff=True,
    )


@pytest.fixture
def admin_client(superuser: RevelUser) -> Client:
    """A Django test client logged in as superuser."""
    client = Client()
    client.force_login(superuser)
    return client


@pytest.fixture(autouse=True)
def _use_simple_staticfiles(settings: t.Any) -> None:
    """Use plain StaticFilesStorage so admin templates don't need a manifest."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


class TestSendSystemAnnouncementAdminView:
    """Tests for the admin Send System Announcement view."""

    def test_get_renders_form(self, admin_client: Client) -> None:
        """GET should render the announcement form for a superuser."""
        url = reverse(SEND_ANNOUNCEMENT_URL)
        response = admin_client.get(url)

        assert response.status_code == 200
        assert "form" in response.context
        assert b"Send System Announcement" in response.content

    def test_non_superuser_redirected(self, staff_user: RevelUser) -> None:
        """Non-superuser staff should be redirected to the admin index."""
        client = Client()
        client.force_login(staff_user)
        url = reverse(SEND_ANNOUNCEMENT_URL)

        response = client.get(url)

        assert response.status_code == 302
        assert response.url == reverse("admin:index")  # type: ignore[attr-defined]

    @patch("notifications.admin.dispatch_notifications_batch")
    def test_post_creates_and_dispatches(
        self,
        mock_dispatch: t.Any,
        admin_client: Client,
        regular_user: RevelUser,
    ) -> None:
        """POST should create notifications and dispatch via Celery."""
        url = reverse(SEND_ANNOUNCEMENT_URL)

        response = admin_client.post(
            url,
            {
                "title": "Privacy Updated",
                "body": "We changed the policy.",
                "url": "https://example.com/privacy",
            },
        )

        assert response.status_code == 302

        # Notification created — exclude superuser from count expectation
        notifications = Notification.objects.filter(
            notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
        )
        assert notifications.count() == 1
        notif = notifications.filter(user=regular_user).first()
        assert notif is not None
        assert notif.context["announcement_title"] == "Privacy Updated"
        assert notif.context["announcement_body"] == "We changed the policy."
        assert notif.context["policy_url"] == "https://example.com/privacy"

        mock_dispatch.delay.assert_called_once()

    @patch("notifications.admin.dispatch_notifications_batch")
    def test_post_with_url_sets_policy_url(
        self,
        mock_dispatch: t.Any,
        admin_client: Client,
        regular_user: RevelUser,
    ) -> None:
        """POST with URL should include policy_url in context."""
        url = reverse(SEND_ANNOUNCEMENT_URL)

        admin_client.post(
            url,
            {
                "title": "Title",
                "body": "Body",
                "url": "https://example.com/policy",
            },
        )

        notif = Notification.objects.filter(user=regular_user).first()
        assert notif is not None
        assert notif.context["policy_url"] == "https://example.com/policy"

    @patch("notifications.admin.dispatch_notifications_batch")
    def test_post_without_url_omits_policy_url(
        self,
        mock_dispatch: t.Any,
        admin_client: Client,
        regular_user: RevelUser,
    ) -> None:
        """POST without URL should not include policy_url in context."""
        url = reverse(SEND_ANNOUNCEMENT_URL)

        admin_client.post(url, {"title": "Title", "body": "Body"})

        notif = Notification.objects.filter(user=regular_user).first()
        assert notif is not None
        assert "policy_url" not in notif.context

    @patch("notifications.admin.dispatch_notifications_batch")
    def test_post_with_include_guests(
        self,
        mock_dispatch: t.Any,
        admin_client: Client,
        regular_user: RevelUser,
        guest_user: RevelUser,
    ) -> None:
        """POST with include_guests should notify guest users too."""
        url = reverse(SEND_ANNOUNCEMENT_URL)

        admin_client.post(
            url,
            {"title": "Title", "body": "Body", "include_guests": "on"},
        )

        notified_users = set(
            Notification.objects.filter(
                notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
            ).values_list("user_id", flat=True)
        )
        assert regular_user.id in notified_users
        assert guest_user.id in notified_users

    @patch("notifications.admin.dispatch_notifications_batch")
    def test_post_excludes_guests_by_default(
        self,
        mock_dispatch: t.Any,
        admin_client: Client,
        regular_user: RevelUser,
        guest_user: RevelUser,
    ) -> None:
        """POST without include_guests should exclude guest users."""
        url = reverse(SEND_ANNOUNCEMENT_URL)

        admin_client.post(url, {"title": "Title", "body": "Body"})

        notified_users = set(
            Notification.objects.filter(
                notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
            ).values_list("user_id", flat=True)
        )
        assert regular_user.id in notified_users
        assert guest_user.id not in notified_users

    def test_post_with_empty_fields_shows_errors(self, admin_client: Client) -> None:
        """POST with empty required fields should show form errors."""
        url = reverse(SEND_ANNOUNCEMENT_URL)

        response = admin_client.post(url, {"title": "", "body": ""})

        assert response.status_code == 200
        assert response.context["form"].errors

    @patch("notifications.admin.dispatch_notifications_batch")
    def test_post_with_no_active_users_shows_info(
        self,
        mock_dispatch: t.Any,
        admin_client: Client,
        django_user_model: type[RevelUser],
    ) -> None:
        """POST should show info message when no matching users exist."""
        # Deactivate all non-superuser users (superuser is excluded by guest=False
        # filter, but is_active=False also removes them from the queryset)
        django_user_model.objects.filter(is_superuser=False).update(is_active=False)
        url = reverse(SEND_ANNOUNCEMENT_URL)

        response = admin_client.post(url, {"title": "Title", "body": "Body"})

        assert response.status_code == 302
        assert (
            Notification.objects.filter(
                notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
            ).count()
            == 0
        )
        mock_dispatch.delay.assert_not_called()


# ===== Guest User Preference Tests =====


class TestSystemAnnouncementGuestPreferences:
    """Tests that SYSTEM_ANNOUNCEMENT is enabled for guest users."""

    def test_guest_user_receives_system_announcements(self, guest_user: RevelUser) -> None:
        """Guest users should NOT have SYSTEM_ANNOUNCEMENT disabled."""
        from notifications.service.dispatcher import determine_delivery_channels

        channels = determine_delivery_channels(guest_user, NotificationType.SYSTEM_ANNOUNCEMENT)

        # SYSTEM_ANNOUNCEMENT is not in the disabled list, so guests receive it
        assert len(channels) > 0

    def test_system_announcement_not_in_guest_disabled_types(self) -> None:
        """SYSTEM_ANNOUNCEMENT should not be in the guest disabled types list."""
        from notifications.signals.user import _get_guest_notification_type_settings

        disabled_settings = _get_guest_notification_type_settings()

        assert NotificationType.SYSTEM_ANNOUNCEMENT not in disabled_settings
