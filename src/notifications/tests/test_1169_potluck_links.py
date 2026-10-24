"""Potluck notification call-to-actions must link to the event URL (#1169).

The potluck context carries ``event_url`` (made org-aware by enrichment) and never ``frontend_url``, so
templates pointing at ``context.frontend_url`` rendered a button with an empty ``href``.
"""

import re

import pytest

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Organization
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.registry import get_template

pytestmark = pytest.mark.django_db

HOST = "events.tenant-example.org"
EVENT_ID = "0b9a3d1e-0000-4000-8000-000000000001"
# Enrichment makes ``event_url`` org-aware, so an organization on a custom domain gets its own origin.
EVENT_URL = f"https://{HOST}/events/{EVENT_ID}"
POTLUCK_TYPES = [
    NotificationType.POTLUCK_ITEM_CREATED,
    NotificationType.POTLUCK_ITEM_CREATED_AND_CLAIMED,
    NotificationType.POTLUCK_ITEM_UPDATED,
    NotificationType.POTLUCK_ITEM_CLAIMED,
    NotificationType.POTLUCK_ITEM_UNCLAIMED,
    NotificationType.POTLUCK_ITEM_DELETED,
]


@pytest.fixture
def tenant_org(organization: Organization) -> Organization:
    """The organization on an active custom domain, with the platform origin pinned."""
    site = SiteSettings.get_solo()
    site.frontend_base_url = "https://app.letsrevel.io"
    site.save()
    Organization.objects.filter(pk=organization.pk).update(custom_domain=HOST, custom_domain_enabled=True)
    organization.refresh_from_db()
    return organization


def _notification(user: RevelUser, org: Organization, notification_type: NotificationType) -> Notification:
    """A potluck notification the way the signal builds it: no ``frontend_url`` in the context."""
    return Notification.objects.create(
        user=user,
        notification_type=notification_type,
        context={
            "potluck_item_id": "item-id",
            "item_name": "Potato Salad",
            "event_id": EVENT_ID,
            "event_name": "Summer Picnic",
            "organization_id": str(org.id),
            "action": "created",
            "event_url": f"https://app.letsrevel.io/events/{EVENT_ID}",
        },
    )


@pytest.mark.parametrize("notification_type", POTLUCK_TYPES)
def test_email_html_button_links_to_event_url(
    regular_user: RevelUser, tenant_org: Organization, notification_type: NotificationType
) -> None:
    """The email button's href is the org-aware event URL, never empty."""
    notification = _notification(regular_user, tenant_org, notification_type)
    html = get_template(notification_type).get_email_html_body(notification)

    assert html is not None
    assert re.findall(r'<a href="([^"]*)" class="button"', html) == [EVENT_URL]


@pytest.mark.parametrize("notification_type", POTLUCK_TYPES)
def test_every_channel_links_to_event_url(
    regular_user: RevelUser, tenant_org: Organization, notification_type: NotificationType
) -> None:
    """Plain-text email, in-app and Telegram bodies carry the event URL too."""
    notification = _notification(regular_user, tenant_org, notification_type)
    template = get_template(notification_type)

    assert EVENT_URL in template.get_email_text_body(notification)
    assert EVENT_URL in template.get_in_app_body(notification)
    assert EVENT_URL in template.get_telegram_body(notification)
