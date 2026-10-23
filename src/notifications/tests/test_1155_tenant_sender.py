"""Attendee transactional mail goes out with the org sender for white-label tenants only (#1155)."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, Organization
from notifications.enums import (
    MANDATORY_TYPES,
    ORG_SENDER_TYPES,
    TENANT_ORG_SENDER_TYPES,
    DeliveryChannel,
    DeliveryStatus,
    NotificationType,
)
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.email import EmailChannel
from notifications.service.org_sender import is_white_label_tenant

pytestmark = pytest.mark.django_db

DEFAULT_FROM = "Let's Revel <revel@letsrevel.io>"
ORG_FROM = "Test Org via Revel <test-org@mail.letsrevel.io>"
HOST = "events.example.com"
ONE_CLICK = "https://api.example.test/api/notification-preferences/one-click?token="
FRONTEND = "https://app.letsrevel.io"


@pytest.fixture(autouse=True)
def _email_settings(settings: t.Any) -> None:
    settings.DEFAULT_FROM_EMAIL = DEFAULT_FROM
    settings.ORG_EMAIL_DOMAIN = "mail.letsrevel.io"
    settings.BASE_URL = "https://api.example.test"
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.frontend_base_url = FRONTEND
    site.save()


@pytest.fixture
def tenant_org(organization: Organization) -> Organization:
    Organization.objects.filter(pk=organization.pk).update(
        custom_domain=HOST,
        custom_domain_enabled=True,
        contact_email="hello@org.example",
        contact_email_verified=True,
    )
    organization.refresh_from_db()
    return organization


def _envelope(
    user: RevelUser, notification_type: str, context: dict[str, t.Any]
) -> tuple[str, list[str], dict[str, str]]:
    notification = Notification.objects.create(notification_type=notification_type, user=user, context=context)
    delivery = NotificationDelivery.objects.create(
        notification=notification, channel=DeliveryChannel.EMAIL, status=DeliveryStatus.PENDING
    )
    return EmailChannel._envelope(notification, delivery)


TENANT_TYPES = sorted(TENANT_ORG_SENDER_TYPES)


def test_tenant_types_are_disjoint_from_org_sender_types_and_staff_facing_types() -> None:
    assert not TENANT_ORG_SENDER_TYPES & ORG_SENDER_TYPES
    assert NotificationType.MEMBERSHIP_REQUEST_CREATED not in TENANT_ORG_SENDER_TYPES
    assert NotificationType.STAFF_TICKET_CREATED not in TENANT_ORG_SENDER_TYPES
    assert NotificationType.STAFF_SERIES_PASS_PURCHASED not in TENANT_ORG_SENDER_TYPES
    assert NotificationType.RSVP_CONFIRMATION not in TENANT_ORG_SENDER_TYPES


@pytest.mark.parametrize(
    "notification_type",
    [
        # Org staff/owner alerts: never sent to the attendee, so never on the tenant sender.
        NotificationType.STAFF_SERIES_PASS_PURCHASED,
        NotificationType.STAFF_SERIES_PASS_CANCELLED,
        NotificationType.RSVP_CONFIRMATION,
        NotificationType.RSVP_UPDATED,
        NotificationType.RSVP_CANCELLED,
    ],
)
def test_staff_facing_types_stay_on_platform_sender_for_tenant(
    regular_user: RevelUser, tenant_org: Organization, notification_type: str
) -> None:
    sender, reply_to, headers = _envelope(regular_user, notification_type, {"organization_id": str(tenant_org.id)})

    assert sender == DEFAULT_FROM
    assert reply_to == []
    assert "Feedback-ID" not in headers


@pytest.mark.parametrize("notification_type", TENANT_TYPES)
def test_tenant_org_uses_org_sender_and_verified_reply_to(
    regular_user: RevelUser, tenant_org: Organization, notification_type: str
) -> None:
    sender, reply_to, headers = _envelope(regular_user, notification_type, {"organization_id": str(tenant_org.id)})

    assert sender == ORG_FROM
    assert reply_to == ["hello@org.example"]
    assert headers["Feedback-ID"] == f"test-org:{notification_type}:revel"
    assert headers["X-Mailin-custom"].endswith(f"|org:{tenant_org.id}")


@pytest.mark.parametrize("notification_type", TENANT_TYPES)
def test_tenant_org_keeps_platform_list_unsubscribe(
    regular_user: RevelUser, tenant_org: Organization, notification_type: str
) -> None:
    _sender, _reply_to, headers = _envelope(regular_user, notification_type, {"organization_id": str(tenant_org.id)})

    unsubscribe = headers["List-Unsubscribe"]
    assert HOST not in unsubscribe
    if notification_type in MANDATORY_TYPES:
        assert unsubscribe.startswith(f"<{FRONTEND}/unsubscribe?token=")
        assert "List-Unsubscribe-Post" not in headers
    else:
        assert unsubscribe.startswith(f"<{ONE_CLICK}")
        assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_tenant_reply_to_omitted_when_contact_email_unverified(
    regular_user: RevelUser, tenant_org: Organization
) -> None:
    Organization.objects.filter(pk=tenant_org.pk).update(contact_email_verified=False)

    sender, reply_to, _headers = _envelope(
        regular_user, NotificationType.TICKET_CREATED, {"organization_id": str(tenant_org.id)}
    )

    assert sender == ORG_FROM
    assert reply_to == []


def test_tenant_org_resolved_through_event(
    regular_user: RevelUser, tenant_org: Organization, public_event: Event
) -> None:
    sender, reply_to, _headers = _envelope(
        regular_user, NotificationType.TICKET_CREATED, {"event_id": str(public_event.id)}
    )

    assert sender == ORG_FROM
    assert reply_to == ["hello@org.example"]


def test_role_slug_tenant_falls_back_to_platform_sender(regular_user: RevelUser, tenant_org: Organization) -> None:
    Organization.objects.filter(pk=tenant_org.pk).update(slug="support")

    sender, _reply_to, _headers = _envelope(
        regular_user, NotificationType.TICKET_CREATED, {"organization_id": str(tenant_org.id)}
    )

    assert sender == DEFAULT_FROM


@pytest.mark.parametrize("notification_type", TENANT_TYPES)
def test_non_tenant_org_keeps_platform_sender(
    regular_user: RevelUser, organization: Organization, notification_type: str
) -> None:
    Organization.objects.filter(pk=organization.pk).update(
        contact_email="hello@org.example", contact_email_verified=True
    )

    sender, reply_to, headers = _envelope(regular_user, notification_type, {"organization_id": str(organization.id)})

    assert sender == DEFAULT_FROM
    assert reply_to == []
    assert "Feedback-ID" not in headers
    assert headers["X-Mailin-custom"].startswith("delivery:") and "|org:" not in headers["X-Mailin-custom"]


@pytest.mark.parametrize("notification_type", TENANT_TYPES)
def test_kill_switch_off_keeps_platform_sender(
    regular_user: RevelUser, tenant_org: Organization, notification_type: str
) -> None:
    Organization.objects.filter(pk=tenant_org.pk).update(custom_domain_enabled=False)

    sender, reply_to, _headers = _envelope(regular_user, notification_type, {"organization_id": str(tenant_org.id)})

    assert sender == DEFAULT_FROM
    assert reply_to == []


def test_domain_enabled_but_not_active_keeps_platform_sender(regular_user: RevelUser, tenant_org: Organization) -> None:
    Organization.objects.filter(pk=tenant_org.pk).update(custom_domain=None)

    sender, _reply_to, _headers = _envelope(
        regular_user, NotificationType.TICKET_CREATED, {"organization_id": str(tenant_org.id)}
    )

    assert sender == DEFAULT_FROM


def test_tenant_org_without_resolvable_org_uses_platform_sender(regular_user: RevelUser) -> None:
    sender, reply_to, headers = _envelope(regular_user, NotificationType.TICKET_CREATED, {"filler": True})

    assert sender == DEFAULT_FROM
    assert reply_to == []
    assert "Feedback-ID" not in headers


@pytest.mark.parametrize("notification_type", sorted(ORG_SENDER_TYPES))
def test_existing_org_sender_types_unchanged_for_tenant_and_non_tenant(
    regular_user: RevelUser, organization: Organization, notification_type: str
) -> None:
    """ORG_SENDER_TYPES keep the org sender and one-click header whether or not the org is a tenant."""
    for tenant in (False, True):
        Organization.objects.filter(pk=organization.pk).update(
            custom_domain=HOST if tenant else None, custom_domain_enabled=tenant
        )

        sender, _reply_to, headers = _envelope(
            regular_user, notification_type, {"organization_id": str(organization.id)}
        )

        assert sender == ORG_FROM
        assert headers["List-Unsubscribe"].startswith(f"<{ONE_CLICK}")
        assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


@pytest.mark.parametrize(
    "notification_type", sorted(set(NotificationType) - ORG_SENDER_TYPES - TENANT_ORG_SENDER_TYPES)
)
def test_other_types_stay_on_platform_sender_even_for_tenant(
    regular_user: RevelUser, tenant_org: Organization, notification_type: str
) -> None:
    sender, reply_to, _headers = _envelope(regular_user, notification_type, {"organization_id": str(tenant_org.id)})

    assert sender == DEFAULT_FROM
    assert reply_to == []


def test_is_white_label_tenant(organization: Organization) -> None:
    organization.custom_domain, organization.custom_domain_enabled = HOST, True
    assert is_white_label_tenant(organization)
    organization.custom_domain_enabled = False
    assert not is_white_label_tenant(organization)
    organization.custom_domain, organization.custom_domain_enabled = None, True
    assert not is_white_label_tenant(organization)


@pytest.mark.parametrize("notification_type", TENANT_TYPES)
def test_non_tenant_envelope_costs_one_org_lookup(
    regular_user: RevelUser, organization: Organization, notification_type: str
) -> None:
    """Non-tenant orgs pay the same single org lookup: the gate needs the org to decide."""
    notification = Notification.objects.create(
        notification_type=notification_type,
        user=regular_user,
        context={"organization_id": str(organization.id)},
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification, channel=DeliveryChannel.EMAIL, status=DeliveryStatus.PENDING
    )
    SiteSettings.get_solo()

    with CaptureQueriesContext(connection) as queries:
        EmailChannel._envelope(notification, delivery)

    assert len(queries.captured_queries) == 1


def test_tenant_envelope_costs_one_org_query(regular_user: RevelUser, tenant_org: Organization) -> None:
    """The tenant gate rides on the sender-org lookup: no extra query per email."""
    notification = Notification.objects.create(
        notification_type=NotificationType.TICKET_CREATED,
        user=regular_user,
        context={"organization_id": str(tenant_org.id)},
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification, channel=DeliveryChannel.EMAIL, status=DeliveryStatus.PENDING
    )
    # Warm the SiteSettings singleton cache so only the org lookup is counted.
    SiteSettings.get_solo()

    with CaptureQueriesContext(connection) as queries:
        EmailChannel._envelope(notification, delivery)

    assert len([q for q in queries.captured_queries if '"events_organization"' in q["sql"]]) == 1
    assert len(queries.captured_queries) == 1


@patch("notifications.service.templates.registry.get_template")
def test_delivered_tenant_ticket_email_end_to_end(
    mock_get_template: MagicMock, regular_user: RevelUser, tenant_org: Organization
) -> None:
    template = MagicMock()
    template.get_email_subject.return_value = "Subject"
    template.get_email_text_body.return_value = "Body"
    template.get_email_html_body.return_value = "<p>Body</p>"
    template.get_email_attachments.return_value = {}
    mock_get_template.return_value = template
    notification = Notification.objects.create(
        notification_type=NotificationType.TICKET_CREATED,
        user=regular_user,
        context={"organization_id": str(tenant_org.id)},
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification, channel=DeliveryChannel.EMAIL, status=DeliveryStatus.PENDING
    )

    assert EmailChannel().deliver(notification, delivery) is True

    msg = mail.outbox[-1]
    assert msg.from_email == ORG_FROM
    assert msg.reply_to == ["hello@org.example"]
    assert msg.extra_headers["List-Unsubscribe"].startswith(f"<{FRONTEND}/unsubscribe?token=")


def test_footer_unsubscribe_link_stays_on_platform_for_tenant(
    regular_user: RevelUser, tenant_org: Organization
) -> None:
    from notifications.service.templates.registry import get_template

    notification = Notification.objects.create(
        notification_type=NotificationType.RSVP_CONFIRMATION,
        user=regular_user,
        context={"organization_id": str(tenant_org.id)},
    )
    context = get_template(notification.notification_type)._get_template_context(notification)

    link = context["context"]["unsubscribe_link"]
    assert link.startswith(f"{FRONTEND}/unsubscribe?token=")
    assert HOST not in link
