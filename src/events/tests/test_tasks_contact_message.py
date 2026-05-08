"""Tests for send_organization_contact_message_email and the post_save signal wiring."""

from unittest.mock import patch

import pytest
from django.core import mail

from accounts.models import RevelUser
from common.tasks import to_safe_email_address
from events.models import Organization, OrganizationContactMessage
from events.tasks import send_organization_contact_message_email

pytestmark = pytest.mark.django_db


@pytest.fixture
def org_in_form_mode(organization: Organization) -> Organization:
    """Organization configured in FORM contact mode with a verified email."""
    organization.contact_email = "info@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.FORM
    organization.save()
    return organization


@pytest.fixture
def contact_message(org_in_form_mode: Organization, organization_owner_user: RevelUser) -> OrganizationContactMessage:
    """A persisted contact message (signal-side effects mocked at the test level)."""
    with patch("events.tasks.send_organization_contact_message_email.delay"):
        return OrganizationContactMessage.objects.create(
            organization=org_in_form_mode,
            sender=organization_owner_user,
            sender_email_snapshot="sender@external.example",
            subject="Hello",
            message="Body of the message",
        )


def test_email_task_sets_to_org_contact_email(contact_message: OrganizationContactMessage) -> None:
    """The To header equals organization.contact_email (after the test-mode safe rewrite)."""
    send_organization_contact_message_email(message_id=str(contact_message.id))

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    expected = [to_safe_email_address(contact_message.organization.contact_email or "")]
    assert sent.to == expected


def test_email_task_sets_reply_to_sender_snapshot(contact_message: OrganizationContactMessage) -> None:
    """Reply-To header equals sender_email_snapshot (after the test-mode safe rewrite)."""
    send_organization_contact_message_email(message_id=str(contact_message.id))

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    # send_email passes reply_to through unchanged (no safe-rewrite), so we just match the snapshot.
    assert sent.reply_to == ["sender@external.example"]


def test_email_task_from_is_platform_noreply(contact_message: OrganizationContactMessage, settings: object) -> None:
    """From header is the platform noreply address, never the sender (DMARC alignment)."""
    send_organization_contact_message_email(message_id=str(contact_message.id))

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    # Default settings.DEFAULT_FROM_EMAIL is the platform's noreply
    from django.conf import settings as django_settings

    assert sent.from_email == django_settings.DEFAULT_FROM_EMAIL
    assert "sender@external.example" not in sent.from_email


def test_email_task_subject_prefixed_with_org_name(contact_message: OrganizationContactMessage) -> None:
    """Subject begins with [<Org name>]."""
    send_organization_contact_message_email(message_id=str(contact_message.id))

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.subject.startswith(f"[{contact_message.organization.name}]")


def test_email_task_skipped_when_org_email_unverified(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """If contact_email_verified became False between persistence and dispatch, skip silently."""
    organization.contact_email = "info@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.FORM
    organization.save()
    with patch("events.tasks.send_organization_contact_message_email.delay"):
        msg = OrganizationContactMessage.objects.create(
            organization=organization,
            sender=organization_owner_user,
            sender_email_snapshot="sender@external.example",
            message="Body",
        )

    # Bypass full_clean (which would reject contact_method=FORM with unverified email)
    # to simulate a corrupted state and verify the dispatcher still skips safely.
    Organization.objects.filter(pk=organization.pk).update(contact_email_verified=False)

    send_organization_contact_message_email(message_id=str(msg.id))

    assert mail.outbox == []
