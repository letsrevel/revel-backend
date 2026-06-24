"""Organization lifecycle: creation and editing of the organization entity itself."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events import schema
from events.exceptions import RevenueReportCadenceOwnerOnlyError
from events.models import Organization
from events.service.organization_service.contact import (
    _create_and_send_contact_email_verification,
    validate_contact_method,
)
from events.utils.reserved_slug_tokens import find_reserved_token

# Canned message for the revenue-report-cadence owner-only guard. Co-located with
# the service that raises ``RevenueReportCadenceOwnerOnlyError`` so the per-app
# exception handlers render an identical, translatable ``{"detail": ...}`` body.
REVENUE_CADENCE_OWNER_ONLY_MESSAGE = _("Only the organization owner can change the revenue report cadence.")


@transaction.atomic
def create_organization(
    owner: RevelUser,
    name: str,
    contact_email: str,
    description: str | None = None,
    city_id: int | None = None,
    address: str | None = None,
    location_maps_url: str | None = None,
    location_maps_embed: str | None = None,
    slug: str | None = None,
) -> Organization:
    """Create a new organization.

    Args:
        owner: The user who will own the organization
        name: The name of the organization
        contact_email: The contact email for the organization
        description: Optional description for the organization
        city_id: Optional city id for the organization
        address: Optional address for the organization
        location_maps_url: Optional shareable Google Maps URL
        location_maps_embed: Optional Google Maps embed URL for iframe
        slug: Optional explicit slug; when omitted the model derives one from the name.

    Returns:
        The created Organization instance

    Raises:
        HttpError: If the user already owns an organization
    """
    if offending := find_reserved_token(name):
        raise HttpError(
            400,
            str(_('The name contains a reserved word: "%(token)s". Please choose a different name.'))
            % {"token": offending},
        )
    # Check if user already owns an organization
    if Organization.objects.filter(owner=owner).exists():
        raise HttpError(
            400, str(_("You already own an organization. Only one organization per user as owner is allowed."))
        )

    # Check if contact email matches user email and is verified
    contact_email_verified = False
    if contact_email.lower() == owner.email.lower() and owner.email_verified:
        contact_email_verified = True

    # Create the organization
    organization = Organization.objects.create(
        name=name,
        slug=slug or "",  # empty → the SluggedModel mixin derives it from the name
        owner=owner,
        description=description or "",
        contact_email=contact_email,
        contact_email_verified=contact_email_verified,
        visibility=Organization.Visibility.STAFF_ONLY,  # Default to staff only
        city_id=city_id,
        address=address,
        location_maps_url=location_maps_url,
        location_maps_embed=location_maps_embed,
    )

    # Send verification email if contact email is not auto-verified
    if not contact_email_verified:
        _create_and_send_contact_email_verification(organization, contact_email, owner)

    return organization


@transaction.atomic
def update_organization(
    organization: Organization, payload: schema.OrganizationEditSchema, *, requester: RevelUser
) -> Organization:
    """Update an organization's editable fields.

    Validates ``contact_method`` against the organization's verified-email state
    and ``revenue_report_cadence`` against the presence of ``billing_email``
    before persisting. Other fields are applied via ``update_db_instance``.

    ``revenue_report_cadence`` controls scheduled financial-report delivery and is
    owner-only, consistent with the owner-only org revenue endpoints. ``edit_organization``
    is staff-grantable, so a non-owner attempting to change the cadence is rejected here
    regardless of which permission got them to this endpoint.
    """
    from events.service import update_db_instance

    data = payload.model_dump(exclude_unset=True)
    if "contact_method" in data:
        validate_contact_method(organization, Organization.ContactMethod(data["contact_method"]))

    if (
        "revenue_report_cadence" in data
        and data["revenue_report_cadence"] != organization.revenue_report_cadence
        and organization.owner_id != requester.pk
    ):
        raise RevenueReportCadenceOwnerOnlyError

    # Apply cadence from payload (if present) before the billing_email check,
    # so the check reflects the intended post-save state.
    effective_cadence = data.get("revenue_report_cadence", organization.revenue_report_cadence)
    if effective_cadence != Organization.RevenueReportCadence.NONE and not organization.billing_email:
        raise ValidationError({"revenue_report_cadence": "A billing email is required to enable scheduled reports."})

    return update_db_instance(organization, payload)
