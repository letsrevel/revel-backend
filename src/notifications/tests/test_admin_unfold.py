"""The notifications admins render through Unfold (owner requirement, #1035)."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse
from unfold.admin import ModelAdmin as UnfoldModelAdmin

from accounts.models import RevelUser
from events.models import Organization
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import EmailSuppression, Notification, NotificationDelivery, NotificationPreference
from notifications.service.email_policy import suppress

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _use_simple_staticfiles(settings: t.Any) -> None:
    """Use plain StaticFilesStorage so admin templates don't need a manifest."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def admin_client(django_user_model: type[RevelUser]) -> Client:
    superuser = django_user_model.objects.create_superuser(
        username="admin@example.com", email="admin@example.com", password="admin-password"
    )
    client = Client()
    client.force_login(superuser)
    return client


def _assert_unfold(response: t.Any) -> None:
    assert response.status_code == 200
    names = [tpl.name for tpl in response.templates if tpl.name]
    assert any(name.startswith("unfold/") for name in names), names


@pytest.mark.parametrize(
    "model", [Notification, NotificationDelivery, NotificationPreference, EmailSuppression], ids=lambda m: m.__name__
)
def test_admin_is_unfold_model_admin(model: type[t.Any]) -> None:
    from django.contrib import admin

    assert isinstance(admin.site._registry[model], UnfoldModelAdmin)


def test_notification_pages(admin_client: Client, regular_user: RevelUser) -> None:
    notif = Notification.objects.create(
        notification_type=NotificationType.TICKET_CREATED, user=regular_user, context={"filler": True}
    )
    NotificationDelivery.objects.create(notification=notif, channel=DeliveryChannel.EMAIL)

    _assert_unfold(admin_client.get(reverse("admin:notifications_notification_changelist")))
    _assert_unfold(admin_client.get(reverse("admin:notifications_notification_add")))
    _assert_unfold(admin_client.get(reverse("admin:notifications_notification_change", args=[notif.pk])))


def test_notification_delivery_pages(admin_client: Client, regular_user: RevelUser) -> None:
    notif = Notification.objects.create(
        notification_type=NotificationType.TICKET_CREATED, user=regular_user, context={"filler": True}
    )
    delivery = NotificationDelivery.objects.create(notification=notif, channel=DeliveryChannel.EMAIL)

    _assert_unfold(admin_client.get(reverse("admin:notifications_notificationdelivery_changelist")))
    _assert_unfold(admin_client.get(reverse("admin:notifications_notificationdelivery_change", args=[delivery.pk])))


def test_notification_preference_pages_show_org_mutes(
    admin_client: Client, regular_user: RevelUser, organization: Organization
) -> None:
    prefs = NotificationPreference.objects.get(user=regular_user)
    prefs.muted_organizations.add(organization)

    changelist = admin_client.get(reverse("admin:notifications_notificationpreference_changelist"))
    _assert_unfold(changelist)
    row = next(obj for obj in changelist.context["cl"].result_list if obj.pk == prefs.pk)
    assert row._muted_org_count == 1
    assert "Muted orgs" in changelist.content.decode()

    change = admin_client.get(reverse("admin:notifications_notificationpreference_change", args=[prefs.pk]))
    _assert_unfold(change)
    content = change.content.decode()
    assert "Organization mutes" in content
    assert 'name="muted_organizations"' in content
    assert organization.name in content

    _assert_unfold(admin_client.get(reverse("admin:notifications_notificationpreference_add")))


def test_email_suppression_pages(admin_client: Client) -> None:
    row = suppress("x@example.com", EmailSuppression.Reason.HARD_BOUNCE, EmailSuppression.Source.PROVIDER)

    _assert_unfold(admin_client.get(reverse("admin:notifications_emailsuppression_changelist")))
    _assert_unfold(admin_client.get(reverse("admin:notifications_emailsuppression_add")))
    # has_change_permission is False: the change URL renders the read-only view.
    _assert_unfold(admin_client.get(reverse("admin:notifications_emailsuppression_change", args=[row.pk])))


def test_send_announcement_view(admin_client: Client) -> None:
    response = admin_client.get(reverse("admin:notifications_notification_send_announcement"))

    _assert_unfold(response)
    assert "Send System Announcement" in response.content.decode()
