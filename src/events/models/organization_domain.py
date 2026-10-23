"""OrganizationDomain: a custom hostname an organization serves its pages from (white-label, #1152)."""

from django.contrib.gis.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from common.models import TimeStampedModel
from events.utils.hostname import custom_hostname_validator, normalize_hostname

from .organization import Organization


class OrganizationDomain(TimeStampedModel):
    """A custom domain request and its lifecycle.

    Only the ``ACTIVE`` domain is resolved to an organization (through the denormalized
    ``Organization.custom_domain`` column); status transitions go through
    ``events.service.custom_domain_service``, which keeps that column and the tenant cache in sync.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACTIVE = "active", _("Active")
        FAILED = "failed", _("Failed")
        REMOVED = "removed", _("Removed")

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="domains")
    hostname = models.CharField(max_length=253, validators=[custom_hostname_validator])
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    cloudflare_hostname_id = models.CharField(max_length=64, null=True, blank=True)
    verification_records = models.JSONField(
        default=list,
        blank=True,
        help_text=_("DNS records the organization must create to verify ownership."),
    )
    last_errors = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Problems Cloudflare reported at the last status sync (hostname verification, certificate)."),
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["hostname"],
                condition=~Q(status="removed"),
                name="unique_hostname_among_non_removed",
                violation_error_message=_("This hostname is already in use."),
            ),
            models.UniqueConstraint(
                fields=["organization"],
                condition=Q(status__in=["pending", "active"]),
                name="one_live_domain_per_organization",
                violation_error_message=_("This organization already has a pending or active domain."),
            ),
        ]

    def __str__(self) -> str:
        return self.hostname

    def clean(self) -> None:
        """Store the normalized hostname so constraints and lookups compare canonical values."""
        super().clean()
        self.hostname = normalize_hostname(self.hostname)
