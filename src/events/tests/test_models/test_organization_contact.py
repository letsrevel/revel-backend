"""Tests for Organization.contact_method validation and OrganizationContactMessage model."""

import pytest
from django.core.exceptions import ValidationError

from accounts.models import RevelUser
from events.models import Organization, OrganizationContactMessage


@pytest.mark.django_db
def test_clean_allows_contact_method_none_without_verified_email(organization_owner_user: RevelUser) -> None:
    """contact_method=NONE never requires a verified email."""
    org = Organization(
        name="No Contact",
        slug="no-contact",
        owner=organization_owner_user,
        contact_email=None,
        contact_email_verified=False,
        contact_method=Organization.ContactMethod.NONE,
    )
    org.full_clean(exclude={"city"})  # city has its own validation we don't care about


@pytest.mark.django_db
def test_clean_rejects_contact_method_email_without_verified_email(organization_owner_user: RevelUser) -> None:
    """contact_method=EMAIL requires contact_email and contact_email_verified=True."""
    org = Organization(
        name="Email Mode",
        slug="email-mode",
        owner=organization_owner_user,
        contact_email="info@example.com",
        contact_email_verified=False,
        contact_method=Organization.ContactMethod.EMAIL,
    )
    with pytest.raises(ValidationError) as exc_info:
        org.full_clean(exclude={"city"})
    assert "contact_method" in exc_info.value.message_dict


@pytest.mark.django_db
def test_clean_rejects_contact_method_form_without_verified_email(organization_owner_user: RevelUser) -> None:
    """contact_method=FORM requires contact_email and contact_email_verified=True."""
    org = Organization(
        name="Form Mode",
        slug="form-mode",
        owner=organization_owner_user,
        contact_email="info@example.com",
        contact_email_verified=False,
        contact_method=Organization.ContactMethod.FORM,
    )
    with pytest.raises(ValidationError) as exc_info:
        org.full_clean(exclude={"city"})
    assert "contact_method" in exc_info.value.message_dict


@pytest.mark.django_db
def test_clean_rejects_contact_method_email_when_email_is_blank(organization_owner_user: RevelUser) -> None:
    """contact_method=EMAIL must have a non-empty contact_email."""
    org = Organization(
        name="Email Blank",
        slug="email-blank",
        owner=organization_owner_user,
        contact_email=None,
        contact_email_verified=True,
        contact_method=Organization.ContactMethod.EMAIL,
    )
    with pytest.raises(ValidationError) as exc_info:
        org.full_clean(exclude={"city"})
    assert "contact_method" in exc_info.value.message_dict


@pytest.mark.django_db
def test_clean_allows_contact_method_email_with_verified_email(organization_owner_user: RevelUser) -> None:
    """contact_method=EMAIL passes when contact_email is set and verified."""
    org = Organization(
        name="Verified Email",
        slug="verified-email",
        owner=organization_owner_user,
        contact_email="info@example.com",
        contact_email_verified=True,
        contact_method=Organization.ContactMethod.EMAIL,
    )
    org.full_clean(exclude={"city"})


@pytest.mark.django_db
def test_clean_allows_contact_method_form_with_verified_email(organization_owner_user: RevelUser) -> None:
    """contact_method=FORM passes when contact_email is set and verified."""
    org = Organization(
        name="Verified Form",
        slug="verified-form",
        owner=organization_owner_user,
        contact_email="info@example.com",
        contact_email_verified=True,
        contact_method=Organization.ContactMethod.FORM,
    )
    org.full_clean(exclude={"city"})


@pytest.mark.django_db
def test_organization_contact_message_creation(organization: Organization, organization_owner_user: RevelUser) -> None:
    """OrganizationContactMessage persists with sender and snapshot."""
    msg = OrganizationContactMessage.objects.create(
        organization=organization,
        sender=organization_owner_user,
        sender_email_snapshot=organization_owner_user.email,
        subject="Hello",
        message="Body of the message",
    )
    assert msg.organization_id == organization.id
    assert msg.sender_id == organization_owner_user.id
    assert msg.sender_email_snapshot == organization_owner_user.email
    assert msg.subject == "Hello"
    assert msg.message == "Body of the message"


@pytest.mark.django_db
def test_organization_contact_message_sender_set_null_on_user_delete(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Deleting the sender keeps the contact message but nulls the FK."""
    other_user = RevelUser.objects.create_user(
        username="contact_sender", email="contact_sender@example.com", password="pass"
    )
    msg = OrganizationContactMessage.objects.create(
        organization=organization,
        sender=other_user,
        sender_email_snapshot=other_user.email,
        message="Body",
    )
    other_user.delete()
    msg.refresh_from_db()
    assert msg.sender_id is None
    assert msg.sender_email_snapshot == "contact_sender@example.com"
