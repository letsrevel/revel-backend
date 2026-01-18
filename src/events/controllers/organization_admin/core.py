from ninja import File
from ninja.files import UploadedFile
from ninja_extra import api_controller, route

from accounts.schema import VerifyEmailSchema
from common.authentication import I18nJWTAuth
from common.schema import EmailSchema, ValidationErrorResponse
from common.thumbnails.service import delete_image_with_derivatives
from common.throttling import UserDefaultThrottle, WriteThrottle
from common.utils import safe_save_uploaded_file
from events import models, schema
from events.controllers.permissions import IsOrganizationOwner, IsOrganizationStaff, OrganizationPermission
from events.service import organization_service, stripe_service, update_db_instance

from .base import OrganizationAdminBaseController


@api_controller("/organization-admin/{slug}", auth=I18nJWTAuth(), tags=["Organization Admin"], throttle=WriteThrottle())
class OrganizationAdminCoreController(OrganizationAdminBaseController):
    """Core organization admin operations.

    Handles organization details, contact email, Stripe integration, and media uploads.
    """

    @route.get(
        "",
        url_name="get_organization_admin",
        response=schema.OrganizationAdminDetailSchema,
        permissions=[IsOrganizationStaff()],
        throttle=UserDefaultThrottle(),
    )
    def get_organization(self, slug: str) -> models.Organization:
        """Get comprehensive organization details including all platform fee and Stripe fields."""
        return self.get_one(slug)

    @route.put(
        "",
        url_name="edit_organization",
        response=schema.OrganizationRetrieveSchema,
        permissions=[OrganizationPermission("edit_organization")],
    )
    def update_organization(self, slug: str, payload: schema.OrganizationEditSchema) -> models.Organization:
        """Update organization by slug.

        Note: contact_email cannot be updated through this endpoint.
        Use the update-contact-email endpoint instead for email changes.
        """
        organization = self.get_one(slug)
        return update_db_instance(organization, payload)

    @route.post(
        "/update-contact-email",
        url_name="update_contact_email",
        response={200: schema.OrganizationRetrieveSchema},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def update_contact_email(self, slug: str, payload: EmailSchema) -> models.Organization:
        """Update organization contact email with verification.

        Updates the contact email for the organization and triggers an email
        verification flow. The email will be marked as unverified until the
        verification link is clicked.

        **Special Case:**
        If the new email matches the requester's verified email address,
        it will be automatically verified without requiring a verification link.

        **Parameters:**
        - `contact_email`: The new contact email address

        **Returns:**
        - 200: The updated organization (contact_email_verified will be False unless auto-verified)

        **Verification Flow:**
        1. Email is updated but marked as unverified
        2. Verification email is sent to the new address
        3. User clicks verification link
        4. Email is marked as verified

        **Error Cases:**
        - 400: Email is the same as current contact email
        - 403: User lacks edit_organization permission
        """
        organization = self.get_one(slug)
        organization_service.update_contact_email(
            organization=organization,
            new_email=payload.email,
            requester=self.user(),
        )
        return organization

    @route.post(
        "/verify-contact-email",
        url_name="verify_contact_email",
        response={200: schema.OrganizationRetrieveSchema},
    )
    def verify_contact_email(self, slug: str, payload: VerifyEmailSchema) -> models.Organization:
        """Verify organization contact email using token from email link.

        Completes the contact email verification process. This endpoint is
        typically called when a user clicks the verification link in their email.

        **Parameters:**
        - `token`: The verification token from the email (passed in request body)

        **Returns:**
        - 200: The organization with contact_email_verified = True
        - 400: Invalid token or verification failed

        **Error Cases:**
        - 400: Token is invalid, expired, or already used
        - 400: Organization not found
        - 400: Email address has changed since verification was sent
        """
        return organization_service.verify_contact_email(payload.token)

    @route.post(
        "/stripe/connect",
        url_name="stripe_connect",
        response=schema.StripeOnboardingLinkSchema,
        permissions=[IsOrganizationOwner()],
    )
    def stripe_connect(self, slug: str, payload: EmailSchema) -> schema.StripeOnboardingLinkSchema:
        """Get a link to onboard the organization to Stripe.

        **Parameters:**
        - `email`: The email address to associate with the Stripe Connect account (passed in request body)

        **Returns:**
        - 200: Onboarding URL for Stripe Connect

        **Permissions:**
        - Requires organization owner permission
        """
        organization = self.get_one(slug)
        account_id = organization.stripe_account_id or stripe_service.create_connect_account(
            organization, payload.email
        )
        onboarding_url = stripe_service.create_account_link(account_id, organization)
        return schema.StripeOnboardingLinkSchema(onboarding_url=onboarding_url)

    @route.post(
        "/stripe/account/verify",
        url_name="stripe_account_verify",
        response=schema.StripeAccountStatusSchema,
        permissions=[IsOrganizationOwner()],
    )
    def stripe_account_verify(self, slug: str) -> schema.StripeAccountStatusSchema:
        """Get the organization's Stripe account status."""
        organization = stripe_service.stripe_verify_account(self.get_one(slug))
        return schema.StripeAccountStatusSchema(
            is_connected=True,
            charges_enabled=organization.stripe_charges_enabled,
            details_submitted=organization.stripe_details_submitted,
        )

    @route.post(
        "/upload-logo",
        url_name="org_upload_logo",
        response=schema.OrganizationRetrieveSchema,
        permissions=[OrganizationPermission("edit_organization")],
    )
    def upload_logo(self, slug: str, logo: File[UploadedFile]) -> models.Organization:
        """Upload logo to organization."""
        organization = self.get_one(slug)
        organization = safe_save_uploaded_file(instance=organization, field="logo", file=logo, uploader=self.user())
        return organization

    @route.post(
        "/upload-cover-art",
        url_name="org_upload_cover_art",
        response=schema.OrganizationRetrieveSchema,
        permissions=[OrganizationPermission("edit_organization")],
    )
    def upload_cover_art(self, slug: str, cover_art: File[UploadedFile]) -> models.Organization:
        """Upload cover art to organization."""
        organization = self.get_one(slug)
        organization = safe_save_uploaded_file(
            instance=organization, field="cover_art", file=cover_art, uploader=self.user()
        )
        return organization

    @route.delete(
        "/delete-logo",
        url_name="org_delete_logo",
        response={204: None},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def delete_logo(self, slug: str) -> tuple[int, None]:
        """Delete logo and its derivatives from organization."""
        organization = self.get_one(slug)
        delete_image_with_derivatives(organization, "logo")
        return 204, None

    @route.delete(
        "/delete-cover-art",
        url_name="org_delete_cover_art",
        response={204: None},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def delete_cover_art(self, slug: str) -> tuple[int, None]:
        """Delete cover art and its derivatives from organization."""
        organization = self.get_one(slug)
        delete_image_with_derivatives(organization, "cover_art")
        return 204, None

    @route.post(
        "/create-event-series",
        url_name="create_event_series",
        response={200: schema.EventSeriesRetrieveSchema, 400: ValidationErrorResponse},
        permissions=[OrganizationPermission("create_event_series")],
    )
    def create_event_series(self, slug: str, payload: schema.EventSeriesEditSchema) -> models.EventSeries:
        """Create a new event series."""
        organization = self.get_one(slug)
        return models.EventSeries.objects.create(organization=organization, **payload.model_dump())

    @route.post(
        "/create-event",
        url_name="create_event",
        response={200: schema.EventDetailSchema, 400: ValidationErrorResponse},
        permissions=[OrganizationPermission("create_event")],
    )
    def create_event(self, slug: str, payload: schema.EventCreateSchema) -> models.Event:
        """Create a new event."""
        organization = self.get_one(slug)
        return models.Event.objects.create(organization=organization, **payload.model_dump())
