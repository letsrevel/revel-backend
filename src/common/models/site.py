"""Site-wide singleton models: legal documents and global settings."""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from simple_history.models import HistoricalRecords
from solo.models import SingletonModel

from common.fields import MarkdownField


class Legal(SingletonModel):
    """Singleton model for legal documents like Terms and Conditions and Privacy Policy."""

    terms_and_conditions = MarkdownField(
        help_text="Terms and Conditions text in markdown format", blank=True, default=""
    )
    privacy_policy = MarkdownField(help_text="Privacy Policy text in markdown format", blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self) -> str:  # pragma: no cover
        return "Legal Documents"

    class Meta:
        verbose_name = "Legal Documents"


class SiteSettings(SingletonModel):
    """Singleton model for common application settings."""

    class BannerSeverity(models.TextChoices):
        DEBUG = "debug"
        INFO = "info"
        WARNING = "warning"
        ERROR = "error"
        CRITICAL = "critical"

    notify_user_joined = models.BooleanField(
        default=False, help_text="Send a notification when a new user joins the platform."
    )
    notify_organization_created = models.BooleanField(
        default=False, help_text="Send a notification when a new organization is created."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    data_retention_days = models.PositiveIntegerField(
        verbose_name="Data Retention Period",
        help_text="The number of days to retain data before deletion.",
        default=30,
    )
    live_emails = models.BooleanField(default=False, help_text="Live-emails enabled")
    frontend_base_url = models.URLField(default=settings.FRONTEND_BASE_URL)
    internal_catchall_email = models.EmailField(
        verbose_name="Internal Catchall Email",
        help_text="The catchall email address for internal use.",
        default=settings.INTERNAL_CATCHALL_EMAIL,
    )

    # Platform business details for VAT invoicing
    platform_business_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Legal business name for invoices (e.g., 'Revel S.r.l.').",
    )
    platform_business_address = models.TextField(
        blank=True,
        default="",
        help_text="Full business address for invoices.",
    )
    platform_vat_country = models.CharField(
        max_length=2,
        blank=True,
        default="",
        help_text="ISO 3166-1 alpha-2 country code for platform's VAT registration.",
    )
    platform_vat_id = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Platform's VAT ID including country prefix (e.g., IT12345678901).",
    )
    platform_vat_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Platform's domestic VAT rate (e.g., 22.00 for 22%).",
    )

    # Invoice settings
    platform_invoice_bcc_email = models.EmailField(
        blank=True,
        default="",
        help_text="BCC email for all platform fee invoices (internal accounting copy).",
    )

    # Maintenance banner
    maintenance_message = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Message to display in the maintenance banner. Leave blank to disable.",
    )
    maintenance_severity = models.CharField(
        max_length=20,
        choices=BannerSeverity.choices,
        default=BannerSeverity.INFO,
        help_text="Severity level of the maintenance banner.",
    )
    maintenance_scheduled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the maintenance window starts.",
    )
    maintenance_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the maintenance window ends. Banner is hidden after this time.",
    )

    history = HistoricalRecords()

    def clean(self) -> None:
        """Normalize maintenance_message so whitespace-only values become empty."""
        if self.maintenance_message:
            self.maintenance_message = self.maintenance_message.strip()

    @property
    def is_maintenance_banner_active(self) -> bool:
        """Whether the maintenance banner should currently be displayed."""
        from django.utils import timezone

        if not self.maintenance_message or not self.maintenance_message.strip():
            return False
        if self.maintenance_ends_at and self.maintenance_ends_at < timezone.now():
            return False
        return True

    def __str__(self) -> str:  # pragma: no cover
        return "Common Settings"

    class Meta:
        verbose_name = "Common Settings"
        verbose_name_plural = "Common Settings"
