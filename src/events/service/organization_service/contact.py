"""Organization contact concerns: email verification, contact method, and contact messages."""

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.jwt import blacklist as blacklist_token
from accounts.jwt import check_blacklist, create_token
from accounts.models import RevelUser
from accounts.service.account import token_to_payload
from events import schema, tasks
from events.models import Organization, OrganizationContactMessage


def _create_and_send_contact_email_verification(
    organization: Organization,
    email: str,
    user: RevelUser,
) -> str:
    """Create verification token and send verification email.

    Args:
        organization: The organization
        email: The email to verify
        user: The user associated with the verification

    Returns:
        The verification token
    """
    verification_payload = schema.VerifyOrganizationContactEmailJWTPayloadSchema(
        organization_id=organization.id,
        user_id=user.id,
        email=email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(verification_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    # Send verification email
    def send_verification_email() -> None:
        tasks.send_organization_contact_email_verification.delay(
            email=email,
            token=token,
            organization_name=organization.name,
            organization_slug=organization.slug,
        )

    transaction.on_commit(send_verification_email)
    return token


@transaction.atomic
def update_contact_email(organization: Organization, new_email: str, requester: RevelUser) -> str:
    """Update organization contact email and send verification.

    Self-heal: changing to a new (unverified) email forces ``contact_method`` back
    to ``NONE``. This preserves the resolver invariant that a non-NONE contact
    method always points at a verified email, without surprising owners with a
    hard 400 on a normal email-change flow.

    Args:
        organization: The organization to update
        new_email: The new contact email address
        requester: The user requesting the change

    Returns:
        The verification token to be sent via email

    Raises:
        HttpError: If the email is the same as current one
    """
    # Check if email is different from current
    if organization.contact_email and organization.contact_email.lower() == new_email.lower():
        raise HttpError(400, str(_("This is already the contact email for this organization.")))

    # Check if new email matches requester's verified email
    if new_email.lower() == requester.email.lower() and requester.email_verified:
        # Automatically verify since it matches the user's verified email
        organization.contact_email = new_email
        organization.contact_email_verified = True
        organization.save(update_fields=["contact_email", "contact_email_verified"])
        return ""  # No token needed

    # Update the email but mark as unverified, and reset contact_method.
    organization.contact_email = new_email
    organization.contact_email_verified = False
    organization.contact_method = Organization.ContactMethod.NONE
    organization.save(update_fields=["contact_email", "contact_email_verified", "contact_method"])

    # Create verification token and send email
    return _create_and_send_contact_email_verification(organization, new_email, requester)


def create_contact_message(
    organization: Organization,
    sender: RevelUser,
    *,
    subject: str,
    message: str,
) -> OrganizationContactMessage:
    """Persist a contact-form submission for an organization.

    The caller is responsible for verifying that the org's ``contact_method``
    is ``FORM``; this function does not re-check the precondition. Sender's
    email is captured from the user record so that downstream delivery uses
    the verified address regardless of later account changes.
    """
    if organization.contact_method != Organization.ContactMethod.FORM:
        raise HttpError(400, str(_("Contact form is not enabled for this organization.")))

    return OrganizationContactMessage.objects.create(
        organization=organization,
        sender=sender,
        sender_email_snapshot=sender.email,
        subject=subject,
        message=message,
    )


def validate_contact_method(organization: Organization, contact_method: Organization.ContactMethod) -> None:
    """Validate that the requested ``contact_method`` is allowed for this org.

    Setting EMAIL or FORM requires a verified contact email. NONE is always
    allowed. Raises ``HttpError(400)`` if the rule is violated.
    """
    if contact_method == Organization.ContactMethod.NONE:
        return
    if not (organization.contact_email and organization.contact_email_verified):
        raise HttpError(
            400,
            str(_("A verified contact email is required to enable a contact method other than NONE.")),
        )


@transaction.atomic
def verify_contact_email(token: str) -> Organization:
    """Verify an organization's contact email.

    Args:
        token: The verification token

    Returns:
        The organization with verified contact email

    Raises:
        HttpError: If token is invalid or organization not found
    """
    payload = token_to_payload(token, schema.VerifyOrganizationContactEmailJWTPayloadSchema)
    check_blacklist(payload.jti)

    organization = Organization.objects.filter(id=payload.organization_id).first()
    if not organization:
        raise HttpError(400, str(_("Organization not found.")))

    # Check that the email in the token matches the current contact email
    if not organization.contact_email or organization.contact_email.lower() != payload.email.lower():
        raise HttpError(400, str(_("This verification link is for a different email address.")))

    # Mark as verified
    blacklist_token(token)
    organization.contact_email_verified = True
    organization.save(update_fields=["contact_email_verified"])

    return organization
