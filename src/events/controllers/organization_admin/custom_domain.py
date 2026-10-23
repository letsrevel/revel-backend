"""Org-admin custom-domain endpoints (white-label, #1151).

Owner-only and gated on the platform's ``custom_domain_enabled`` switch (403 when off). Session-only:
it is deliberately absent from the app-token allow-list.
"""

from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import IsOrganizationOwner
from events.service import custom_domain_service

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    auth=I18nJWTAuth(),
    tags=["Organization Admin - Custom Domain"],
    throttle=UserDefaultThrottle(),
    permissions=[IsOrganizationOwner()],
)
class OrganizationAdminCustomDomainController(OrganizationAdminBaseController):
    """Request, inspect and remove the organization's custom domain."""

    @route.get(
        "/custom-domain",
        url_name="get_custom_domain",
        response=schema.CustomDomainSchema | None,
    )
    def get_custom_domain(self, slug: str) -> models.OrganizationDomain | None:
        """Get the organization's custom domain with its status and DNS instructions.

        **Returns:**
        - 200: The domain (pending, active or failed), or `null` when none was requested

        **Error Cases:**
        - 403: Custom domains are not enabled for this organization, or caller is not the owner
        """
        return custom_domain_service.get_current_domain(self.get_one(slug))

    @route.post(
        "/custom-domain",
        url_name="request_custom_domain",
        response={201: schema.CustomDomainSchema},
        throttle=WriteThrottle(),
    )
    def request_custom_domain(
        self, slug: str, payload: schema.CustomDomainRequestSchema
    ) -> tuple[int, models.OrganizationDomain]:
        """Request a custom domain.

        Returns the DNS records the organization must create. The domain becomes active once
        Cloudflare has verified the DNS and issued the certificate.

        **Error Cases:**
        - 400: Invalid hostname
        - 403: Custom domains are not enabled for this organization, or caller is not the owner
        - 409: The organization already has a pending or active domain, or the hostname is taken
        - 502: The domain provisioning service failed
        """
        return 201, custom_domain_service.request_domain(self.get_one(slug), payload.hostname)

    @route.post(
        "/custom-domain/refresh",
        url_name="refresh_custom_domain",
        response=schema.CustomDomainSchema,
        throttle=WriteThrottle(),
    )
    def refresh_custom_domain(self, slug: str) -> models.OrganizationDomain:
        """Re-check the custom domain's status with the provisioning service now.

        **Error Cases:**
        - 403: Custom domains are not enabled for this organization, or caller is not the owner
        - 404: The organization has no custom domain
        - 502: The domain provisioning service failed
        """
        domain = custom_domain_service.get_current_domain_or_404(self.get_one(slug))
        return custom_domain_service.sync_domain_status(domain)

    @route.delete(
        "/custom-domain",
        url_name="delete_custom_domain",
        response={204: None},
        throttle=WriteThrottle(),
    )
    def delete_custom_domain(self, slug: str) -> tuple[int, None]:
        """Remove the custom domain (and its provisioning), freeing the hostname.

        **Error Cases:**
        - 403: Custom domains are not enabled for this organization, or caller is not the owner
        - 404: The organization has no custom domain
        - 502: The domain provisioning service failed
        """
        domain = custom_domain_service.get_current_domain_or_404(self.get_one(slug))
        custom_domain_service.delete_domain(domain)
        return 204, None
